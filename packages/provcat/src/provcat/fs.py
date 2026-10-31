"""An fsspec file system over a Catalog. It needs ``provcat[fs]``.

    fs = catalog.fs()
    with fs.open("data+table://acme/nlp/prod/corpora/wiki/part-0.csv") as f:
        rows = f.read()
    with fsspec.open("data+table://acme/nlp/prod/corpora/wiki/part-0.csv") as f:
        ...

The file system is a thin adapter. Each method calls a Catalog method, so the
hooks of the app run at every spot, and the base cache is the only cache.
``catalog.fs()`` registers the logical schemes of the catalog as fsspec
protocols, so fsspec.open, and libraries built on fsspec, take logical URIs.
A scheme that fsspec already knows, such as ``s3`` or ``data``, is refused.
Physical URIs work through the file system object, but they are not
registered, so fsspec's own s3, gcs and file systems stay as they are.

Paths are whole URIs, with the scheme and any ``?at=``. A name from ``ls`` or
``find`` is a URI under the one that was listed, with any ``/`` in its
selector written ``%2F``. A path with ``?at=`` is taken as it is, not as a
glob pattern.

A writer puts its file at close. fsspec's text mode and compression close
the file even when a ``with`` block fails, so the partial file is put. Inside
``with fs.transaction:`` each write is put only when the block ends without
an error. fsspec's caching layers, such as ``simplecache::``, keep copies
that the hooks do not see, so do not use them over a catalog.
"""
from __future__ import annotations

import shutil
from typing import Any

try:
    from fsspec import AbstractFileSystem
    from fsspec.registry import _registry, known_implementations, register_implementation
except ImportError as e:  # pragma: no cover - depends on the install
    raise ImportError("catalog.fs() needs fsspec: install provcat[fs]") from e

from .errors import ConfigError, NotFound, StoreError
from .stores import Entry
from .uri import Uri, parse


def _name(u: Uri) -> str:
    """The URI as a path: a "/" in the selector is written %2F, so that
    fsspec, which splits paths on "/", keeps the selector whole."""
    base, sep, query = str(u).partition("?")
    return base + sep + query.replace("/", "%2F")


def _missing(e: NotFound) -> FileNotFoundError:
    err = FileNotFoundError(str(e))
    err.__cause__ = e
    return err


class CatalogFileSystem(AbstractFileSystem):
    """fsspec over a Catalog. Use ``catalog.fs()`` to make one."""

    protocol = ("provcat",)
    root_marker = ""
    cachable = False
    _bound: Any = None

    def __init__(self, catalog=None, **kw):
        super().__init__(**kw)
        if catalog is None:
            catalog = self._bound
        if catalog is None:
            from .catalog import Catalog

            catalog = Catalog.open()
        self.catalog = catalog

    @classmethod
    def over(cls, catalog, register: bool = True) -> CatalogFileSystem:
        """A file system over catalog. With register, each logical scheme of
        the catalog becomes an fsspec protocol that opens this catalog. The
        last catalog to register a scheme in a process serves it."""
        if not register:
            return cls(catalog=catalog)
        schemes = tuple(sorted(getattr(catalog.resolver, "specs", {})))
        for s in schemes:
            current = _registry.get(s)
            ours = isinstance(current, type) and issubclass(current, CatalogFileSystem)
            if not ours and (current is not None or s in known_implementations):
                raise ConfigError(f"the logical scheme {s!r} is already an fsspec protocol; "
                                  f"rename the scheme to use it with fsspec")
        bound = type("BoundCatalogFileSystem", (cls,), {"_bound": catalog, "protocol": schemes or cls.protocol})
        for s in schemes:
            register_implementation(s, bound, clobber=True)
        return bound()

    @classmethod
    def _strip_protocol(cls, path):
        if isinstance(path, list):
            return [cls._strip_protocol(p) for p in path]
        return str(path)

    def unstrip_protocol(self, name: str) -> str:
        return name

    @staticmethod
    def _parent(path):
        u = parse(path)
        if not u.segments:
            return _name(u)
        return _name(Uri(u.scheme, u.authority, u.segments[:-1], u.selector))

    def _info(self, name: str, e: Entry) -> dict:
        return {"name": name, "size": e.size or 0, "type": "file" if e.kind == "file" else "directory",
                "version": e.version, "uri": str(e.uri)}

    def info(self, path, **kw) -> dict:
        path = self._strip_protocol(path)
        try:
            return self._info(path, self.catalog.stat(path))
        except NotFound as e:
            raise _missing(e) from e

    def ls(self, path, detail=True, **kw):
        path = self._strip_protocol(path)
        u = parse(path)
        try:
            here = self.catalog.stat(u)
            if here.kind == "file":
                out = [self._info(path, here)]
            else:
                out = [self._info(_name(u.child(e.uri.segments[-1])), e) for e in self.catalog.list(u)]
        except NotFound as e:
            raise _missing(e) from e
        return out if detail else [o["name"] for o in out]

    def expand_path(self, path, recursive=False, maxdepth=None, assume_literal=False, **kw):
        paths = path if isinstance(path, list) else [path]
        if any("?at=" in str(p) for p in paths):
            assume_literal = True  # "?" there starts a selector, not a pattern
        return super().expand_path(path, recursive=recursive, maxdepth=maxdepth,
                                   assume_literal=assume_literal, **kw)

    def _open(self, path, mode="rb", block_size=None, autocommit=True, cache_options=None, **kw):
        path = self._strip_protocol(path)
        try:
            return self.catalog.open_file(path, mode, autocommit=autocommit)
        except NotFound as e:
            raise _missing(e) from e

    def rm_file(self, path):
        path = self._strip_protocol(path)
        try:
            self.catalog.delete(path)
        except NotFound as e:
            raise _missing(e) from e
        except StoreError:
            if self.info(path)["type"] != "directory":
                raise
            # A folder goes away with its files; rm(recursive=True) deletes them.

    def cp_file(self, path1, path2, **kw):
        with self.catalog.open_file(self._strip_protocol(path1)) as src, \
                self.catalog.open_file(self._strip_protocol(path2), "wb") as dst:
            shutil.copyfileobj(src, dst)

    def mkdir(self, path, create_parents=True, **kw):
        pass  # a folder exists when a file is in it

    def makedirs(self, path, exist_ok=False):
        pass

    def ukey(self, path):
        return self.info(path).get("version") or super().ukey(path)
