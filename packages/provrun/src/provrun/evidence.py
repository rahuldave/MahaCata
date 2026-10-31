"""Evidence: typed, append-only events for one run, chained by hash.

A run writes its evidence into one folder::

    <run>/evidence/events.jsonl            one event on each line, in order
    <run>/evidence/payloads/<sha256>       large payloads, named by their SHA-256
    <run>/evidence/manifest.json           written when the run closes

Each event holds the SHA-256 of the line before it, so a changed, removed or
reordered line breaks the chain, and ``verify`` finds it. The manifest holds
the hash of the last line, so a removed tail is found too.

The platform writes evidence, not the agent. Each component calls ``emit`` or
``step`` itself. ``step`` also opens an OpenTelemetry span, so the live
telemetry and the canonical events come from one call.

In the strict mode a failed write raises ``EvidenceError`` and stops the run.
In the lenient mode it warns, and the manifest records that the evidence is
incomplete.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import otel
from .errors import ProvrunError

EVENT_TYPES = frozenset({
    "run_created", "run_completed", "run_failed",
    "access_requested", "access_granted", "access_denied",
    "context_resolved", "snapshot_materialized", "input_staged", "input_verified",
    "sandbox_started", "sandbox_completed",
    "prompt_rendered", "model_called", "tool_called",
    "output_artifact_created", "note_added", "trace_imported", "step_failed",
})
CONFIDENCE = ("observed", "parsed", "inferred", "verified")
MODES = ("strict", "lenient")


class EvidenceError(ProvrunError):
    """Evidence could not be written or does not verify."""


def _canonical(obj: Any) -> str:
    # ASCII only, so no character in a value, such as U+2028, can split a line.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class EvidenceEvent:
    run_id: str
    seq: int
    time: str
    type: str
    component: str
    actor: str
    refs: tuple[str, ...] = ()
    data: dict = field(default_factory=dict)
    payload: str | None = None
    confidence: str = "observed"
    prev: str | None = None

    def line(self) -> str:
        """The canonical JSON line of the event; its hash is the next event's prev."""
        d = asdict(self)
        d["refs"] = list(self.refs)
        return _canonical(d)

    @classmethod
    def from_line(cls, line: str) -> EvidenceEvent:
        d = json.loads(line)
        d["refs"] = tuple(d.get("refs", ()))
        return cls(**d)


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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Step:
    """What a component fills in while a step runs; ``EvidenceRecorder.step``
    writes it as one event when the step ends."""

    def __init__(self, refs: tuple[str, ...], data: dict, payload: Any):
        self.refs = list(refs)
        self.data = dict(data)
        self.payload = payload


