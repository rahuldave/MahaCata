"""The shared logic of object stores: S3, Google Cloud Storage and Azure Blob.

An object store keeps flat keys such as ``a/b/c.csv`` in a bucket, and "/"
makes the folders. ObjectStore turns that into stat, list and fetch, with the
cache, folder fetches and offline mode. A driver gives four primitives:

- ``locate(uri)``: the bucket and the key that a URI names.
- ``_head(uri, bucket, key)``: the size and version of one object, or NotFound.
- ``_pages(uri, bucket, prefix, delimiter, limit)``: pages of a listing. Each
  page is a pair of lists, the folder prefixes and the objects as
  ``(key, size, version)``.
- ``_get(uri, bucket, key, out)``: write the object's bytes to the open file
  ``out``, and return the version of the bytes written.
- ``_put(uri, bucket, key, src)``: write the local file ``src`` as the object,
  and return the version of the bytes written.
- ``_delete(uri, bucket, key)``: delete the object, or raise NotFound.

The version is whatever changes when the bytes change: the ETag on S3, the
generation on GCS, the ETag on Azure. The cache keeps it, and fetches a file
again when it changes.

A folder fetch refuses a folder that holds keys no URI can name, for example
``a//b``, and names that one disk could store as one, for example ``Dir/a`` and
``dir/b``. Fetches of one folder run one at a time. It marks the folder done
only after every file is current and every deleted object is gone from the
cache.
"""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from ..cache import LocalCache, find_twins
from ..errors import NotFound, OfflineMiss, StoreError, UriError
from ..uri import Uri
from . import Entry, Store

Page = tuple[list[str], list[tuple[str, int, str]]]


