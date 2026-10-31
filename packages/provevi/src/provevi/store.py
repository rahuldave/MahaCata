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
    """Appends each line to a local file, and flushes it at once."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")

    def append(self, line: str) -> None:
        self._f.write(line + "\n")
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()


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


class EvidenceStore(ABC):
    """Where runs keep their evidence. One store can hold many runs."""

    @abstractmethod
    def open_run(self, run_id: str, folder: Path) -> RunWriter:
        """Open the evidence of a new run. folder is the run's evidence folder,
        which holds the bundle files that provrun commits through provcat."""


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
    """The ``file`` store: each run's evidence in files in its own folder."""

    def open_run(self, run_id: str, folder: Path) -> RunWriter:
        return FileRunWriter(folder)
