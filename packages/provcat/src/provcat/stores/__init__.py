"""Stores read and write physical URIs. One driver class serves one kind of storage.

This follows the Perl URI module: the URI scheme picks the class. A store is a
named instance of a driver with options, so two stores, such as ``s3://`` and a
configured ``s3-acme://``, can use the same driver with different endpoints.
Register a new driver with ``register_driver``.

A driver gives four things:

- ``stat(uri, cache)``: one Entry, a file or a folder, or NotFound.
- ``list(uri, cache)``: the Entries directly inside a folder.
- ``pin(uri, cache)``: the URI with any moving name (a branch) replaced by a fixed one.
- ``fetch(uri, cache)``: a local path that holds the object.

Every driver but ``github`` and ``hf`` can be written, and also gives:

- ``put(uri, src, cache)``: write the local file ``src`` at ``uri``, and return its Entry.
- ``delete(uri, cache)``: delete the object at ``uri``.

The ``lakefs`` driver and the object stores (``s3``, ``gcs`` and ``azure``) also
give ``commit`` and ``branch``. An object store needs versioning on its bucket
for a commit. ``lakefs`` also gives ``merge``. ``github`` and ``hf`` refuse writes
with a StoreError.

Every method takes the cache, because pinning a moving name records what it
pointed at, and offline mode reads only the cache.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..cache import LocalCache
from ..errors import ConfigError, StoreError
from ..uri import Uri


@dataclass(frozen=True)
class Entry:
    uri: Uri
    kind: str  # "file" or "folder"
    size: int | None = None
    version: str | None = None  # a value that changes when the content changes

    def as_dict(self) -> dict:
        return {"uri": str(self.uri), "kind": self.kind, "size": self.size, "version": self.version}


class Store(ABC):
    driver: str = ""
    # True when fetch reads a branch at the version that pin gives. The
    # catalog then pins first, so the hooks at pin see each version read.
    fetch_pins: bool = False

    def __init__(self, name: str, options: dict | None = None, *, offline: bool = False):
        self.name = name
        self.options = dict(options or {})
        self.offline = offline
        types = self.option_types()
        unknown = set(self.options) - set(types)
        if unknown:
            raise ConfigError(f"stores.{name}: unknown options {sorted(unknown)} for driver {self.driver}")
        for key, value in self.options.items():
            want = types[key]
            ok = isinstance(value, want) and not (want is not bool and isinstance(value, bool))
            if want is list:
                ok = ok and all(isinstance(v, str) for v in value)
            if not ok:
                raise ConfigError(f"stores.{name}.{key} must be a {_TYPE_NAMES.get(want, want)}")

    def option_types(self) -> dict[str, type | tuple]:
        """The options this driver takes, and the type of each."""
        return {}

    def pin(self, uri: Uri, cache: LocalCache) -> Uri:
        return uri

    @abstractmethod
    def stat(self, uri: Uri, cache: LocalCache) -> Entry: ...

    @abstractmethod
    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]: ...

    @abstractmethod
    def fetch(self, uri: Uri, cache: LocalCache) -> Path: ...

    def read(self, uri: Uri, cache: LocalCache) -> bytes:
        return self.fetch(uri, cache).read_bytes()

    def put(self, uri: Uri, src: Path, cache: LocalCache) -> Entry:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) cannot be written")

    def delete(self, uri: Uri, cache: LocalCache) -> None:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) cannot be written")


_TYPE_NAMES = {str: "string", bool: "true or false", list: "list of strings", (int, float): "number"}

DRIVERS: dict[str, type[Store]] = {}


def register_driver(cls: type[Store]) -> type[Store]:
    DRIVERS[cls.driver] = cls
    return cls


def make_store(name: str, raw: dict, *, offline: bool = False) -> Store:
    if not isinstance(raw, dict):
        raise ConfigError(f"stores.{name} must be a mapping")
    options = dict(raw)
    driver = options.pop("driver", name)
    cls = DRIVERS.get(driver)
    if cls is None:
        raise ConfigError(f"stores.{name}: unknown driver {driver!r}; known: {', '.join(sorted(DRIVERS))}")
    return cls(name, options, offline=offline)


DEFAULT_STORES = {"file": {"driver": "file"}, "s3": {"driver": "s3"}, "github": {"driver": "github"},
                  "gs": {"driver": "gcs"}, "az": {"driver": "azure"}, "hf": {"driver": "hf"}}

from . import azure, gcs, github, hf, lakefs, local, s3  # noqa: E402,F401  (registers the drivers)
