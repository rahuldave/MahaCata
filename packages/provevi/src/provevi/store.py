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

__all__ = [
    "EvidenceStore", "FileRunWriter", "FileStore", "FolderRecord", "JsonlSink", "PayloadStore", "RunRecord",
    "RunWriter", "Sink", "check_store_uri", "open_store",
]


class Sink(ABC):
    """A place that evidence lines go to."""

    @abstractmethod
    def append(
        self,
        line: str,  # One event line, with no newline
    ) -> None:  # Holds the line, or raises
        """Take one event line."""

    def close(self) -> None:  # Nothing in the base sink
        """Finish: no line comes after. The base sink does nothing."""


class JsonlSink(Sink):
    """Appends each line to a local file, with no buffer in between.

    A line that fails to write is never written later, and a writer that takes a line back can cut it from the
    file. The file is made with mode 0o666 less the umask, as open() makes it."""

    def __init__(
        self,
        path: Path,  # The file to append to; it and its folder are made if missing
    ):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)

    def append(
        self,
        line: str,  # One event line; the sink adds the newline
    ) -> None:  # Appends the line; ValueError once the sink is closed
        """Write the line whole, or not at all.

        A short write followed by an error is cut back, so a lost line never joins the next one."""
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

    def close(self) -> None:  # Flushes and closes the file
        """Flush the file to disk and close it. A second close does nothing."""
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


class PayloadStore:
    """Large payloads, each in a file named by its SHA-256, written once."""

    def __init__(
        self,
        root: Path,  # The folder of payloads, made at the first put
    ):
        self.root = root

    def put(
        self,
        data: bytes | str,  # The payload; a text is stored as UTF-8
    ) -> str:  # The payload's SHA-256, which is its file's name
        """Store a payload once: a payload already stored is not written again."""
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

    def get(
        self,
        digest: str,  # The payload's SHA-256
    ) -> bytes:  # The payload; OSError when it is not stored
        """Read a stored payload."""
        return (self.root / digest).read_bytes()

    def names(self) -> list[str]:  # The SHA-256 of each payload, sorted; hidden temporary files are left out
        """List the payloads stored."""
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.glob("*") if not p.name.startswith("."))


class RunWriter(Sink):
    """The evidence of one open run.

    It is the recorder's first sink: an event counts as written only when the writer holds its line."""

    @abstractmethod
    def put_payload(
        self,
        data: bytes | str,  # The payload; a text is stored as UTF-8
    ) -> str:  # The payload's SHA-256, which an event's `payload` holds
        """Store a payload of the run once."""

    @abstractmethod
    def get_payload(
        self,
        digest: str,  # The payload's SHA-256
    ) -> bytes:  # The payload
        """Read back a payload of the run."""

    @abstractmethod
    def payload_names(self) -> list[str]:  # The SHA-256 of each payload stored, sorted
        """The payloads of the run, for the manifest."""

    @abstractmethod
    def seal(
        self,
        manifest: dict,  # The manifest: `run_id`, `events` (the count), `last` (the hash of the last line), `payloads`, `complete` and `problems`
    ) -> None:  # Records the manifest, or raises
        """Record the manifest. Called once, after close."""

    @abstractmethod
    def manifest(self) -> dict:  # The manifest as seal recorded it
        """The manifest recorded by seal."""

    def record_bundle(
        self,
        uri: str | None,  # The URI the bundle was committed at; `None` when it was not committed
        pinned: str | None,  # The URI pinned to the commit; `None` when it was not committed
    ) -> None:  # Records them; nothing in the base class
        """Note where the run's bundle was committed, after the seal.

        A store that keeps no record of runs ignores it."""


class RunRecord(ABC):
    """A sealed or open run as a store holds it, read back for the checks."""

    @abstractmethod
    def lines(self) -> list[str]:  # Each line with no newline
        """The event lines, in order. Raises EvidenceError if they cannot be read."""

    @abstractmethod
    def manifest_bytes(self) -> bytes | None:  # The bytes of the manifest; `None` when the run has none
        """Read the manifest as stored. May raise OSError."""

    @abstractmethod
    def payload_digest(
        self,
        name: str,  # The payload's name: the SHA-256 that an event names it by
    ) -> str | None:  # The SHA-256 of its stored bytes; `None` when there is none
        """Hash the stored payload called name.

        May raise OSError."""

    @abstractmethod
    def payload_files(self) -> tuple[set[str], list[str]]:  # The names, and the problems
        """List the stored payloads, and the entries that are not payloads.

        Each entry that is not a payload, such as a special file, gives a problem."""

    def payload_bytes(
        self,
        name: str,  # The payload's name: its SHA-256
    ) -> bytes | None:  # Its bytes; `None` when there is none, and always in the base class
        """Read the stored payload called name."""
        return None

    def problems(self) -> list[str]:  # One line each; empty in the base class
        """Problems of the store itself that the chain does not show."""
        return []


