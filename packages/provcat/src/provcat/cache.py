"""The local cache.

The cache keeps a readable copy of remote objects, so a folder of it can be
mounted into a container. The layout is::

    <root>/<store>/<key...>                the cached files
    <root>/.provcat/meta/<hash>.json       the version of one cached file
    <root>/.provcat/done/<hash>.json       a folder that was fetched whole
    <root>/.provcat/refs/<hash>            a moving name pinned to a version
    <root>/.provcat/tmp/                   files and folders being written
    <root>/.provcat/locks/<hash>.lock      one lock for each key being written
    <root>/.provcat/snapshots/<kind>/<sha>/<name>  a read-only copy, named by its content

A store decides the key. GitHub keys hold the commit, so they never change.
S3 keys are bucket and object key, and the meta file holds the ETag.

The records under .provcat are named by a hash of store and key, so two keys
can never share or block a record. A meta record also holds the size, the
modification time, the inode and the SHA-256 of the file it describes. If the
time or the inode differs, as after the cache was copied, the file is hashed
again and kept if the hash matches. On Linux a time's tick can be a few
milliseconds, so the inode tells apart two files of one size from one tick.
Writes of one key take its lock, so the last record describes the last file,
and a read that saw the record change while it looked returns nothing. If the
size differs, or the bytes differ from the hash, the file is fetched again. A
rewrite in place that keeps the size, the time and the inode, as
``rsync --inplace -t`` makes, is not noticed. The locks need a filesystem with
``flock``, such as a local disk. On a share without it, every write fails.

Two names that differ only in case or in Unicode form land in one file on macOS
and Windows. The stores refuse a folder that holds such names.

Every write goes to a temporary name under .provcat/tmp and then is renamed, so
a reader never sees half a file.

A fetch can rewrite a cached file in place when a branch moves, so a cached
path is not safe to mount while something reads it. A snapshot, made by
``Catalog.snapshot``, copies a file or a folder into a folder named by the
SHA-256 of its content, and makes the copy read-only. Nothing in provcat writes to a snapshot again, so a mount of
one never changes.
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
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from .errors import StoreError
from .uri import _check_segment

__all__ = ["LocalCache", "tree_digest"]

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


def _bytes_of(meta: dict | None) -> tuple | None:
    """The parts of a meta record that name the bytes: its version, size and
    hash. A rewrite that only notes a new time or inode keeps them."""
    if meta is None:
        return None
    f = meta.get("file") or {}
    return meta.get("version"), f.get("size"), f.get("sha256")


class LocalCache:
    """The local cache of fetched files and folders, their versions, the pinned refs and the snapshots.

    A store driver calls it to find and to write what it fetches. Each key is a
    list of path segments under the store's name."""

    def __init__(
        self,
        root: str | Path | None = None,  # The cache folder; None takes PROVCAT_CACHE, then XDG_CACHE_HOME/provcat, then ~/.cache/provcat
    ):
        self.root = Path(root) if root else default_root()

    def path(
        self,
        store: str,  # The store's name
        key: Iterable[str],  # The path segments under the store
    ) -> Path:  # Where the file or folder of the key lives in the cache; UriError for a bad segment
        """Return the cache path of a key, whether or not anything is there."""
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
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "sha256": _sha256(p)}

    def _unchanged(self, p: Path, store: str, key: list[str], meta: dict) -> bool:
        """True if p still holds the bytes that meta describes."""
        want = meta.get("file") or {}
        st = p.stat()
        if st.st_size != want.get("size"):
            return False
        # A time's tick can be coarse, as on Linux, so two writers' files can share
        # a size and a time. Each write renames a new file into place, so the
        # inode tells them apart.
        if st.st_mtime_ns == want.get("mtime_ns") and st.st_ino == want.get("ino"):
            return True
        if _sha256(p) != want.get("sha256"):
            return False
        record = self._record("meta", store, key)
        try:  # the bytes match; remember the new time so the hash is not needed next time
            with self.lock(store, key):
                if self._read_json(record) == meta:  # no put wrote a newer record meanwhile
                    self._write_json(record, {**meta, "file": {**want, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}})
        except (OSError, StoreError):
            pass  # a read-only cache
        return True

    # files

    def get(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments under the store
        version: str | None,  # The version wanted; None accepts any cached version, as offline mode does
    ) -> Path | None:  # The cached file; None when it is missing, changed, or of another version
        """Return the cached file if it is there, unchanged, and has this version."""
        p = self.path(store, key)
        record = self._record("meta", store, key)
        meta = self._read_json(record)
        try:
            if meta is None or not p.is_file() or not self._unchanged(p, store, key, meta):
                return None
        except OSError:
            return None
        if version is not None and meta.get("version") != version:
            return None
        if _bytes_of(self._read_json(record)) != _bytes_of(meta):  # a put finished meanwhile
            return None
        return p

    def put(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments under the store
        fill: Callable[[Path], str | None],  # Writes the file at the temporary path it is given, and may return its version
        version: str | None = None,  # The version to record, when fill returns none
    ) -> Path:  # The cached file
        """Write a file with fill, then record its version.

        The version that fill returns wins over the version argument, so the
        record always describes the bytes on disk. With neither, StoreError.
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

        # One writer of a key at a time, so the last record describes the last file.
        # Without the lock, a third writer's file could take the inode number of
        # the first writer's freed file, and the first writer's late record vouch for it.
        try:
            with self.lock(store, key):
                self._record("meta", store, key).unlink(missing_ok=True)
                self._write_atomic(p, run, measure)
                self._write_json(self._record("meta", store, key),
                                 {"store": store, "key": key, "version": got["version"], "file": got["file"]})
        except OSError as e:
            raise StoreError(f"cannot write {p} in the cache: {e}") from e
        return p

    # whole folders

    def folder_done(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the folder
        version: str | None,  # The version wanted; None accepts any
    ) -> Path | None:  # The cached folder when it was fetched whole at that version; else None
        """Find a folder that was fetched whole."""
        p = self.path(store, key)
        done = self._read_json(self._record("done", store, key))
        if not p.is_dir() or done is None:
            return None
        if version is not None and done.get("version") != version:
            return None
        return p

    def covered(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the file or folder
        version: str,  # The version the whole folder must have been fetched at
        min_len: int,  # The shortest key prefix to look at, such as 1 to stop at the bucket
    ) -> Path | None:  # The cached file or folder; None when no such folder covers it
        """Return the cached file or folder if a folder at or above key was fetched whole."""
        p = self.path(store, key)
        if not p.exists():
            return None
        for n in range(len(key), min_len - 1, -1):
            if self.folder_done(store, key[:n], version):
                return p
        return None

    def mark_folder(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the folder
        version: str,  # The version it was fetched at
    ) -> None:  # Writes the record under .provcat/done
        """Record that a folder was fetched whole at a version."""
        self._write_json(self._record("done", store, key), {"store": store, "key": key, "version": version})

    def unmark_folder(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the folder
    ) -> None:  # Deletes the record, if there is one
        """Forget that a folder was fetched whole."""
        self._record("done", store, key).unlink(missing_ok=True)

    def prune(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the folder
        keep: set[tuple[str, ...]],  # The paths under the folder, as tuples of segments, to keep
    ) -> None:  # Deletes those files, their records, and the folders left empty
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
    def lock(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments to lock
    ) -> Iterator[None]:  # A context manager that holds the lock while its block runs
        """Hold an exclusive lock for one key, across threads and processes."""
        lock = self.root / META / "locks" / f"{_digest(store, key)}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def install_folder(
        self,
        store: str,  # The store's name
        key: list[str],  # The path segments of the folder
        version: str,  # The version of the folder, recorded once it is in place
        fill: Callable[[Path], None],  # Writes the folder's contents into the temporary folder it is given
    ) -> Path:  # The cached folder
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

    def get_ref(
        self,
        store: str,  # The store's name
        name: list[str],  # The moving name, as segments, such as a repository, a branch and a time
    ) -> str | None:  # The version it was pinned to; None when it was never pinned
        """Read the version that a moving name was last pinned to, as offline mode does."""
        data = self._read_json(self._record("refs", store, name))
        return data.get("value") if data else None

    def set_ref(
        self,
        store: str,  # The store's name
        name: list[str],  # The moving name, as segments
        value: str,  # The fixed version it points at now
    ) -> None:  # Writes the record under .provcat/refs
        """Record the version that a moving name points at."""
        self._write_json(self._record("refs", store, name), {"store": store, "name": name, "value": value})


def tree_digest(
    p: Path,  # A file or a folder
) -> str:  # The hex SHA-256; StoreError for a link or a special file
    """The SHA-256 of a file, or of a folder.

    A folder's digest covers every entry under it, folders included, so an
    empty folder counts. Each entry is ``F\0path\0sha256\n`` for a file or
    ``D\0path\0\n`` for a folder, in sorted order, after the line
    ``provcat-tree-v1``. A file name holds no NUL, so the form is unambiguous.
    A file and a folder can still have equal digests, because a file can hold
    any bytes; snapshots keep them apart by kind. A link or any other special
    file is refused, so the digest always describes plain bytes."""
    if p.is_symlink():
        raise StoreError(f"{p} is a symbolic link; a snapshot holds plain files only")
    if p.is_file():
        return _sha256(p)
    if not p.is_dir():
        raise StoreError(f"{p} is not a file or a folder")
    h = hashlib.sha256(b"provcat-tree-v1\n")
    for f in sorted(p.rglob("*"), key=lambda q: q.relative_to(p).as_posix()):
        rel = f.relative_to(p).as_posix()
        if f.is_symlink() or not (f.is_file() or f.is_dir()):
            raise StoreError(f"{f} is not a plain file or folder; a snapshot holds plain files only")
        if f.is_dir():
            h.update(f"D\0{rel}\0\n".encode())
        else:
            h.update(f"F\0{rel}\0{_sha256(f)}\n".encode())
    return h.hexdigest()


def kind_of(p: Path) -> str:
    return "folder" if p.is_dir() and not p.is_symlink() else "file"


def _clone(src: Path, dest: Path) -> None:
    """Copy src to dest, with a copy-on-write clone where the disk has one."""
    flags = {"darwin": ["-c"], "linux": ["--reflink=auto"]}.get(sys.platform)
    if flags is not None:
        r = subprocess.run(["cp", *flags, "-R", str(src), str(dest)], capture_output=True)
        if r.returncode == 0:
            return
        shutil.rmtree(dest, ignore_errors=True) if dest.is_dir() else dest.unlink(missing_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copyfile(src, dest)


def _freeze(p: Path, *, top: bool = True) -> None:
    """Take away every write permission, files first and the folders last.
    With top=False the folder p itself stays writable, because a folder can be
    renamed only while it is writable."""
    if p.is_dir():
        for f in sorted(p.rglob("*"), key=lambda q: len(q.parts), reverse=True):
            f.chmod(0o555 if f.is_dir() else 0o444)
        if top:
            p.chmod(0o555)
    else:
        p.chmod(0o444)


def _thaw(p: Path) -> None:
    if p.is_dir():
        p.chmod(0o755)
        for f in p.rglob("*"):
            f.chmod(0o755 if f.is_dir() else 0o644)
    elif p.exists():
        p.chmod(0o644)


def copy_readonly(src: Path, dest: Path) -> None:
    """Copy a file or a folder to dest, with a copy-on-write clone where the
    disk has one, and take away every write permission of the copy. A clone
    shares no inode with src, so a change to one never reaches the other."""
    _clone(src, dest)
    _freeze(dest)


def _remove(p: Path) -> None:
    _thaw(p)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()


def snapshot(cache: LocalCache, src: Path, name: str) -> tuple[Path, str]:
    """Copy src into the snapshots of the cache as name, make the copy
    read-only, and return its path and its digest.

    A snapshot lives at ``.provcat/snapshots/<kind>/<digest>/<name>``, where
    the kind is file or folder, so a file never stands for a folder with the
    same digest. The digest is taken from the copy, so it describes exactly the
    bytes that the snapshot holds, even if src changes during the copy. Two
    snapshots with the same kind, content and name share one folder. Before a
    snapshot is used again, its digest is checked; a snapshot that someone
    changed is replaced. One lock for each digest keeps two processes apart.
    """
    _check_segment(name)
    try:
        tmp = Path(tempfile.mkdtemp(dir=cache._tmpdir(), prefix="s-"))
    except OSError as e:
        raise StoreError(f"cannot write a snapshot in the cache: {e}") from e
    try:
        _clone(src, tmp / name)
        kind = kind_of(tmp / name)
        digest = tree_digest(tmp / name)
        dest = cache.root / META / "snapshots" / kind / digest / name
        with cache.lock("snapshots", [kind, digest, name]):
            if dest.exists() or dest.is_symlink():
                if kind_of(dest) == kind and not dest.is_symlink() and tree_digest_or_none(dest) == digest:
                    return dest, digest
                _remove(dest)  # changed since it was made; replace it
            _freeze(tmp / name, top=False)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(tmp / name, dest)
            if dest.is_dir():
                dest.chmod(0o555)
            return dest, digest
    except OSError as e:
        raise StoreError(f"cannot write a snapshot of {src} in the cache: {e}") from e
    finally:
        if (tmp / name).exists():
            _thaw(tmp / name)
        shutil.rmtree(tmp, ignore_errors=True)


def tree_digest_or_none(p: Path) -> str | None:
    try:
        return tree_digest(p)
    except (StoreError, OSError):
        return None
