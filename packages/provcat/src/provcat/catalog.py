"""The Catalog joins a configuration, a resolver, the stores and the cache.

    cat = Catalog.open()                       # provcat.yaml, PROVCAT_CONFIG or defaults
    cat.resolve("data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production")
    path = cat.fetch("github://rahuldave/provcat/README.md?at=branch:main")

A catalog for one tenant, ``Catalog.open(tenant="acme")`` or PROVCAT_TENANT,
sees only the shared configuration and that tenant's part, and refuses the
logical URIs of other tenants.

A URI whose scheme is a store is already physical, so resolve returns it as it
is. A URI whose scheme is a logical scheme goes through the resolver once, and
the result must name a store.

Writes take a logical or a physical URI:

    cat.put("data+table://acme/shared/dev/agreements/qa_outputs/part-0.parquet", "out.parquet")
    pinned = cat.commit("data+table://acme/shared/dev/agreements/qa_outputs", "qa run 42")

A snapshot pins a URI, fetches the pinned version, and copies it into a
read-only folder of the cache named by its content:

    snap = cat.snapshot("data+table://acme/shared/prod/agreements/qa_outputs")
    snap.pinned, snap.path, snap.digest

A write goes to a branch: a URI with no ?at=, which writes to the scheme's
default branch, or one with ?at=branch:NAME. Any other selector names a fixed
version, which cannot change, so it is refused.

Every method runs through a lifecycle spot, where the hooks of the app run
(see provcat.hooks). A caller can pass the hooks, as an orchestrator does:
``Catalog(config, hooks=..., app=..., principal=...)``. Otherwise the app comes
from PROVCAT_APP, and its hooks from the proxy configuration. ``close`` runs
the session spot again.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import hooks as _hooks
from .cache import LocalCache, snapshot
from .config import ConfigSource, View, check, open_config, view
from .errors import ConfigError, ResolveError, StoreError, UriError
from .files import CatalogReader, CatalogWriter
from .hooks import Hooks, Proxy, Request
from .resolver import Resolution, Resolver, TemplateResolver
from .stores import DEFAULT_STORES, Entry, Store, make_store
from .uri import Selector, Uri, parse

if TYPE_CHECKING:
    from .fs import CatalogFileSystem

__all__ = ["Catalog", "Snapshot"]


@dataclass(frozen=True)
class Snapshot:
    """A read-only copy of one pinned version of a file or a folder."""

    uri: Uri  # The URI as asked for, logical or physical
    pinned: Uri  # The physical URI, pinned to a fixed version; a file:// URI pins to itself
    path: Path  # The read-only copy, at .provcat/snapshots/KIND/DIGEST/NAME in the cache
    digest: str  # The SHA-256 of the file, or of the folder (see cache.tree_digest)
    kind: str  # "file" or "folder"


class Catalog:
    """Resolve, read and write catalog URIs, through the stores, the cache and the hooks.

    Most code opens one with ``Catalog.open``, which reads a configuration
    source. The constructor takes the configuration as a dict."""

    def __init__(
        self,
        config: dict | None = None,  # The configuration: stores, schemes, stages, entries, cache, tenants; None for the default stores alone
        *,
        cache_root: str | Path | None = None,  # The cache folder; None takes the app's cache_root, then cache.root, then the default
        offline: bool = False,  # Work from the cache alone: what it does not hold raises OfflineMiss
        resolver: Resolver | None = None,  # The resolver of logical URIs; None makes a TemplateResolver from the configuration
        tenant: str | None = None,  # The tenant whose view of the configuration to use; None for the admin
        app: str | None = None,  # The app whose hooks run. Without hooks= or principal=, it must be the process's app, from PROVCAT_APP, or None for that app. With hooks= or principal=, it names the caller's app; an app that the hooks file names needs its Proxy from hooks.load
        hooks: Hooks | Proxy | dict | None = None,  # A caller's own hooks: a Hooks, a Proxy from hooks.load, or a dict of spot to hooks; the process's hooks run after them
        principal: str | None = None,  # Who uses the catalog, as the hooks see it; None for the user of the process. As with hooks=, a process launched with PROVCAT_APP refuses it with ConfigError
    ):
        tenant = tenant or None
        if hooks is not None or principal is not None:
            # A caller such as an orchestrator brings hooks or a principal; the process's hooks run last.
            proxy = _hooks.given(hooks, app)
            self.app = proxy.app
        else:
            proxy = _hooks.process_proxy(app)
            self.app = _hooks.process_app()
        cfg = check(config or {}, "config")
        if isinstance(cfg, View):
            if cfg.tenant != tenant:
                raise ConfigError(f"this configuration is the view of {cfg.tenant or 'the admin'}, "
                                  f"not of {tenant or 'the admin'}")
        elif "tenants" in cfg or "admin" in cfg:
            cfg = view(cfg, tenant)
        elif tenant is not None:
            raise ConfigError(f"no tenant {tenant!r}: the configuration has no tenants section")
        self.tenant = tenant
        self.config = cfg
        self.offline = offline
        stores = {**DEFAULT_STORES, **cfg.get("stores", {})}
        self.stores: dict[str, Store] = {n: make_store(n, s or {}, offline=offline) for n, s in stores.items()}
        self.resolver = resolver or TemplateResolver(cfg.get("schemes"), cfg.get("stages"), tenant,
                                                     cfg.get("entries"), self._refs_in_path)
        for entry in getattr(self.resolver, "entries", {}).values():
            if entry.store not in self.stores and self.resolver.serves(entry.key):
                raise ConfigError(f"entries.{entry.key}: there is no store named {entry.store!r}")
        clash = [n for n in self.stores if self.resolver.handles(n)]
        if clash:
            raise ConfigError(f"{clash} are both stores and logical schemes; rename one")
        cache_cfg = cfg.get("cache", {})
        unknown = set(cache_cfg) - {"root"}
        if unknown:
            raise ConfigError(f"cache: unknown keys {sorted(unknown)}")
        # The --cache argument, then the app's cache_root, then cache.root.
        self.cache = LocalCache(cache_root or proxy.cache_root or cache_cfg.get("root"))
        self.hooks = proxy.hooks
        self.context = _hooks.context(proxy.app, tenant, principal)
        self._run("session", None, None, lambda r: None, event="open")

    def _refs_in_path(self, name: str) -> bool | None:
        """Whether store name keeps refs in its path; None for no such store."""
        store = self.stores.get(name)
        return None if store is None else getattr(store, "refs_in_path", True)

    def close(self) -> None:  # Nothing; a hook at close may still raise
        """End the session: the hooks at the session spot see event close."""
        self._run("session", None, None, lambda r: None, event="close")

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, kind, err, tb) -> None:
        if kind is None:
            self.close()
            return
        try:
            self.close()
        except Exception:
            pass  # the error from the body matters more

    def _run(self, spot: str, logical: Uri | None, physical: Uri | None, work, **args):
        return self.hooks.run(self.context, Request(spot, logical, physical, args), work)

    @classmethod
    def open(
        cls,
        spec: str | ConfigSource | None = None,  # A YAML path, an etcd:// URL or a ConfigSource; None takes PROVCAT_CONFIG, then ./provcat.yaml, then the defaults
        *,
        tenant: str | None = None,  # The tenant; None takes PROVCAT_TENANT, and with none the catalog is the admin's
        **kw: Any,  # The other arguments of Catalog, such as offline, cache_root, app and hooks
    ) -> Catalog:  # A catalog over the configuration that the source loads for the tenant
        """Open a catalog over a configuration source: a YAML file, etcd, or the defaults.

        The other keyword arguments go to Catalog, such as offline, cache_root,
        resolver, app, hooks and principal."""
        if tenant is None:
            tenant = os.environ.get("PROVCAT_TENANT")
        tenant = tenant or None
        source = spec if isinstance(spec, ConfigSource) else open_config(spec)
        return cls(source.load(tenant), tenant=tenant, **kw)

    def resolve(
        self,
        uri: str | Uri,  # The URI to resolve, logical or physical
    ) -> Resolution:  # The logical URI, the physical URI it names, and the steps between
        """Find the physical URI that a URI names.

        A logical URI runs the resolve spot; a physical one is its own answer."""
        u = parse(uri)
        if u.scheme in self.stores:
            return Resolution(u, u)
        return self._run("resolve", u, None, lambda r: self._lookup(r.logical))

    def _lookup(self, u: Uri) -> Resolution:
        if u.scheme in self.stores:
            return Resolution(u, u)
        if not self.resolver.handles(u.scheme):
            known = sorted(self.stores) + sorted(getattr(self.resolver, "specs", {}))
            raise ResolveError(f"unknown scheme {u.scheme!r}; known: {', '.join(known)}")
        res = self.resolver.resolve(u)
        if res.physical.scheme not in self.stores:
            raise ResolveError(f"{u} resolves to {res.physical}, but there is no store named "
                               f"{res.physical.scheme!r}; add one to the configuration")
        if res.ts_branch is not None and getattr(self.stores[res.physical.scheme], "ts_names_branch", False):
            # A GitHub URI has no branch in its path, so the time carries it.
            sel = Selector("ts", f"{res.ts_branch}@{res.physical.selector.value}")
            physical = res.physical.with_selector(sel)
            res = replace(res, physical=physical, via=(*res.via, f"ts on branch {res.ts_branch} -> {physical}"))
        return res

    def _store(self, uri: str | Uri) -> tuple[Uri | None, Uri]:
        """The logical URI, or None for a physical one, and the physical URI."""
        u = parse(uri)
        return (None if u.scheme in self.stores else u), self.resolve(u).physical

    def _driver(self, u: Uri) -> Store:
        if u.scheme not in self.stores:
            raise ResolveError(f"{u}: there is no store named {u.scheme!r}")
        return self.stores[u.scheme]

    def _pin(self, logical: Uri | None, u: Uri) -> Uri:
        return self._run("pin", logical, u, lambda r: self._driver(r.physical).pin(r.physical, self.cache))

    def _fetch(self, logical: Uri | None, u: Uri, pinned: bool = False) -> Path:
        if not pinned and getattr(self._driver(u), "fetch_pins", False):
            # This store pins inside fetch, so pin here first and let the
            # hooks at pin see the version.
            u = self._pin(logical, u)
        return self._run("fetch", logical, u, lambda r: self._driver(r.physical).fetch(r.physical, self.cache))

    def pin(
        self,
        uri: str | Uri,  # The URI to pin, logical or physical
    ) -> Uri:  # The physical URI, with any branch or time replaced by a fixed version; StoreError where the store cannot pin, as for a file in a bucket without versioning or a bucket folder with no provcat commit
        """Resolve uri and replace its moving name with a fixed version. It runs the pin spot."""
        return self._pin(*self._store(uri))

    def stat(
        self,
        uri: str | Uri,  # The URI of a file or a folder, logical or physical
    ) -> Entry:  # The file or the folder; NotFound when there is none
        """Describe one file or folder: its kind, size and version. It runs the stat spot."""
        logical, u = self._store(uri)
        return self._run("stat", logical, u, lambda r: self._driver(r.physical).stat(r.physical, self.cache))

    def list(
        self,
        uri: str | Uri,  # The URI of a folder, logical or physical
    ) -> list[Entry]:  # The files and folders directly inside it
        """List a folder, one level deep. It runs the list spot."""
        logical, u = self._store(uri)
        return self._run("list", logical, u, lambda r: self._driver(r.physical).list(r.physical, self.cache))

    def fetch(
        self,
        uri: str | Uri,  # The URI of a file or a folder, logical or physical
    ) -> Path:  # A local path that holds it: a copy in the cache, or for file:// the file itself
        """Return a local path that holds the file or folder."""
        return self._fetch(*self._store(uri))

    def snapshot(
        self,
        uri: str | Uri,  # The URI of a file or a folder, logical or physical
    ) -> Snapshot:  # The pinned URI, the read-only copy, its digest and its kind
        """Pin a URI, fetch that version, and copy it into a read-only folder of the cache.

        No later fetch changes the copy. The URI must be pinnable: a file
        or folder in lakeFS or GitHub, a file in a versioned bucket, a folder
        with a provcat commit, or a local file or folder. The file store pins a URI to
        itself; the digest still names the bytes. An object store without
        versioning cannot pin, so it raises a StoreError. The pin and the fetch
        run their own spots inside the snapshot spot."""
        u = parse(uri)
        logical, physical = self._store(u)

        def work(r: Request) -> Snapshot:
            pinned = self._pin(logical, r.physical)
            src = self._fetch(logical, pinned, pinned=True)
            name = pinned.segments[-1] if pinned.segments else (pinned.authority or "root")
            path, digest = snapshot(self.cache, src, name)
            return Snapshot(u, pinned, path, digest, "folder" if path.is_dir() else "file")

        return self._run("snapshot", logical, physical, work)

    def read(
        self,
        uri: str | Uri,  # The URI of a file, logical or physical
    ) -> bytes:  # The bytes of the file; IsADirectoryError for a folder
        """Fetch a file and return its bytes."""
        p = self.fetch(uri)
        if p.is_dir():
            raise IsADirectoryError(f"{uri} is a folder")
        return p.read_bytes()

    def open_file(
        self,
        uri: str | Uri,  # The URI of the file, logical or physical; a write takes a branch
        mode: str = "rb",  # "rb" to read, "wb" to write; any other mode raises ValueError
        autocommit: bool = True,  # For a write: True puts the file at close; False puts nothing until the caller calls the handle's commit()
    ) -> CatalogReader | CatalogWriter:  # A CatalogReader for "rb", a CatalogWriter for "wb"
        """Open a file to read ("rb") or to write ("wb").

        A read looks up and pins uri once, at open, and then reads only that
        version. It runs the pin and open spots, read_block for each read, and
        close with the byte ranges read. A write goes to a temporary file, and
        close runs the close spot around a put; with autocommit off, commit()
        does. Nothing is committed to the history of the store. If a hook at
        open refuses after the handle was made, the handle is dropped unseen."""
        u = parse(uri)
        made: list = []
        if mode == "rb":
            logical, physical = self._store(u)
            pinned = self._pin(logical, physical)

            def reader(r: Request) -> CatalogReader:
                path = self._fetch(logical, r.physical, pinned=True)
                if path.is_dir():
                    raise IsADirectoryError(f"{u} is a folder")

                def block(start: int, length: int, read):
                    return self._run("read_block", logical, r.physical, read, offset=start, length=length)

                def done(h: CatalogReader) -> None:
                    self._run("close", logical, r.physical, lambda r2: None, mode="rb", ranges=h.ranges)

                made.append(CatalogReader(u, r.physical, path, block, done))
                return made[-1]

            return self._opened(made, "open", logical, pinned, reader, mode="rb")
        if mode == "wb":
            logical, physical = self._writable(u)

            def writer(r: Request) -> CatalogWriter:
                def done(h: CatalogWriter, path: Path) -> None:
                    def put(r2: Request):
                        h.entries = [self._put(logical, r2.physical, path)]
                        return h.entries

                    self._run("close", logical, r.physical, put, mode="wb")

                made.append(CatalogWriter(u, done, autocommit))
                return made[-1]

            return self._opened(made, "open", logical, physical, writer, mode="wb")
        raise ValueError(f"mode {mode!r}: provcat opens a file with rb or wb")

    def _opened(self, made: list, *run, **args):
        try:
            return self._run(*run, **args)
        except BaseException:
            for h in made:
                h.abandon()
            raise

    def fs(
        self,
        register: bool = True,  # Register the logical schemes of the catalog as fsspec protocols
        replace: bool = False,  # Take a scheme over from a catalog that registered it with another context or other hooks
    ) -> CatalogFileSystem:  # A file system that reads and writes through this catalog
        """Make an fsspec file system over this catalog; it needs provcat[fs].

        Once the logical schemes are fsspec protocols, fsspec.open and pandas
        take logical URIs (see provcat.fs)."""
        from .fs import CatalogFileSystem

        return CatalogFileSystem.over(self, register=register, replace=replace)

    # Writes

    def _writable(self, uri: str | Uri) -> tuple[Uri | None, Uri]:
        u = parse(uri)
        if u.selector is not None and u.selector.kind != "branch":
            raise UriError(f"{u}: ?at={u.selector} names a fixed version; write to a branch")
        logical, physical = self._store(u)
        if physical.selector is not None and physical.selector.kind != "branch":
            raise UriError(f"{u} resolves to {physical}, a fixed version; write to a branch")
        if u.selector is not None and u.scheme != physical.scheme:
            # A scheme whose target has no {ref} drops the branch, so the
            # write would land on the default one.
            plain = self._lookup(Uri(u.scheme, u.authority, u.segments)).physical
            if plain == physical:
                raise UriError(f"{u}: scheme {u.scheme!r} does not use the branch in ?at=, "
                               f"so the write would go to {physical}")
        return logical, physical

    def _put(self, logical: Uri | None, u: Uri, f: Path) -> Entry:
        return self._run("put", logical, u, lambda r: self._driver(r.physical).put(r.physical, f, self.cache))

    def put(
        self,
        uri: str | Uri,  # Where to write, logical or physical, on a branch
        src: str | Path | bytes,  # A local file, a local folder, or the bytes to write
    ) -> list[Entry]:  # One Entry for each file written
        """Write a local file, a local folder or bytes at a URI on a branch.

        A folder is written file by file under the URI, and each file runs the
        put spot."""
        logical, u = self._writable(uri)
        if isinstance(src, bytes):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "data"
                f.write_bytes(src)
                return [self._put(logical, u, f)]
        src = Path(src)
        if src.is_file():
            return [self._put(logical, u, src)]
        if not src.is_dir():
            raise FileNotFoundError(f"{src} does not exist")
        # Every target first, so a name that no URI can hold fails before
        # anything is written.
        plan = []
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            target, name = u, logical
            for seg in f.relative_to(src).parts:
                target = target.child(seg)
                name = name.child(seg) if name else None
            plan.append((name, target, f))
        return [self._put(name, target, f) for name, target, f in plan]

    def delete(
        self,
        uri: str | Uri,  # The file to delete, logical or physical, on a branch
    ) -> None:  # Nothing; it runs the delete spot
        """Delete a file on a branch."""
        logical, u = self._writable(uri)
        self._run("delete", logical, u, lambda r: self._driver(r.physical).delete(r.physical, self.cache))

    def _history(self, spot: str, u: Uri, what: str) -> Store:
        store = self._driver(u)
        if getattr(store, spot, None) is None:
            raise StoreError(f"{u}: store {store.name} ({store.driver}) has no {what}")
        return store

    def commit(
        self,
        uri: str | Uri,  # A URI on the branch to commit, logical or physical
        message: str,  # The commit message
        metadata: dict[str, str] | None = None,  # Key-value pairs stored with the commit
    ) -> Uri:  # The physical URI of uri, pinned to the new commit
        """Commit the branch that a URI is on. It runs the commit spot.

        The metadata records where the commit came from: a logical URI adds
        provcat.uri. A store without commits raises StoreError."""
        logical, u = self._writable(uri)
        self._history("commit", u, "commits")
        meta = dict(metadata or {})
        if logical is not None:
            meta.setdefault("provcat.uri", str(logical))
        return self._run("commit", logical, u, lambda r: self._driver(r.physical).commit(
            r.physical, r.args["message"], r.args["metadata"]), message=message, metadata=meta)

    def branch(
        self,
        uri: str | Uri,  # A URI on the branch to make, logical or physical
        source: str,  # The branch, tag or commit that the new branch starts from
    ) -> Uri:  # The physical URI of the new branch
        """Make the branch that a URI names. A store without branches raises StoreError."""
        logical, u = self._writable(uri)
        self._history("branch", u, "branches")
        return self._run("branch", logical, u, lambda r: self._driver(r.physical).branch(
            r.physical, r.args["source"]), source=source)

    def merge(
        self,
        uri: str | Uri,  # A URI on the branch to merge into, logical or physical
        source: str,  # The branch, tag or commit to merge from
        message: str = "",  # The message of the merge commit
        metadata: dict[str, str] | None = None,  # Key-value pairs stored with the merge commit
        strategy: str | None = None,  # How a conflict is settled: None fails, "source-wins" or "dest-wins"
    ) -> Uri:  # The physical URI of uri, pinned to the merge commit
        """Merge a branch, a tag or a commit into the branch that a URI is on.

        As with commit, a logical URI adds provcat.uri to the metadata. A store
        without merges raises StoreError."""
        logical, u = self._writable(uri)
        self._history("merge", u, "merges")
        meta = dict(metadata or {})
        if logical is not None:
            meta.setdefault("provcat.uri", str(logical))
        return self._run("merge", logical, u, lambda r: self._driver(r.physical).merge(
            r.physical, r.args["source"], r.args["message"], r.args["metadata"], r.args["strategy"]),
            source=source, message=message, metadata=meta, strategy=strategy)