class FolderRecord(RunRecord):
    """A run's evidence folder, as the ``file`` store writes it."""

    def __init__(
        self,
        folder: Path,  # The run's evidence folder
    ):
        self.folder = Path(folder)

    def lines(self) -> list[str]:  # Each line of `events.jsonl`, with no newline
        """The event lines, in order. Raises EvidenceError if events.jsonl cannot be read."""
        from .checks import _lines

        return _lines(self.folder)

    def manifest_bytes(self) -> bytes | None:  # The bytes of `manifest.json`; `None` when it is missing
        """The manifest as the folder holds it. May raise OSError."""
        path = self.folder / "manifest.json"
        return path.read_bytes() if path.exists() else None

    def payload_digest(
        self,
        name: str,  # The payload's name under `payloads/`: its SHA-256
    ) -> str | None:  # The SHA-256 of the file's bytes; `None` when it is missing
        """Hash the payload file called name. May raise OSError."""
        p = self.folder / "payloads" / name
        if not p.exists():
            return None
        return sha256_bytes(p.read_bytes())

    def payload_bytes(
        self,
        name: str,  # The payload's name under `payloads/`: its SHA-256
    ) -> bytes | None:  # The file's bytes; `None` when it is not a regular file
        """Read the payload file called name."""
        p = self.folder / "payloads" / name
        return p.read_bytes() if p.is_file() else None

    def payload_files(self) -> tuple[set[str], list[str]]:  # The paths under `payloads/`, and the problems
        """List the files under payloads/, and the entries that are not files.

        Hidden names are left out. Each entry that is not a regular file or a link, and each folder that
        cannot be read, gives a problem."""
        from .checks import _files

        present, odd = _files(self.folder / "payloads")
        # The store leaves out hidden names, such as its own temporary files and
        # a .DS_Store that macOS writes, so the check leaves them out too.
        return {rel for rel in present if not rel.startswith(".")}, odd


class EvidenceStore(ABC):
    """Where runs keep their evidence. One store can hold many runs."""

    @abstractmethod
    def open_run(
        self,
        run_id: str,  # The new run's ID
        folder: Path,  # The run's evidence folder, which holds the bundle files that provrun commits through provcat
    ) -> RunWriter:  # The writer of the run's evidence
        """Open the evidence of a new run."""

    # A store that only writes need not read back; the checks then read the
    # files in the run folder.

    def read_run(
        self,
        run_id: str,  # The run's ID
        folder: Path | None = None,  # The run's evidence folder, for a store that keeps payloads there
    ) -> RunRecord:  # The run as the store holds it
        """Read the evidence of a run back.

        Raises RunNotFound, or CannotReadBack for a store that only writes, as the base class does."""
        from .errors import CannotReadBack

        raise CannotReadBack(f"{self!r} cannot read a run back")

    def list_runs(self) -> list[str]:  # The run IDs
        """List the run IDs in the store, oldest first.

        The base class raises CannotReadBack."""
        from .errors import CannotReadBack

        raise CannotReadBack(f"{self!r} cannot list its runs")

    def run_folder(
        self,
        run_id: str,  # The run's ID
    ) -> Path | None:  # The run folder; `None` when the store does not know it, and always in the base class
        """Find the run folder, which holds the run's outputs."""
        return None


