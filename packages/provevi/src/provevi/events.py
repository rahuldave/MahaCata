"""The format of an evidence event.

Each event is one line of canonical JSON. Each line holds the SHA-256 of the
line before it, in ``prev``, so a changed, removed or reordered line breaks
the chain. provevi owns this format; provrun decides what each event means.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

__all__ = ["CONFIDENCE", "EvidenceEvent", "canonical", "now", "sha256_bytes"]

CONFIDENCE = ("observed", "parsed", "inferred", "verified")


def canonical(
    obj: Any,  # A value JSON can hold; any other value is written as its str
) -> str:  # Its JSON with sorted keys, no spaces and ASCII only
    """The canonical JSON of a value: sorted keys, no spaces, ASCII only."""
    # ASCII only, so no character in a value, such as U+2028, can split a line.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_bytes(
    data: bytes,  # The bytes to hash
) -> str:  # Their SHA-256, as 64 lowercase hex digits
    """The SHA-256 of some bytes, as hex: how provevi names payloads and chains events."""
    return hashlib.sha256(data).hexdigest()


def now() -> str:  # Such as `2026-10-31T16:40:32.123456Z`
    """The time of an event: UTC, to the microsecond, ending in Z."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class EvidenceEvent:
    """One evidence event: one line of a run's evidence, chained to the line before it by ``prev``."""

    run_id: str  # The run that the event belongs to
    seq: int  # The event's position in the run, from 0
    time: str  # The UTC time the event was written, as `now` gives it
    type: str  # What happened, such as `run_created` or `model_called`; provrun decides the types
    component: str  # The part of provrun that wrote the event: a class such as `RunContext`, `agent`, or a name such as `tool:grep`
    actor: str  # The user or the service identity of the run
    refs: tuple[str, ...] = ()  # The logical URIs that the event is about, pinned or not
    data: dict = field(default_factory=dict)  # Small facts, such as a model name or a token count
    payload: str | None = None  # The SHA-256 of a large payload stored apart from the event, or `None`
    confidence: str = "observed"  # How far a reader can trust the event: one of `CONFIDENCE`
    prev: str | None = None  # The SHA-256 of the line of the event before it; `None` for the first

    def line(self) -> str:  # One line of canonical JSON, with no newline
        """The canonical JSON line of the event; its hash is the next event's prev."""
        d = asdict(self)
        d["refs"] = list(self.refs)
        return canonical(d)

    @classmethod
    def from_line(
        cls,
        line: str,  # One line of a run's `events.jsonl`
    ) -> EvidenceEvent:  # The event the line holds
        """Read the event that one line holds.

        Raises ValueError or TypeError if the line is not an event with fields of the right types."""
        try:
            d = json.loads(line)
        except RecursionError:
            raise ValueError("the line is nested too deeply") from None
        if not isinstance(d, dict):
            raise ValueError("the line is not a JSON object")
        d["refs"] = d.get("refs", [])
        if not isinstance(d["refs"], list) or not all(isinstance(r, str) for r in d["refs"]):
            raise ValueError("refs is not a list of strings")
        d["refs"] = tuple(d["refs"])
        event = cls(**d)
        for name in ("run_id", "time", "type", "component", "actor", "confidence"):
            if not isinstance(getattr(event, name), str):
                raise ValueError(f"{name} is not a string")
        if not isinstance(event.seq, int) or isinstance(event.seq, bool):
            raise ValueError("seq is not a whole number")
        if not isinstance(event.data, dict):
            raise ValueError("data is not a JSON object")
        for name in ("payload", "prev"):
            if getattr(event, name) is not None and not isinstance(getattr(event, name), str):
                raise ValueError(f"{name} is not a string or null")
        return event
