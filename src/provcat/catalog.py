"""The Catalog joins a configuration, a resolver, the stores and the cache.

    cat = Catalog.open()                       # provcat.yaml, PROVCAT_CONFIG or defaults
    cat.resolve("data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production")
    path = cat.fetch("github://rahuldave/provcat/README.md?at=branch:main")

A URI whose scheme is a store is already physical, so resolve returns it as it
is. A URI whose scheme is a logical scheme goes through the resolver once, and
the result must name a store.
"""
from __future__ import annotations

from pathlib import Path

from .cache import LocalCache
from .config import ConfigSource, check, open_config
from .errors import ConfigError, ResolveError
from .resolver import Resolution, Resolver, TemplateResolver
from .stores import DEFAULT_STORES, Entry, Store, make_store
from .uri import Uri, parse


class Catalog:
    def __init__(self, config: dict | None = None, *, cache_root: str | Path | None = None,
                 offline: bool = False, resolver: Resolver | None = None):
        cfg = check(config or {}, "config")
        self.config = cfg
        self.offline = offline
        stores = {**DEFAULT_STORES, **cfg.get("stores", {})}
        self.stores: dict[str, Store] = {n: make_store(n, s or {}, offline=offline) for n, s in stores.items()}
        self.resolver = resolver or TemplateResolver(cfg.get("schemes"), cfg.get("stages"))
        clash = [n for n in self.stores if self.resolver.handles(n)]
        if clash:
            raise ConfigError(f"{clash} are both stores and logical schemes; rename one")
        cache_cfg = cfg.get("cache", {})
        unknown = set(cache_cfg) - {"root"}
        if unknown:
            raise ConfigError(f"cache: unknown keys {sorted(unknown)}")
        self.cache = LocalCache(cache_root or cache_cfg.get("root"))

    @classmethod
    def open(cls, spec: str | ConfigSource | None = None, **kw) -> Catalog:
        source = spec if isinstance(spec, ConfigSource) else open_config(spec)
        return cls(source.load(), **kw)

    def resolve(self, uri: str | Uri) -> Resolution:
        u = parse(uri)
        if u.scheme in self.stores:
            return Resolution(u, u)
        if not self.resolver.handles(u.scheme):
            known = sorted(self.stores) + sorted(getattr(self.resolver, "specs", {}))
            raise ResolveError(f"unknown scheme {u.scheme!r}; known: {', '.join(known)}")
        res = self.resolver.resolve(u)
        if res.physical.scheme not in self.stores:
            raise ResolveError(f"{u} resolves to {res.physical}, whose scheme is not a store")
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

    def read(self, uri: str | Uri) -> bytes:
        p = self.fetch(uri)
        if p.is_dir():
            raise IsADirectoryError(f"{uri} is a folder")
        return p.read_bytes()