class FileRunWriter(RunWriter):
    """The ``file`` store's writer: events.jsonl, payloads/ and manifest.json in the run's evidence folder."""

    def __init__(
        self,
        folder: Path,  # The run's evidence folder; made if missing
    ):
        self.folder = folder
        self.events = JsonlSink(folder / "events.jsonl")
        self.payloads = PayloadStore(folder / "payloads")

    def append(
        self,
        line: str,  # One event line, with no newline
    ) -> None:  # Appends the line; ValueError once the writer is closed
        """Append the line to events.jsonl, whole or not at all."""
        self.events.append(line)

    def close(self) -> None:  # Flushes and closes events.jsonl
        """Flush events.jsonl to disk and close it."""
        self.events.close()

    def put_payload(
        self,
        data: bytes | str,  # The payload; a text is stored as UTF-8
    ) -> str:  # The payload's SHA-256, its file's name under `payloads/`
        """Store a payload once under payloads/."""
        return self.payloads.put(data)

    def get_payload(
        self,
        digest: str,  # The payload's SHA-256
    ) -> bytes:  # The payload; OSError when it is not stored
        """Read a payload from payloads/."""
        return self.payloads.get(digest)

    def payload_names(self) -> list[str]:  # The SHA-256 of each payload, sorted
        """The payloads under payloads/."""
        return self.payloads.names()

    def seal(
        self,
        manifest: dict,  # The manifest the recorder made
    ) -> None:  # Writes manifest.json, as indented JSON
        """Write the manifest to manifest.json."""
        (self.folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def manifest(self) -> dict:  # The manifest that manifest.json holds
        """Read manifest.json back."""
        return json.loads((self.folder / "manifest.json").read_text())


class FileStore(EvidenceStore):
    """The ``file`` store: each run's evidence in files in its own folder."""

    def __init__(
        self,
        root: str | Path | None = None,  # The runs root that read_run, list_runs and run_folder look in; `None` for the default runs root
    ):
        self.root = root

    def __repr__(self) -> str:
        return f"FileStore({self.root!r})"

    def open_run(
        self,
        run_id: str,  # The new run's ID; the file store does not use it
        folder: Path,  # The run's evidence folder, where the files go
    ) -> RunWriter:  # A `FileRunWriter` on the folder
        """Open the evidence of a new run in its folder."""
        return FileRunWriter(folder)

    def read_run(
        self,
        run_id: str,  # The run's ID, a folder under the runs root; not used when folder is given
        folder: Path | None = None,  # The run's evidence folder; `None` to find it under the runs root
    ) -> RunRecord:  # A `FolderRecord` on the evidence folder
        """Read a run's evidence folder back.

        Without folder, it raises RunNotFound when there is no run with that ID under the runs root."""
        from .runs import find_run

        return FolderRecord(folder if folder is not None else find_run(run_id, self.root, ids_only=True) / "evidence")

    def list_runs(self) -> list[str]:  # The run IDs under the runs root, oldest first
        """List the runs under the runs root, as ``list_runs`` does."""
        from .runs import list_runs

        return list_runs(self.root)

    def run_folder(
        self,
        run_id: str,  # The run's ID
    ) -> Path | None:  # The run folder under the runs root; `None` when there is no such run
        """Find the run's folder under the runs root."""
        from .errors import RunNotFound
        from .runs import find_run

        try:
            return find_run(run_id, self.root, ids_only=True)
        except RunNotFound:
            return None


def check_store_uri(
    uri: str,  # A store URI, such as `file` or `sqlite:///evidence.db`
) -> None:  # Returns when uri names a store; ProveviError otherwise
    """Check that a URI names a store, and open nothing.

    A store URI is ``file``, or ``sqlite:///path`` with no query or fragment. Raises ProveviError for any
    other."""
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


def open_store(
    uri: str | None,  # `file`, or `None`, for the file store, or `sqlite:///path` for a SQLite file
    root: str | Path | None = None,  # The runs root of the file store; `None` for the default runs root
    *,
    create: bool = True,  # Make the SQLite file if it is missing, and bring an older schema up to date; with False, a SQLite store only reads, and must exist
) -> EvidenceStore:  # A `FileStore` or a `SqliteStore`
    """Open the store a URI names.

    ``sqlite:///evidence.db`` is relative to the current folder when the store opens, and
    ``sqlite:////var/evidence.db`` is absolute, as in SQLAlchemy and MLflow; ``~`` is the home folder. Raises
    ProveviError."""
    if uri is None:
        return FileStore(root)
    check_store_uri(uri)
    if uri.startswith("sqlite:"):
        from .sqlite import SqliteStore

        return SqliteStore(uri.removeprefix("sqlite:///"), create=create)
    return FileStore(root)
