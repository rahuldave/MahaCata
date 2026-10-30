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

A bucket with versioning on keeps every version of an object, and gives each
an ID. Then a URI can name a fixed version:

- ``?at=version:ID`` names one version of one file. ``pin`` of a file gives it.
- ``?at=commit:ID`` names a provcat commit. ``commit`` lists every file under a
  folder with its version ID, and writes the list as one object,
  ``.provcat/commits/<ID>.json``, at the top of the bucket. The ID is the
  SHA-256 of that object, so a reader checks the list it got. A folder, or a
  file in it, at a commit reads the versions in the list. ``pin`` of a folder
  gives its newest commit, or the newest commit of a folder above it.

A bucket writes one object at a time, so a commit takes two steps that make it
safe while other programs write:

- **The file list is a snapshot.** A commit lists every version under the
  folder, deleted ones too, twice. Each write makes a new version ID, so two
  equal listings mean that nothing changed between them. Then each listed
  version was current at one moment. If they differ, it tries again, and then
  gives up.
- **The commits of a folder have one order.** Commit N of a folder is the
  object ``.provcat/log/<SHA-256 of the folder>/<10^15 - N>``, which holds its
  ID. The commit creates it only if it is not there yet, so of two commits at
  once one gets N and the other tries again with N + 1. The newest commit
  lists first, and each commit names the one before it as its parent.

``branch`` copies a branch, the first folder in the bucket, or a commit, to a
new branch. The copy is file by file, so a branch that fails halfway is left
half made.

A driver with versions gives five more primitives:

- ``_version_of(uri, bucket, key)``: the version ID of the current object.
- ``_history(uri, bucket, prefix)``: every version under the prefix, deleted
  ones too, as ``(key, version ID, size, current)``. A delete marker has the
  size None.
- ``_head_version`` and ``_get_version``: ``_head`` and ``_get`` of one version.
- ``_create(uri, bucket, key, src)``: ``_put`` only if no object has the key;
  else it raises Exists.
- ``_copy(uri, bucket, key, version, to)``: copy an object, or one version of it.

``_version_of`` and ``_history`` raise a StoreError when the bucket keeps no
versions.

A folder fetch refuses a folder that holds keys no URI can name, for example
``a//b``, and names that one disk could store as one, for example ``Dir/a`` and
``dir/b``. Fetches of one folder run one at a time. It marks the folder done
only after every file is current and every deleted object is gone from the
cache.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tempfile
from abc import abstractmethod
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from ..cache import LocalCache, find_twins
from ..errors import NotFound, OfflineMiss, StoreError, UriError
from ..uri import Selector, Uri
from . import Entry, Store

Page = tuple[list[str], list[tuple[str, int, str]]]

META = ".provcat"  # the folder at the top of a bucket that holds commits
_COMMIT_ID = re.compile(r"[0-9a-f]{64}")
_FORMAT = "provcat-commit/1"
_NEVER = 10**15  # log entry N is named _NEVER - N, so the newest lists first
_TRIES = 5  # listings that may differ, and log numbers that may be taken, before a commit gives up


class Exists(StoreError):
    """A create found an object at its key."""


