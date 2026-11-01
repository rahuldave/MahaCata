"""Stores: where the evidence of a run is kept.

A store opens a run and gives a ``RunWriter``. provrun's recorder hands the
writer each event line, stores each large payload through it, and seals it
with the manifest when the run ends. The ``file`` store keeps one folder for
each run::

    <evidence>/events.jsonl            one event on each line, in order
    <evidence>/payloads/<sha256>       large payloads, named by their SHA-256
    <evidence>/manifest.json           written when the run is sealed
"""
from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from .events import sha256_bytes


class Sink(ABC):
    """A place that evidence lines go to."""

    @abstractmethod
    def append(self, line: str) -> None: ...

    def close(self) -> None:
        pass


class JsonlSink(Sink):
    """Appends each line to a local file, with no buffer in between: a line
    that fails to write is never written later, and a writer that takes a
    line back can cut it from the file. The file is made with mode 0o666 less
    the umask, as open() makes it."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)

    def append(self, line: str) -> None:
        """Write the line whole, or not at all: a short write followed by an
        error is cut back, so a lost line never joins the next one."""
        if self._fd is None:
            raise ValueError(f"{self.path} is closed")
        data = (line + "\n").encode("utf-8")
        size = os.fstat(self._fd).st_size
        try:
            while data:
                data = data[os.write(self._fd, data):]
        except BaseException:
            os.ftruncate(self._fd, size)
            raise

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class PayloadStore:
    """Large payloads, each in a file named by its SHA-256, written once."""

    def __init__(self, root: Path):
        self.root = root

    def put(self, data: bytes | str) -> str:
        raw = data.encode() if isinstance(data, str) else data
        digest = sha256_bytes(raw)
        dest = self.root / digest
        if not dest.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return digest

    def get(self, digest: str) -> bytes:
        return (self.root / digest).read_bytes()

    def names(self) -> list[str]:
        """The digests stored, in order."""
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.glob("*") if not p.name.startswith("."))


class RunWriter(Sink):
    """The evidence of one open run. It is the recorder's first sink: an
    event counts as written only when the writer holds its line."""

    @abstractmethod
    def put_payload(self, data: bytes | str) -> str:
        """Store data once, and return its SHA-256."""

    @abstractmethod
    def get_payload(self, digest: str) -> bytes: ...

    @abstractmethod
    def payload_names(self) -> list[str]: ...

    @abstractmethod
    def seal(self, manifest: dict) -> None:
        """Record the manifest. Called once, after close."""

    @abstractmethod
    def manifest(self) -> dict:
        """The manifest recorded by seal."""

    def record_bundle(self, uri: str | None, pinned: str | None) -> None:
        """Note where the run's bundle was committed, after the seal. A store
        that keeps no record of runs ignores it."""


class RunRecord(ABC):
    """A sealed or open run as a store holds it, read back for the checks."""

    @abstractmethod
    def lines(self) -> list[str]:
        """The event lines, in order. Raises EvidenceError if they cannot be read."""

    @abstractmethod
    def manifest_bytes(self) -> bytes | None:
        """The manifest as stored, or None if the run has none. May raise OSError."""

    @abstractmethod
    def payload_digest(self, name: str) -> str | None:
        """The SHA-256 of the stored payload called name, or None if there is
        none. May raise OSError."""

    @abstractmethod
    def payload_files(self) -> tuple[set[str], list[str]]:
        """The names of the stored payloads, and a problem for each entry that
        is not a payload, such as a special file."""

    def problems(self) -> list[str]:
        """Problems of the store itself that the chain does not show."""
        return []


class FolderRecord(RunRecord):
    """A run's evidence folder, as the ``file`` store writes it."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def lines(self) -> list[str]:
        from .checks import _lines

        return _lines(self.folder)

    def manifest_bytes(self) -> bytes | None:
        path = self.folder / "manifest.json"
        return path.read_bytes() if path.exists() else None

    def payload_digest(self, name: str) -> str | None:
        p = self.folder / "payloads" / name
        if not p.exists():
            return None
        return sha256_bytes(p.read_bytes())

    def payload_files(self) -> tuple[set[str], list[str]]:
        from .checks import _files

        present, odd = _files(self.folder / "payloads")
        # The store leaves out hidden names, such as its own temporary files and
        # a .DS_Store that macOS writes, so the check leaves them out too.
        return {rel for rel in present if not rel.startswith(".")}, odd


