"""The local cache.

The cache keeps a readable copy of remote objects, so a folder of it can be
mounted into a container. The layout is::

    <root>/<store>/<key...>                the cached files
    <root>/.provcat/meta/<hash>.json       the version of one cached file
    <root>/.provcat/done/<hash>.json       a folder that was fetched whole
    <root>/.provcat/refs/<hash>            a moving name pinned to a version
    <root>/.provcat/tmp/                   files and folders being written
    <root>/.provcat/locks/<hash>.lock      one lock for each folder install

A store decides the key. GitHub keys hold the commit, so they never change.
S3 keys are bucket and object key, and the meta file holds the ETag.

The records under .provcat are named by a hash of store and key, so two keys
can never share or block a record. A meta record also holds the size, the
modification time and the SHA-256 of the file it describes. If the time differs,
for example after the cache was copied, the file is hashed again and kept when
the hash matches. If anything else changed the bytes, the file is fetched again.

Two names that differ only in case or in Unicode form land in one file on macOS
and Windows. ``find_twins`` finds such names, and the stores refuse a folder
that holds them.

Every write goes to a temporary name under .provcat/tmp and then is renamed, so
a reader never sees half a file.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import unicodedata
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from .errors import StoreError
from .uri import _check_segment

META = ".provcat"


def default_root() -> Path:
    env = os.environ.get("PROVCAT_CACHE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "provcat"


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def find_twins(paths: Iterable[tuple[str, ...]]) -> tuple[str, str] | None:
    """Return two paths that one disk could store as one, or None.

    Every prefix is compared, so ``Dir/a`` and ``dir/b`` are twins too.
    """
    seen: dict[tuple[str, ...], tuple[str, ...]] = {}
    for path in paths:
        for n in range(1, len(path) + 1):
            prefix = path[:n]
            folded = tuple(_fold(p) for p in prefix)
            other = seen.setdefault(folded, prefix)
            if other != prefix:
                return "/".join(other), "/".join(prefix)
    return None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _digest(store: str, key: Iterable[str]) -> str:
    return hashlib.sha256("\0".join([store, *key]).encode()).hexdigest()


class LocalCache:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else default_root()

    def path(self, store: str, key: Iterable[str]) -> Path:
        return self.root.joinpath(_check_segment(store), *[_check_segment(k) for k in key])

    def _record(self, kind: str, store: str, key: Iterable[str]) -> Path:
        return self.root / META / kind / f"{_digest(store, key)}.json"

    def _tmpdir(self) -> Path:
        d = self.root / META / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_atomic(self, dest: Path, fill: Callable[[Path], None],
                      before_replace: Callable[[Path], None] | None = None) -> None:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._tmpdir(), prefix="f-")
            os.close(fd)
        except OSError as e:
            raise StoreError(f"cannot write {dest} in the cache: {e}") from e
        try:
            fill(Path(tmp))
            if before_replace is not None:
                before_replace(Path(tmp))
            os.replace(tmp, dest)
        except OSError as e:
            Path(tmp).unlink(missing_ok=True)
            raise StoreError(f"cannot write {dest} in the cache: {e}") from e
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _read_json(p: Path) -> dict | None:
        try:
            data = json.loads(p.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _write_json(self, p: Path, data: dict) -> None:
        self._write_atomic(p, lambda t: t.write_text(json.dumps(data)))

    @staticmethod
    def _fingerprint(p: Path) -> dict:
        st = p.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha256(p)}

    def _unchanged(self, p: Path, record: Path, meta: dict) -> bool:
        """True if p still holds the bytes that meta describes."""
        want = meta.get("file") or {}
        st = p.stat()
        if st.st_size != want.get("size"):
            return False
        if st.st_mtime_ns == want.get("mtime_ns"):
            return True
        if _sha256(p) != want.get("sha256"):
            return False
        try:  # the bytes match; remember the new time so the hash is not needed next time
            self._write_json(record, {**meta, "file": {**want, "mtime_ns": st.st_mtime_ns}})
        except StoreError:
            pass  # a read-only cache
        return True

    # files

    def get(self, store: str, key: list[str], version: str | None) -> Path | None:
        """Return the cached file if it is there, unchanged, and has this version.

        A version of None accepts any cached version (offline mode).
        """
        p = self.path(store, key)
        record = self._record("meta", store, key)
        meta = self._read_json(record)
        try:
            if meta is None or not p.is_file() or not self._unchanged(p, record, meta):
                return None
        except OSError:
            return None
        if version is not None and meta.get("version") != version:
            return None
        return p

    def put(self, store: str, key: list[str], fill: Callable[[Path], str | None], version: str | None = None) -> Path:
        """Write a file with fill(tmp_path), then record its version.

        fill may return the version of what it wrote. That value wins over the
        version argument, so the record always describes the bytes on disk.
        """
        p = self.path(store, key)
        got: dict = {}

        def run(tmp: Path) -> None:
            got["version"] = fill(tmp) or version
            if got["version"] is None:
                raise StoreError(f"no version for {p}")

        def measure(tmp: Path) -> None:
            # Measure the temporary file, not p: another writer may replace p at any time.
            # A rename keeps the modification time, so the measure describes p after the rename.
            got["file"] = self._fingerprint(tmp)

        self._record("meta", store, key).unlink(missing_ok=True)
        self._write_atomic(p, run, measure)
        self._write_json(self._record("meta", store, key),
                         {"store": store, "key": key, "version": got["version"], "file": got["file"]})
        return p

    # whole folders

    def folder_done(self, store: str, key: list[str], version: str | None) -> Path | None:
        p = self.path(store, key)
        done = self._read_json(self._record("done", store, key))
        if not p.is_dir() or done is None:
            return None
        if version is not None and done.get("version") != version:
            return None
        return p

    def covered(self, store: str, key: list[str], version: str, min_len: int) -> Path | None:
        """Return the cached file or folder if a folder at or above key was fetched whole."""
        p = self.path(store, key)
        if not p.exists():
            return None
        for n in range(len(key), min_len - 1, -1):
            if self.folder_done(store, key[:n], version):
                return p
        return None

    def mark_folder(self, store: str, key: list[str], version: str) -> None:
        self._write_json(self._record("done", store, key), {"store": store, "key": key, "version": version})

    def unmark_folder(self, store: str, key: list[str]) -> None:
        self._record("done", store, key).unlink(missing_ok=True)

    def prune(self, store: str, key: list[str], keep: set[tuple[str, ...]]) -> None:
        """Delete cached files under key whose relative keys are not in keep."""
        base = self.path(store, key)
        if not base.is_dir():
            return
        for f in sorted(base.rglob("*"), reverse=True):
            rel = f.relative_to(base).parts
            if f.is_file() and rel not in keep:
                f.unlink()
                self._record("meta", store, key + list(rel)).unlink(missing_ok=True)
            elif f.is_dir() and not any(f.iterdir()):
                f.rmdir()

    @contextlib.contextmanager
    def lock(self, store: str, key: list[str]):
        """Hold an exclusive lock for one key, across threads and processes."""
        lock = self.root / META / "locks" / f"{_digest(store, key)}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def install_folder(self, store: str, key: list[str], version: str, fill: Callable[[Path], None]) -> Path:
        """Build a folder with fill(tmp_dir), then move it into place whole.

        Installs of one key run one at a time. If another thread or process
        installed the same version first, its folder stays.
        """
        try:
            with self.lock(store, key):
                if self.folder_done(store, key, version):
                    return self.path(store, key)
                return self._install(store, key, version, fill)
        except OSError as e:
            raise StoreError(f"cannot lock {self.path(store, key)} in the cache: {e}") from e

    def _install(self, store: str, key: list[str], version: str, fill: Callable[[Path], None]) -> Path:
        dest = self.path(store, key)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=self._tmpdir(), prefix="d-"))
        except OSError as e:
            raise StoreError(f"cannot write {dest} in the cache: {e}") from e
        try:
            fill(tmp)
            if self.folder_done(store, key, version):
                return dest
            self.unmark_folder(store, key)
            if dest.exists():
                old = Path(tempfile.mkdtemp(dir=self._tmpdir(), prefix="old-"))
                os.replace(dest, old / "x")
                shutil.rmtree(old, ignore_errors=True)
            try:
                os.replace(tmp, dest)
            except OSError as e:
                if e.errno in (errno.ENOTEMPTY, errno.EEXIST) and self.folder_done(store, key, version):
                    return dest  # another process won the race
                raise
        except OSError as e:
            raise StoreError(f"cannot write {dest} in the cache: {e}") from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.mark_folder(store, key, version)
        return dest

    # refs

    def get_ref(self, store: str, name: list[str]) -> str | None:
        data = self._read_json(self._record("refs", store, name))
        return data.get("value") if data else None

    def set_ref(self, store: str, name: list[str], value: str) -> None:
        self._write_json(self._record("refs", store, name), {"store": store, "name": name, "value": value})