class ObjectStore(Store):
    page_size: int | None = None  # None leaves it to the server; tests set it small to force paging

    @abstractmethod
    def locate(self, uri: Uri) -> tuple[str, str]: ...

    @abstractmethod
    def _head(self, uri: Uri, bucket: str, key: str) -> tuple[int, str]: ...

    @abstractmethod
    def _pages(self, uri: Uri, bucket: str, prefix: str, delimiter: bool,
               limit: int | None = None) -> Iterator[Page]: ...

    @abstractmethod
    def _get(self, uri: Uri, bucket: str, key: str, out: BinaryIO) -> str: ...

    @abstractmethod
    def _put(self, uri: Uri, bucket: str, key: str, src: Path) -> str: ...

    @abstractmethod
    def _delete(self, uri: Uri, bucket: str, key: str) -> None: ...

    def _is_marker(self, uri: Uri, bucket: str, key: str) -> bool:
        """True if the key is a folder marker that is not a file, as on ADLS Gen2."""
        return False

    def _online(self, uri: Uri) -> None:
        if self.offline:
            raise OfflineMiss(f"{uri}: offline, and only fetch can use the cache")

    def _key(self, uri: Uri) -> list[str]:
        return [uri.authority, *uri.segments]

    @staticmethod
    def _child(uri: Uri, rel: str) -> Uri | None:
        try:
            for seg in rel.split("/"):
                uri = uri.child(seg)
            return uri
        except UriError:
            return None  # a key that no URI can name, for example one with "//"

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        self._online(uri)
        bucket, key = self.locate(uri)
        if key:
            try:
                size, version = self._head(uri, bucket, key)
                return Entry(uri, "file", size, version)
            except NotFound:
                pass
        prefix = key + "/" if key else ""
        for folders, objects in self._pages(uri, bucket, prefix, delimiter=False, limit=1):
            if any(p.startswith(prefix) for p in folders) or any(o[0].startswith(prefix) for o in objects):
                return Entry(uri, "folder")
            break
        if not key:
            return Entry(uri, "folder")
        raise NotFound(f"{uri}: no object and no folder")

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        self._online(uri)
        bucket, key = self.locate(uri)
        prefix = key + "/" if key else ""
        entries, seen, named = [], False, set()
        for folders, objects in self._pages(uri, bucket, prefix, delimiter=True):
            # ADLS Gen2 lists the file "a/b" for the prefix "a/b/", so keep only
            # what is really below the prefix.
            folders = [p for p in folders if p.startswith(prefix)]
            objects = [o for o in objects if o[0].startswith(prefix)]
            seen = seen or bool(folders or objects)
            for p in folders:
                child = self._child(uri, p[len(prefix):].rstrip("/"))
                # A folder can come twice, on two pages, as a prefix and as an
                # ADLS marker blob; list it once.
                if child and str(child) not in named:
                    named.add(str(child))
                    entries.append(Entry(child, "folder"))
            for k, size, version in objects:
                rel = k[len(prefix):]
                child = self._child(uri, rel) if rel else None
                if child:
                    entries.append(Entry(child, "file", size, version))
        if not seen and key:
            # An object store lists a missing prefix as empty. A folder with any
            # key below it, even a marker or a key no URI can name, still lists
            # as []. Only a prefix with nothing below it is an error.
            try:
                self._head(uri, bucket, key)
            except NotFound:
                if self._is_marker(uri, bucket, key):
                    return []
                raise NotFound(f"{uri}: no object and no folder") from None
            raise NotFound(f"{uri}: is a file, not a folder")
        return entries

    def walk(self, uri: Uri) -> list[Entry]:
        """Every file below a folder, at any depth.

        Keys that end in "/" are folder markers and are left out. Any other key
        that no URI can name is an error, so a fetched folder is never short.
        """
        bucket, key = self.locate(uri)
        prefix = key + "/" if key else ""
        files, bad = [], []
        for _, objects in self._pages(uri, bucket, prefix, delimiter=False):
            for k, size, version in objects:
                if not k.startswith(prefix):
                    continue
                rel = k[len(prefix):]
                if not rel or rel.endswith("/"):
                    continue
                child = self._child(uri, rel)
                if child is None:
                    bad.append(k)
                else:
                    files.append(Entry(child, "file", size, version))
        if bad:
            raise StoreError(f"{uri}: keys that no URI can name, so the folder cannot be cached: {bad[:5]}")
        twins = find_twins(f.uri.segments[len(uri.segments):] for f in files)
        if twins:
            raise StoreError(f"{uri}: {twins[0]!r} and {twins[1]!r} would be one name on a disk "
                             "that ignores case or Unicode form")
        return files

    def _download(self, entry: Entry, cache: LocalCache) -> Path:
        bucket, key = self.locate(entry.uri)

        def fill(tmp: Path) -> str:
            with tmp.open("wb") as f:
                return self._get(entry.uri, bucket, key, f)

        return cache.put(self.name, self._key(entry.uri), fill)

    def fetch(self, uri: Uri, cache: LocalCache) -> Path:
        key = self._key(uri)
        self.locate(uri)
        if self.offline:
            hit = cache.get(self.name, key, None) or cache.folder_done(self.name, key, None)
            if hit is None:
                raise OfflineMiss(f"{uri}: not in the cache")
            return hit
        entry = self.stat(uri, cache)
        if entry.kind == "file":
            return cache.get(self.name, key, entry.version) or self._download(entry, cache)
        try:
            with cache.lock(self.name, key):
                return self._fetch_folder(uri, key, cache)
        except OSError as e:
            raise StoreError(f"cannot lock {cache.path(self.name, key)} in the cache: {e}") from e

    def _write_target(self, uri: Uri) -> tuple[str, str]:
        self._online(uri)
        bucket, key = self.locate(uri)
        if not key or key.endswith("/"):
            raise UriError(f"{uri}: name an object to write, not a bucket or folder")
        return bucket, key

    def _is_folder(self, uri: Uri, bucket: str, key: str) -> bool:
        """True if some key starts with key + "/", or key is a folder marker."""
        if self._is_marker(uri, bucket, key):
            return True
        prefix = key + "/"
        for folders, objects in self._pages(uri, bucket, prefix, delimiter=False, limit=1):
            # ADLS Gen2 lists the file a/b itself for the prefix a/b/, so filter.
            return any(f.startswith(prefix) for f in folders) or any(o[0].startswith(prefix) for o in objects)
        return False

    def put(self, uri: Uri, src: Path, cache: LocalCache) -> Entry:
        bucket, key = self._write_target(uri)
        if self._is_folder(uri, bucket, key):
            raise StoreError(f"{uri}: a folder; write to a name inside it")
        size = Path(src).stat().st_size
        version = self._put(uri, bucket, key, Path(src))
        return Entry(uri, "file", size, version)

    def delete(self, uri: Uri, cache: LocalCache) -> None:
        bucket, key = self._write_target(uri)
        try:
            self._delete(uri, bucket, key)
        except NotFound:
            if self._is_folder(uri, bucket, key):
                raise StoreError(f"{uri}: a folder; delete the objects in it") from None
            raise

    def _fetch_folder(self, uri: Uri, key: list[str], cache: LocalCache) -> Path:
        cache.unmark_folder(self.name, key)
        files = self.walk(uri)
        keep = set()
        for f in files:
            keep.add(tuple(f.uri.segments[len(uri.segments):]))
            if cache.get(self.name, self._key(f.uri), f.version) is None:
                self._download(f, cache)
        cache.prune(self.name, key, keep)
        folder = cache.path(self.name, key)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(f"cannot write {folder} in the cache: {e}") from e
        cache.mark_folder(self.name, key, "listed")
        return folder
