"""Evidence: typed, append-only events for one run, chained by hash.

provrun decides what each event means and builds the chain; provevi stores
the events and checks them. A run writes its evidence through a provevi
store, by default the ``file`` store, into one folder::

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
import threading
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import provevi
from provevi import (CONFIDENCE, ENDINGS, EvidenceEvent, EvidenceStore, FileStore, JsonlSink, PayloadStore,
                     Sink, sha256_bytes, verify, verify_against)
from provevi.events import canonical as _canonical
from provevi.events import now as _now

from . import otel
from .errors import ProvrunError

__all__ = [
    "CONFIDENCE", "ENDINGS", "EVENT_TYPES", "MODES", "EvidenceError", "EvidenceEvent", "EvidenceRecorder",
    "JsonlSink", "PayloadStore", "Sink", "Step", "read_events", "sha256_bytes", "verify", "verify_against",
]

EVENT_TYPES = frozenset({
    "run_created", "run_completed", "run_failed",
    "access_requested", "access_granted", "access_denied",
    "context_resolved", "snapshot_materialized", "input_staged", "input_verified",
    "sandbox_started", "sandbox_completed",
    "prompt_rendered", "model_called", "tool_called",
    "output_artifact_created", "note_added", "trace_imported", "step_failed",
})
MODES = ("strict", "lenient")


class EvidenceError(ProvrunError, provevi.EvidenceError):
    """Evidence could not be written or does not verify. It is also a
    provevi EvidenceError, which is what provevi's checks raise."""


def read_events(folder: Path) -> list[EvidenceEvent]:
    """The events of an evidence folder, as provevi.read_events reads them.
    Raises provrun's EvidenceError at a line that is not an event."""
    try:
        return provevi.read_events(folder)
    except provevi.EvidenceError as e:
        raise EvidenceError(str(e)) from e


class Step:
    """What a component fills in while a step runs; ``EvidenceRecorder.step``
    writes it as one event when the step ends."""

    def __init__(self, refs: tuple[str, ...], data: dict, payload: Any):
        self.refs = list(refs)
        self.data = dict(data)
        self.payload = payload


# The keys of a step's data that the recorder, not the step, sets.
_STAMPED = ("started_at", "trace_id", "span_id", "parent_span_id")


def _stamp(data: dict, started: str, span) -> None:
    for key in _STAMPED:
        data.pop(key, None)
    data["started_at"] = started
    data.update(otel.ids(span))


class EvidenceRecorder:
    """Writes the evidence of one run. Thread-safe; one recorder per run."""

    def __init__(self, run_id: str, actor: str, folder: Path, *, mode: str = "strict",
                 sinks: list[Sink] | None = None,
                 redact: Callable[[dict], dict] | None = None,
                 store: EvidenceStore | None = None):
        if mode not in MODES:
            raise ValueError(f"evidence mode must be one of {MODES}, not {mode!r}")
        self.run_id = run_id
        self.actor = actor
        self.folder = folder
        self.mode = mode
        self.redact = redact or (lambda d: d)
        # The store's writer is the first sink: an event counts as written
        # only when the store holds its line.
        self.writer = (store if store is not None else FileStore()).open_run(run_id, folder)
        self.sinks: list[Sink] = [self.writer, *(sinks or [])]
        self.problems: list[str] = []
        self.closed = False
        self._seq = 0
        self._prev: str | None = None
        self._lock = threading.Lock()

    @property
    def payloads(self):
        """The payload store of a file store's writer, kept for callers of
        earlier versions. Use ``writer.get_payload`` instead."""
        return getattr(self.writer, "payloads", None)

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
                    ref = self.writer.put_payload(payload)
                event = EvidenceEvent(self.run_id, self._seq, _now(), type, component, self.actor,
                                      tuple(str(r) for r in refs), data, ref, confidence, self._prev)
                line = event.line()
            except Exception as e:  # a store may raise any error; the mode decides
                self._fail(f"cannot write the {type} event: {e}")
                return None
            # The chain moves on only when the primary sink, the local file,
            # holds the line, so a lost line never breaks the later events.
            primary, *others = self.sinks
            try:
                primary.append(line)
            except Exception as e:
                self._fail(f"sink {primary.__class__.__name__} lost the {type} event: {e}")
                return None
            self._seq += 1
            self._prev = sha256_bytes(line.encode())
            for sink in others:
                try:
                    sink.append(line)
                except Exception as e:
                    self._fail(f"sink {sink.__class__.__name__} lost the {type} event: {e}")
            return event

    @contextlib.contextmanager
    def step(self, type: str, component: str, *, refs: tuple[str, ...] | list[str] = (),
             data: dict | None = None, payload: Any = None) -> Iterator[Step]:
        """Run a step in an OpenTelemetry span, and write its event when it ends.
        If the step raises, the event is ``step_failed`` with the error. The
        event's data holds ``started_at``, the time the step began, in the
        format of the event's time, and the span's IDs when a span records.
        The recorder sets those keys; a value the step gives for them is dropped."""
        s = Step(tuple(refs), data or {}, payload)
        with otel.span(type, self.run_id, component) as span:
            started = _now()
            try:
                yield s
            except BaseException as e:
                s.data.update(step=type, error=f"{e.__class__.__name__}: {e}")
                _stamp(s.data, started, span)
                self.emit("step_failed", component, refs=s.refs, data=s.data, payload=s.payload)
                raise
            otel.annotate(span, s.data)
            _stamp(s.data, started, span)
            self.emit(type, component, refs=s.refs, data=s.data, payload=s.payload)

    def close(self) -> dict:
        """Write the manifest and close the sinks. Returns the manifest."""
        with self._lock:
            if self.closed:
                return self.writer.manifest()
            self.closed = True
            for sink in self.sinks:
                try:
                    sink.close()
                except Exception as e:
                    self.problems.append(f"cannot close sink {sink.__class__.__name__}: {e}")
            try:
                payloads = self.writer.payload_names()
            except Exception as e:
                payloads = []
                self.problems.append(f"cannot list the payloads: {e}")
            manifest = {
                "run_id": self.run_id,
                "events": self._seq,
                "last": self._prev,
                "payloads": payloads,
                "complete": not self.problems,
                "problems": self.problems,
            }
            try:
                self.writer.seal(manifest)
            except Exception as e:
                # With no manifest nothing can verify the run, so this fails
                # it in both modes.
                raise EvidenceError("; ".join([*self.problems, f"cannot seal the evidence: {e}"])) from e
            # provevi's checks and the bundle read the files in the evidence
            # folder, so a store must keep them there, in both modes.
            if not all((self.folder / n).is_file() for n in ("events.jsonl", "manifest.json")):
                raise EvidenceError(f"the store kept no events.jsonl and manifest.json in {self.folder}; "
                                    "nothing can verify the run")
            if self.problems and self.mode == "strict":
                raise EvidenceError("; ".join(self.problems))
            return manifest