class EvidenceRecorder:
    """Writes the evidence of one run. Thread-safe; one recorder per run."""

    def __init__(self, run_id: str, actor: str, folder: Path, *, mode: str = "strict",
                 sinks: list[Sink] | None = None,
                 redact: Callable[[dict], dict] | None = None):
        if mode not in MODES:
            raise ValueError(f"evidence mode must be one of {MODES}, not {mode!r}")
        self.run_id = run_id
        self.actor = actor
        self.folder = folder
        self.mode = mode
        self.redact = redact or (lambda d: d)
        self.payloads = PayloadStore(folder / "payloads")
        self.sinks: list[Sink] = [JsonlSink(folder / "events.jsonl"), *(sinks or [])]
        self.problems: list[str] = []
        self.closed = False
        self._seq = 0
        self._prev: str | None = None
        self._lock = threading.Lock()

    def _fail(self, message: str) -> None:
        self.problems.append(message)
        if self.mode == "strict":
            raise EvidenceError(message)
        warnings.warn(f"evidence of {self.run_id}: {message}", stacklevel=3)

    def emit(self, type: str, component: str, *, refs: tuple[str, ...] | list[str] = (),
             data: dict | None = None, payload: bytes | str | dict | list | None = None,
             confidence: str = "observed") -> EvidenceEvent | None:
        """Write one event. A payload is stored apart and the event keeps its hash."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown evidence event type {type!r}")
        if confidence not in CONFIDENCE:
            raise ValueError(f"confidence must be one of {CONFIDENCE}, not {confidence!r}")
        with self._lock:
            if self.closed:
                raise EvidenceError(f"the evidence of {self.run_id} is closed; no more events")
            data = self.redact(dict(data or {}))
            ref = None
            try:
                if payload is not None:
                    if isinstance(payload, (dict, list)):
                        payload = _canonical(payload)
                    ref = self.payloads.put(payload)
                event = EvidenceEvent(self.run_id, self._seq, _now(), type, component, self.actor,
                                      tuple(str(r) for r in refs), data, ref, confidence, self._prev)
                line = event.line()
            except (OSError, TypeError, ValueError) as e:
                self._fail(f"cannot write the {type} event: {e}")
                return None
            # The chain moves on only when the primary sink, the local file,
            # holds the line, so a lost line never breaks the later events.
            primary, *others = self.sinks
            try:
                primary.append(line)
            except OSError as e:
                self._fail(f"sink {primary.__class__.__name__} lost the {type} event: {e}")
                return None
            self._seq += 1
            self._prev = sha256_bytes(line.encode())
            for sink in others:
                try:
                    sink.append(line)
                except OSError as e:
                    self._fail(f"sink {sink.__class__.__name__} lost the {type} event: {e}")
            return event

    @contextlib.contextmanager
    def step(self, type: str, component: str, *, refs: tuple[str, ...] | list[str] = (),
             data: dict | None = None, payload: Any = None) -> Iterator[Step]:
        """Run a step in an OpenTelemetry span, and write its event when it ends.
        If the step raises, the event is ``step_failed`` with the error."""
        s = Step(tuple(refs), data or {}, payload)
        with otel.span(type, self.run_id, component) as span:
            try:
                yield s
            except BaseException as e:
                s.data.update(step=type, error=f"{e.__class__.__name__}: {e}")
                s.data.update(otel.ids(span))
                self.emit("step_failed", component, refs=s.refs, data=s.data, payload=s.payload)
                raise
            otel.annotate(span, s.data)
            s.data.update(otel.ids(span))
            self.emit(type, component, refs=s.refs, data=s.data, payload=s.payload)

    def close(self) -> dict:
        """Write the manifest and close the sinks. Returns the manifest."""
        with self._lock:
            if self.closed:
                return json.loads((self.folder / "manifest.json").read_text())
            self.closed = True
            for sink in self.sinks:
                try:
                    sink.close()
                except OSError as e:
                    self.problems.append(f"cannot close sink {sink.__class__.__name__}: {e}")
            manifest = {
                "run_id": self.run_id,
                "events": self._seq,
                "last": self._prev,
                "payloads": sorted(p.name for p in self.payloads.root.glob("*")
                                   if not p.name.startswith(".")) if self.payloads.root.exists() else [],
                "complete": not self.problems,
                "problems": self.problems,
            }
            (self.folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            if self.problems and self.mode == "strict":
                raise EvidenceError("; ".join(self.problems))
            return manifest


def _lines(folder: Path) -> list[str]:
    text = (folder / "events.jsonl").read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_events(folder: Path) -> list[EvidenceEvent]:
    """The events of an evidence folder. Raises EvidenceError at a line that is
    not an event."""
    events = []
    for n, line in enumerate(_lines(folder)):
        try:
            events.append(EvidenceEvent.from_line(line))
        except (ValueError, TypeError) as e:
            raise EvidenceError(f"line {n}: not an event: {e}") from e
    return events


ENDINGS = ("run_completed", "run_failed")


def verify(folder: Path, run_id: str | None = None) -> list[str]:
    """Check the chain, the sequence, the payloads and the manifest of one
    evidence folder, and that it is the whole record of one run: run_created
    first, one ending event last, as many events as the manifest says, and the
    run ID given, if one is. Returns the problems found; an empty list means it
    verifies.

    The check proves that the folder is consistent. Someone who can write the
    folder can still write a consistent false record. To rule that out,
    compare the folder with the bundle committed in the store, which
    ``verify_against`` does."""
    problems: list[str] = []
    path = folder / "events.jsonl"
    if not path.exists():
        return [f"{path} is missing"]
    prev = None
    expected = run_id
    run_id = None
    types: list[str] = []
    lines = _lines(folder)
    for n, line in enumerate(lines):
        try:
            event = EvidenceEvent.from_line(line)
        except (ValueError, TypeError) as e:
            problems.append(f"line {n}: not an event: {e}")
            prev = sha256_bytes(line.encode())
            continue
        if event.line() != line:
            problems.append(f"line {n}: not in canonical form")
        if event.seq != n:
            problems.append(f"line {n}: seq is {event.seq}")
        if event.prev != prev:
            problems.append(f"line {n}: the chain is broken")
        types.append(event.type)
        run_id = run_id or event.run_id
        if event.run_id != run_id:
            problems.append(f"line {n}: run {event.run_id}, not {run_id}")
        if event.payload is not None:
            p = folder / "payloads" / event.payload
            if not p.exists():
                problems.append(f"line {n}: payload {event.payload} is missing")
            elif sha256_bytes(p.read_bytes()) != event.payload:
                problems.append(f"line {n}: payload {event.payload} was changed")
        prev = sha256_bytes(line.encode())
    if expected is not None and run_id != expected:
        problems.append(f"the events are of run {run_id}, not {expected}")
    if not types or types[0] != "run_created":
        problems.append("the first event is not run_created")
    if not types or types[-1] not in ENDINGS:
        problems.append("the last event is not run_completed or run_failed")
    if sum(t in ENDINGS for t in types) > 1:
        problems.append("the run ends more than once")
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        problems.append("manifest.json is missing; the run did not close")
    else:
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            return problems + ["manifest.json is not JSON"]
        if manifest.get("last") != prev:
            problems.append("the manifest does not end where the events end")
        if manifest.get("events") != len(lines):
            problems.append(f"the manifest counts {manifest.get('events')} events, and there are {len(lines)}")
        if manifest.get("run_id") != run_id:
            problems.append(f"the manifest is of run {manifest.get('run_id')}, not {run_id}")
        if not manifest.get("complete", False):
            problems.append("the recorder lost events: " + "; ".join(manifest.get("problems", [])))
    return problems


def verify_against(folder: Path, events: bytes, manifest: bytes) -> list[str]:
    """Compare the local evidence with the events and the manifest read from
    the committed bundle. A difference means that one of them was changed."""
    problems = []
    if (folder / "events.jsonl").read_bytes() != events:
        problems.append("events.jsonl differs from the committed bundle")
    if (folder / "manifest.json").read_bytes() != manifest:
        problems.append("manifest.json differs from the committed bundle")
    return problems