class EvidenceStore(ABC):
    """Where runs keep their evidence. One store can hold many runs."""

    @abstractmethod
    def open_run(self, run_id: str, folder: Path) -> RunWriter:
        """Open the evidence of a new run. folder is the run's evidence folder,
        which holds the bundle files that provrun commits through provcat."""

    # A store that only writes need not read back; the checks then read the
    # files in the run folder.

    def read_run(self, run_id: str, folder: Path | None = None) -> RunRecord:
        """The evidence of a run, read back. folder, if given, is the run's
        evidence folder, for a store that keeps payloads there. Raises
        RunNotFound, or CannotReadBack for a store that only writes."""
        from .errors import CannotReadBack

        raise CannotReadBack(f"{self!r} cannot read a run back")

    def list_runs(self) -> list[str]:
        """The run IDs in the store, oldest first."""
        from .errors import CannotReadBack

        raise CannotReadBack(f"{self!r} cannot list its runs")

    def run_folder(self, run_id: str) -> Path | None:
        """The run's folder, with its outputs, or None if the store does not know it."""
        return None


class FileRunWriter(RunWriter):
    def __init__(self, folder: Path):
        self.folder = folder
        self.events = JsonlSink(folder / "events.jsonl")
        self.payloads = PayloadStore(folder / "payloads")

    def append(self, line: str) -> None:
        self.events.append(line)

    def close(self) -> None:
        self.events.close()

    def put_payload(self, data: bytes | str) -> str:
        return self.payloads.put(data)

    def get_payload(self, digest: str) -> bytes:
        return self.payloads.get(digest)

    def payload_names(self) -> list[str]:
        return self.payloads.names()

    def seal(self, manifest: dict) -> None:
        (self.folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def manifest(self) -> dict:
        return json.loads((self.folder / "manifest.json").read_text())


class FileStore(EvidenceStore):
    """The ``file`` store: each run's evidence in files in its own folder.
    root is the runs root that read_run and list_runs look in; None is the
    default runs root."""

    def __init__(self, root: str | Path | None = None):
        self.root = root

    def __repr__(self) -> str:
        return f"FileStore({self.root!r})"

    def open_run(self, run_id: str, folder: Path) -> RunWriter:
        return FileRunWriter(folder)

    def read_run(self, run_id: str, folder: Path | None = None) -> RunRecord:
        from .runs import find_run

        return FolderRecord(folder if folder is not None else find_run(run_id, self.root, ids_only=True) / "evidence")

    def list_runs(self) -> list[str]:
        from .runs import list_runs

        return list_runs(self.root)

    def run_folder(self, run_id: str) -> Path | None:
        from .errors import RunNotFound
        from .runs import find_run

        try:
            return find_run(run_id, self.root, ids_only=True)
        except RunNotFound:
            return None


def check_store_uri(uri: str) -> None:
    """Raise ProveviError unless uri names a store: ``file``, or
    ``sqlite:///path``. Opens nothing."""
    from .errors import ProveviError

    if uri in ("file", "file:"):
        return
    if uri.startswith("sqlite:"):
        if not uri.startswith("sqlite:///") or uri == "sqlite:///" or uri == "sqlite:////":
            raise ProveviError(f"{uri!r} is not a sqlite URI; write sqlite:///relative.db or "
                               "sqlite:////absolute.db")
        if "?" in uri or "#" in uri:
            raise ProveviError(f"{uri!r} has a query or a fragment; a sqlite store URI takes only a path")
        return
    raise ProveviError(f"{uri!r} names no store; use file or sqlite:///path")


def open_store(uri: str | None, root: str | Path | None = None, *, create: bool = True) -> EvidenceStore:
    """The store a URI names: ``file`` (or None) for the file store under
    root, or a SQLite file. ``sqlite:///evidence.db`` is relative to the
    current folder when the store opens, and ``sqlite:////var/evidence.db``
    is absolute, as in SQLAlchemy and MLflow; ``~`` is the home folder. With
    create=False, a SQLite store only reads, and must exist. Raises
    ProveviError."""
    if uri is None:
        return FileStore(root)
    check_store_uri(uri)
    if uri.startswith("sqlite:"):
        from .sqlite import SqliteStore

        return SqliteStore(uri.removeprefix("sqlite:///"), create=create)
    return FileStore(root)