def _canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _in_meta(key: str) -> bool:
    return key == META or key.startswith(META + "/")


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

    def _version_of(self, uri: Uri, bucket: str, key: str) -> str:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) keeps no versions")

    def _history(self, uri: Uri, bucket: str, prefix: str) -> list[tuple[str, str, int | None, bool]]:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) keeps no versions")

    def _create(self, uri: Uri, bucket: str, key: str, src: Path) -> None:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) cannot write only if absent")

    def _head_version(self, uri: Uri, bucket: str, key: str, version: str) -> int:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) keeps no versions")

    def _get_version(self, uri: Uri, bucket: str, key: str, version: str, out: BinaryIO) -> None:
        raise StoreError(f"{uri}: store {self.name} ({self.driver}) keeps no versions")

    def _copy(self, uri: Uri, bucket: str, key: str, version: str | None, to: str) -> None:
        """Copy an object, or one version of it, to another key. This one
        downloads and uploads; a driver that can copy in the cloud does that."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "data"
            with tmp.open("wb") as f:
                if version is None:
                    self._get(uri, bucket, key, f)
                else:
                    self._get_version(uri, bucket, key, version, f)
            self._put(uri, bucket, to, tmp)

    def _root(self, uri: Uri) -> Uri:
        """The URI of the top of the bucket that uri is in."""
        return Uri(uri.scheme, uri.authority)

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
        if uri.selector is not None:
            return self._stat_at(uri, cache)
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
        if uri.selector is not None:
            return self._list_at(uri, cache)
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
        if uri.selector is not None:
            return self._fetch_at(uri, cache)
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
        if uri.selector is not None:
            raise UriError(f"{uri}: ?at={uri.selector} names a fixed version; write without ?at=")
        bucket, key = self.locate(uri)
        if not key or key.endswith("/"):
            raise UriError(f"{uri}: name an object to write, not a bucket or folder")
        if _in_meta(key):
            raise UriError(f"{uri}: {META} at the top of a bucket holds provcat's commits; write elsewhere")
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

    # Fixed versions: ?at=version:ID and ?at=commit:ID

    def _selector(self, uri: Uri) -> Selector:
        sel = uri.selector
        if sel is None or sel.kind not in ("version", "commit"):
            raise UriError(f"{uri}: a {self.driver} URI takes ?at=version:ID or ?at=commit:ID")
        if sel.kind == "commit" and not _COMMIT_ID.fullmatch(sel.value):
            raise UriError(f"{uri}: a commit ID is 64 hex digits")
        return sel

    def _meta_uri(self, uri: Uri, *names: str) -> Uri:
        out = self._root(uri).child(META)
        for n in names:
            out = out.child(n)
        return out

    def _manifest(self, uri: Uri, cid: str, cache: LocalCache | None) -> dict:
        """The file list of commit cid. Its bytes must hash to cid, so cached
        bytes that hash right are good without a network call. With no cache,
        it reads the commit straight from the store."""
        where = self._meta_uri(uri, "commits", cid + ".json")
        cached = cache.get(self.name, self._key(where), None) if cache is not None else None
        data = cached.read_bytes() if cached else None
        if data is None or hashlib.sha256(data).hexdigest() != cid:
            try:
                if cache is not None:
                    data = self.fetch(where, cache).read_bytes()
                else:
                    self._online(uri)
                    bucket, _ = self.locate(uri)
                    buf = io.BytesIO()
                    self._get(where, bucket, f"{META}/commits/{cid}.json", buf)
                    data = buf.getvalue()
            except NotFound:
                raise NotFound(f"{uri}: no commit {cid} in this bucket") from None
            if hashlib.sha256(data).hexdigest() != cid:
                raise StoreError(f"{where} does not match its ID; someone changed it")
        try:
            m = json.loads(data)
        except ValueError:
            raise StoreError(f"{where} is not JSON") from None
        if not isinstance(m, dict) or m.get("format") != _FORMAT or not isinstance(m.get("files"), dict) \
                or not isinstance(m.get("folder"), str):
            raise StoreError(f"{where} is not a provcat commit")
        return m

    def _commit_view(self, uri: Uri, cache: LocalCache) -> tuple[dict, str]:
        """The commit's file list and the path of uri inside the committed folder."""
        sel = self._selector(uri)
        bucket, key = self.locate(uri)
        m = self._manifest(uri, sel.value, cache)
        folder = m["folder"]
        if folder and key != folder and not key.startswith(folder + "/"):
            raise NotFound(f"{uri}: commit {sel.value} is of {folder!r}, which does not hold {key!r}")
        return m, key[len(folder):].lstrip("/") if folder else key

    def _pinned_store(self, sel: Selector) -> str:
        """The cache folder for one fixed version, apart from the moving names."""
        return f"{self.name}@{sel.kind}-{quote(sel.value, safe='')}"

    def _stat_at(self, uri: Uri, cache: LocalCache) -> Entry:
        sel = self._selector(uri)
        if sel.kind == "version":
            self._online(uri)
            bucket, key = self.locate(uri)
            if not key:
                raise UriError(f"{uri}: a version names one file")
            return Entry(uri, "file", self._head_version(uri, bucket, key, sel.value), sel.value)
        m, rel = self._commit_view(uri, cache)
        f = m["files"].get(rel)
        if f is not None:
            return Entry(uri, "file", f["size"], f["version"])
        if not rel or any(k.startswith(rel + "/") for k in m["files"]):
            return Entry(uri, "folder")
        raise NotFound(f"{uri}: not in commit {sel.value}")

    def _list_at(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        entry = self._stat_at(uri, cache)
        if entry.kind == "file":
            raise NotFound(f"{uri}: is a file, not a folder")
        m, rel = self._commit_view(uri, cache)
        prefix = rel + "/" if rel else ""
        out, folders = [], set()
        for k, f in sorted(m["files"].items()):
            if not k.startswith(prefix):
                continue
            first, sep, _ = k[len(prefix):].partition("/")
            if sep:
                if first not in folders:
                    folders.add(first)
                    out.append(Entry(uri.child(first), "folder"))
            else:
                out.append(Entry(uri.child(first), "file", f["size"], f["version"]))
        return out

    def _fetch_at(self, uri: Uri, cache: LocalCache) -> Path:
        sel = self._selector(uri)
        store, key = self._pinned_store(sel), self._key(uri)
        if sel.kind == "commit":
            m, rel = self._commit_view(uri, cache)
            f = m["files"].get(rel)
            if f is None:
                return self._fetch_commit_folder(uri, m, rel, cache)
            version = f["version"]
        else:
            version = sel.value
        hit = cache.get(store, key, version)
        if hit is not None:
            return hit
        self._online(uri)
        bucket, k = self.locate(uri)
        if sel.kind == "commit":
            k = (m["folder"] + "/" + rel) if m["folder"] else rel

        def fill(tmp: Path) -> str:
            with tmp.open("wb") as out:
                self._get_version(uri, bucket, k, version, out)
            return version

        return cache.put(store, key, fill)

    def _fetch_commit_folder(self, uri: Uri, m: dict, rel: str, cache: LocalCache) -> Path:
        sel = self._selector(uri)
        store, key = self._pinned_store(sel), self._key(uri)
        prefix = rel + "/" if rel else ""
        files = {k[len(prefix):]: f for k, f in m["files"].items() if k.startswith(prefix)}
        if not files and rel:
            raise NotFound(f"{uri}: not in commit {sel.value}")
        twins = find_twins(tuple(n.split("/")) for n in files)
        if twins:
            raise StoreError(f"{uri}: {twins[0]!r} and {twins[1]!r} would be one name on a disk "
                             "that ignores case or Unicode form")
        hit = cache.folder_done(store, key, sel.value)
        if hit is not None:
            return hit
        bucket, _ = self.locate(uri)
        base = m["folder"] + "/" + prefix if m["folder"] else prefix
        try:
            with cache.lock(store, key):
                cache.unmark_folder(store, key)
                for name, f in sorted(files.items()):
                    child = key + name.split("/")
                    if cache.get(store, child, f["version"]) is not None:
                        continue
                    self._online(uri)

                    def fill(tmp: Path, name=name, f=f) -> str:
                        with tmp.open("wb") as out:
                            self._get_version(uri, bucket, base + name, f["version"], out)
                        return f["version"]

                    cache.put(store, child, fill)
                cache.prune(store, key, {tuple(n.split("/")) for n in files})
                folder = cache.path(store, key)
                folder.mkdir(parents=True, exist_ok=True)
                cache.mark_folder(store, key, sel.value)
                return folder
        except OSError as e:
            raise StoreError(f"cannot write {cache.path(store, key)} in the cache: {e}") from e

    # Pins, commits and branches

    def _log_prefix(self, folder: str) -> str:
        return f"{META}/log/{hashlib.sha256(folder.encode()).hexdigest()}/"

    def _read_small(self, uri: Uri, bucket: str, key: str) -> bytes:
        buf = io.BytesIO()
        self._get(uri, bucket, key, buf)
        return buf.getvalue()

    def _newest_commit(self, uri: Uri, bucket: str, folder: str) -> tuple[int, str] | None:
        """The number and ID of folder's newest commit, or None."""
        prefix = self._log_prefix(folder)
        for _, objects in self._pages(uri, bucket, prefix, delimiter=False, limit=1):
            for k, _, _ in objects:
                name = k[len(prefix):] if k.startswith(prefix) else ""
                if not name.isdigit():
                    raise StoreError(f"{uri}: {k} is not a commit log entry; {META} is damaged")
                cid = self._read_small(uri, bucket, k).decode(errors="replace").strip()
                if not _COMMIT_ID.fullmatch(cid):
                    raise StoreError(f"{uri}: {k} does not hold a commit ID; {META} is damaged")
                return _NEVER - int(name), cid
            break
        return None

    def pin(self, uri: Uri, cache: LocalCache) -> Uri:
        """A file pins to its version, and a folder to the newest commit that holds it."""
        if uri.selector is not None:
            self._stat_at(uri, cache)
            return uri
        self._online(uri)
        bucket, key = self.locate(uri)
        try:
            entry = self.stat(uri, cache)
        except NotFound as e:
            gone = e  # deleted since; a commit may still hold it
        else:
            gone = None
            if entry.kind == "file":
                return uri.with_selector(Selector("version", self._version_of(uri, bucket, key)))
        # Each folder above has its own log, so only their times compare them.
        parts = key.split("/") if key else []
        found = []
        for n in range(len(parts), -1, -1):
            newest = self._newest_commit(uri, bucket, "/".join(parts[:n]))
            if newest:
                found.append((self._manifest(uri, newest[1], cache)["time"], newest[1]))
        for _, cid in sorted(found, reverse=True):
            pinned = uri.with_selector(Selector("commit", cid))
            try:
                self._stat_at(pinned, cache)
                return pinned
            except NotFound:
                continue  # that commit was made before this folder had files
        if gone is not None:
            raise gone
        raise StoreError(f"{uri}: no provcat commit holds this folder; run provc commit on it first")

    def _snapshot(self, uri: Uri, bucket: str, key: str) -> dict:
        """The current files under key, from two equal listings of every version."""
        prefix = key + "/" if key else ""
        for _ in range(_TRIES):
            first = sorted(self._history(uri, bucket, prefix))
            if first == sorted(self._history(uri, bucket, prefix)):
                break
        else:
            raise StoreError(f"{uri}: the files kept changing while provcat listed them; commit again later")
        files, bad = {}, []
        for k, version, size, current in first:
            rel = k[len(prefix):] if k.startswith(prefix) else ""
            if not current or size is None or not rel or rel.endswith("/") or (not key and _in_meta(rel)):
                continue
            if self._child(uri, rel) is None:
                bad.append(k)
            else:
                files[rel] = {"version": version, "size": size}
        if bad:
            raise StoreError(f"{uri}: keys that no URI can name, so they cannot be committed: {bad[:5]}")
        return files

    def _create_bytes(self, uri: Uri, bucket: str, key: str, data: bytes) -> bool:
        """Create the object with these bytes; False if one was there."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "data"
            tmp.write_bytes(data)
            try:
                self._create(uri, bucket, key, tmp)
                return True
            except Exists:
                return False

    def commit(self, uri: Uri, message: str, metadata: dict[str, str] | None = None,
               allow_empty: bool = False) -> Uri:
        """Record the version of every file under a folder, and return the
        folder pinned to the new commit."""
        if not message:
            raise UriError(f"{uri}: a commit needs a message")
        self._online(uri)
        if uri.selector is not None:
            raise UriError(f"{uri}: commit a folder without ?at=")
        bucket, key = self.locate(uri)
        if _in_meta(key):
            raise UriError(f"{uri}: {META} holds provcat's commits; commit another folder")
        if key:
            try:
                self._head(uri, bucket, key)
                raise UriError(f"{uri}: a file; commit the folder it is in")
            except NotFound:
                pass
        for _ in range(_TRIES):
            # The newest commit first, then the files. Commit N took its files
            # before it made its log entry, so files taken after reading N are
            # never older than commit N's.
            last = self._newest_commit(uri, bucket, key)
            files = self._snapshot(uri, bucket, key)
            if not files:
                raise StoreError(f"{uri}: no files to commit")
            if last and not allow_empty and self._manifest(uri, last[1], None)["files"] == files:
                raise StoreError(f"{uri}: nothing to commit; no file changed since commit {last[1]}")
            number = last[0] + 1 if last else 1
            manifest = {"format": _FORMAT, "folder": key, "number": number,
                        "parent": last[1] if last else None, "message": message,
                        "metadata": {str(k): str(v) for k, v in (metadata or {}).items()},
                        "time": datetime.now(UTC).isoformat(timespec="milliseconds"), "files": files}
            data = _canonical(manifest)
            cid = hashlib.sha256(data).hexdigest()
            where = f"{META}/commits/{cid}.json"
            if not self._create_bytes(uri, bucket, where, data):
                # Its name is its hash, so what is there must be these bytes.
                if hashlib.sha256(self._read_small(uri, bucket, where)).hexdigest() != cid:
                    raise StoreError(f"{uri}: {where} does not match its ID; {META} is damaged")
            # The commit happens here: only one writer can create entry N.
            if self._create_bytes(uri, bucket, f"{self._log_prefix(key)}{_NEVER - number:015d}", cid.encode()):
                return uri.with_selector(Selector("commit", cid))
        raise StoreError(f"{uri}: other commits of this folder kept taking the next number; commit again")

    def branch(self, uri: Uri, source: str) -> Uri:
        """Make the branch that uri names by copying source: a branch, which is a
        folder at the top of the bucket, or a commit ID."""
        self._online(uri)
        if uri.selector is not None:
            raise UriError(f"{uri}: name the new branch without ?at=")
        bucket, key = self.locate(uri)
        if not key or "/" in key or _in_meta(key):
            raise UriError(f"{uri}: name the new branch as a folder at the top of the bucket")
        if not source:
            raise UriError(f"{uri}: a branch needs a source: a branch or a commit")
        if self._is_folder(uri, bucket, key):
            raise StoreError(f"{uri}: branch {key} already exists")
        try:
            self._head(uri, bucket, key)
            raise StoreError(f"{uri}: {key} is a file")
        except NotFound:
            pass
        if _COMMIT_ID.fullmatch(source):
            m = self._manifest(uri, source, None)
            first, _, below = m["folder"].partition("/")
            if not first:
                raise StoreError(f"{uri}: commit {source} is of the whole bucket, not of a branch")
            base = m["folder"] + "/"
            to = key + "/" + (below + "/" if below else "")
            plan = [(base + rel, f["version"], to + rel) for rel, f in sorted(m["files"].items())]
        else:
            if "/" in source or _in_meta(source) or source in (".", ".."):
                raise UriError(f"{uri}: the source {source!r} is not a branch name or a commit ID")
            plan = []
            for _, objects in self._pages(uri, bucket, source + "/", delimiter=False):
                for k, _, _ in objects:
                    if k.startswith(source + "/") and not k.endswith("/"):
                        plan.append((k, None, key + k[len(source):]))
            if not plan:
                raise NotFound(f"{uri}: no branch {source!r} with files in this bucket")
        for src, version, to in plan:
            self._copy(uri, bucket, src, version, to)
        return uri

