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

__all__ = ["DRIVERS", "Entry", "Store", "register_driver"]


@dataclass(frozen=True)
class Entry:
    """One file or folder in a store, as stat, list and put describe it."""

    uri: Uri  # The physical URI of the file or folder
    kind: str  # "file" or "folder"
    size: int | None = None  # The size in bytes; None for a folder, or when the store does not say
    version: str | None = None  # A value that changes when the content changes

    def as_dict(self) -> dict:  # The four fields, with the URI as text, ready for JSON
        """The entry as a plain dict, as provc prints it."""
        return {"uri": str(self.uri), "kind": self.kind, "size": self.size, "version": self.version}


class Store(ABC):
    """The interface of a store driver: read, and maybe write, the physical URIs of one kind of storage.

    A subclass sets ``driver``, the name that ``driver:`` in the configuration
    picks, and is registered with ``register_driver``."""

    driver: str = ""
    # True when fetch reads a branch at the version that pin gives. The
    # catalog then pins first, so the hooks at pin see each version read.
    fetch_pins: bool = False
    # True when a ref of any kind, a branch, a tag or a commit, can stand in
    # the path, as in lakeFS. A bucket sets it False: only a branch stands in
    # its path, and a fixed version goes in ?at=. The resolver reads it to fill
    # {ref} (see provcat.resolver).
    refs_in_path: bool = True

    def __init__(
        self,
        name: str,  # The store's name in the configuration, which is also the scheme of its URIs
        options: dict | None = None,  # The store's options from the configuration, without driver; each is checked against option_types
        *,
        offline: bool = False,  # Work from the cache alone; a driver raises OfflineMiss for what the cache does not hold
    ):
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

    def option_types(self) -> dict[str, type | tuple]:  # Each option's name and its type, or a tuple of types
        """The options this driver takes, and the type of each."""
        return {}

    def pin(
        self,
        uri: Uri,  # A physical URI in this store
        cache: LocalCache,  # The cache, which records what a moving name pointed at
    ) -> Uri:  # The URI with any moving name replaced by a fixed version
        """Replace a moving name, such as a branch, with a fixed version. A store without versions returns uri."""
        return uri

    @abstractmethod
    def stat(
        self,
        uri: Uri,  # A physical URI in this store
        cache: LocalCache,  # The cache, which offline mode reads
    ) -> Entry:  # The file or the folder at uri; NotFound when there is none
        """Describe one file or folder."""

    @abstractmethod
    def list(
        self,
        uri: Uri,  # A physical URI of a folder in this store
        cache: LocalCache,  # The cache, which offline mode reads
    ) -> list[Entry]:  # The files and folders directly inside it
        """List a folder, one level deep."""

    @abstractmethod
    def fetch(
        self,
        uri: Uri,  # A physical URI in this store
        cache: LocalCache,  # The cache, where a remote store copies the file or folder
    ) -> Path:  # A local path that holds the file or folder
        """Return a local path that holds a file or folder: a copy in the cache, or the file itself for a local store."""

    def read(
        self,
        uri: Uri,  # A physical URI of a file in this store
        cache: LocalCache,  # The cache, as for fetch
    ) -> bytes:  # The bytes of the file
        """Fetch a file and return its bytes."""
        return self.fetch(uri, cache).read_bytes()

    def put(
        self,
        uri: Uri,  # The physical URI to write
        src: Path,  # The local file to write
        cache: LocalCache,  # The cache
    ) -> Entry:  # The file written
        """Write a local file at uri. A store that cannot be written, as this base, raises StoreError."""
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) cannot be written")

    def delete(
        self,
        uri: Uri,  # The physical URI of the file to delete
        cache: LocalCache,  # The cache
    ) -> None:  # Nothing
        """Delete the object at uri. A store that cannot be written, as this base, raises StoreError."""
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) cannot be written")


_TYPE_NAMES = {str: "string", bool: "true or false", list: "list of strings", (int, float): "number"}

DRIVERS: dict[str, type[Store]] = {}  # Each driver class under its name, as register_driver adds them


def register_driver(
    cls: type[Store],  # A Store subclass; its driver attribute is the name it registers under
) -> type[Store]:  # The same class, so this works as a class decorator
    """Make a driver class available to the configuration under its ``driver`` name. A later class with the same name replaces it."""
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
