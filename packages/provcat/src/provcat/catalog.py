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
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .cache import LocalCache, snapshot
from .config import ConfigSource, View, check, open_config, view
from .errors import ConfigError, ResolveError, StoreError, UriError
from .resolver import Resolution, Resolver, TemplateResolver
from .stores import DEFAULT_STORES, Entry, Store, make_store
from .uri import Selector, Uri, parse


@dataclass(frozen=True)
class Snapshot:
    """A read-only copy of one pinned version of a file or a folder."""

    uri: Uri  # as asked for
    pinned: Uri  # the physical URI, pinned to a version where the store has versions
    path: Path
    digest: str  # the SHA-256 of the file, or of the folder (see cache.tree_digest)
    kind: str  # "file" or "folder"


class Catalog:
    def __init__(self, config: dict | None = None, *, cache_root: str | Path | None = None,
                 offline: bool = False, resolver: Resolver | None = None, tenant: str | None = None):
        tenant = tenant or None
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
        self.resolver = resolver or TemplateResolver(cfg.get("schemes"), cfg.get("stages"), tenant)
        clash = [n for n in self.stores if self.resolver.handles(n)]
        if clash:
            raise ConfigError(f"{clash} are both stores and logical schemes; rename one")
        cache_cfg = cfg.get("cache", {})
        unknown = set(cache_cfg) - {"root"}
        if unknown:
            raise ConfigError(f"cache: unknown keys {sorted(unknown)}")
        self.cache = LocalCache(cache_root or cache_cfg.get("root"))

    @classmethod
    def open(cls, spec: str | ConfigSource | None = None, *, tenant: str | None = None, **kw) -> Catalog:
        """Open a configuration source. The tenant comes from the argument or
        PROVCAT_TENANT; with none, the catalog is the admin's."""
        if tenant is None:
            tenant = os.environ.get("PROVCAT_TENANT")
        tenant = tenant or None
        source = spec if isinstance(spec, ConfigSource) else open_config(spec)
        return cls(source.load(tenant), tenant=tenant, **kw)

    def resolve(self, uri: str | Uri) -> Resolution:
        u = parse(uri)
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

    def _store(self, uri: str | Uri) -> tuple[Store, Uri]:
        physical = self.resolve(uri).physical
        return self.stores[physical.scheme], physical

    def pin(self, uri: str | Uri) -> Uri:
        store, u = self._store(uri)
        return store.pin(u, self.cache)

    def stat(self, uri: str | Uri) -> Entry:
        store, u = self._store(uri)
        return store.stat(u, self.cache)

    def list(self, uri: str | Uri) -> list[Entry]:
        store, u = self._store(uri)
        return store.list(u, self.cache)

    def fetch(self, uri: str | Uri) -> Path:
        """Return a local path that holds the file or folder."""
        store, u = self._store(uri)
        return store.fetch(u, self.cache)

    def snapshot(self, uri: str | Uri) -> Snapshot:
        """Pin uri, fetch that version, and copy it into a read-only folder of
        the cache that no later fetch changes. A store without versions, such
        as file, pins a URI to itself; the digest still names the bytes."""
        u = parse(uri)
        pinned = self.pin(u)
        src = self.fetch(pinned)
        name = pinned.segments[-1] if pinned.segments else (pinned.authority or "root")
        path, digest = snapshot(self.cache, src, name)
        return Snapshot(u, pinned, path, digest, "folder" if path.is_dir() else "file")

    def read(self, uri: str | Uri) -> bytes:
        p = self.fetch(uri)
        if p.is_dir():
            raise IsADirectoryError(f"{uri} is a folder")
        return p.read_bytes()

    # Writes

    def _writable(self, uri: str | Uri) -> tuple[Store, Uri]:
        u = parse(uri)
        if u.selector is not None and u.selector.kind != "branch":
            raise UriError(f"{u}: ?at={u.selector} names a fixed version; write to a branch")
        store, physical = self._store(u)
        if physical.selector is not None and physical.selector.kind != "branch":
            raise UriError(f"{u} resolves to {physical}, a fixed version; write to a branch")
        if u.selector is not None and u.scheme != physical.scheme:
            # A scheme whose target has no {ref} drops the branch, so the
            # write would land on the default one.
            plain = self.resolve(Uri(u.scheme, u.authority, u.segments)).physical
            if plain == physical:
                raise UriError(f"{u}: scheme {u.scheme!r} does not use the branch in ?at=, "
                               f"so the write would go to {physical}")
        return store, physical

    def put(self, uri: str | Uri, src: str | Path | bytes) -> list[Entry]:
        """Write a local file, a local folder or bytes at uri, and return the
        Entries written. A folder is written file by file under uri."""
        store, u = self._writable(uri)
        if isinstance(src, bytes):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d) / "data"
                f.write_bytes(src)
                return [store.put(u, f, self.cache)]
        src = Path(src)
        if src.is_file():
            return [store.put(u, src, self.cache)]
        if not src.is_dir():
            raise FileNotFoundError(f"{src} does not exist")
        # Every target first, so a name that no URI can hold fails before
        # anything is written.
        plan = []
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            target = u
            for seg in f.relative_to(src).parts:
                target = target.child(seg)
            plan.append((target, f))
        return [store.put(target, f, self.cache) for target, f in plan]

    def delete(self, uri: str | Uri) -> None:
        store, u = self._writable(uri)
        store.delete(u, self.cache)

    def commit(self, uri: str | Uri, message: str, metadata: dict[str, str] | None = None) -> Uri:
        """Commit the branch that uri is on, and return the physical URI pinned
        to the new commit. The metadata records where the commit came from: a
        logical URI adds provcat.uri."""
        store, u = self._writable(uri)
        commit = getattr(store, "commit", None)
        if commit is None:
            raise StoreError(f"{u}: store {store.name} ({store.driver}) has no commits")
        meta = dict(metadata or {})
        logical = parse(uri)
        if logical.scheme != u.scheme:
            meta.setdefault("provcat.uri", str(logical))
        return commit(u, message, meta)

    def branch(self, uri: str | Uri, source: str) -> Uri:
        """Make the branch that uri names, from source: a branch, a tag or a commit."""
        store, u = self._writable(uri)
        make = getattr(store, "branch", None)
        if make is None:
            raise StoreError(f"{u}: store {store.name} ({store.driver}) has no branches")
        return make(u, source)

    def merge(self, uri: str | Uri, source: str, message: str = "", metadata: dict[str, str] | None = None,
              strategy: str | None = None) -> Uri:
        """Merge source, a branch, a tag or a commit, into the branch that uri
        is on, and return the physical URI pinned to the merge commit. As with
        commit, a logical URI adds provcat.uri to the metadata."""
        store, u = self._writable(uri)
        merge = getattr(store, "merge", None)
        if merge is None:
            raise StoreError(f"{u}: store {store.name} ({store.driver}) has no merges")
        meta = dict(metadata or {})
        logical = parse(uri)
        if logical.scheme != u.scheme:
            meta.setdefault("provcat.uri", str(logical))
        return merge(u, source, message, meta, strategy)
