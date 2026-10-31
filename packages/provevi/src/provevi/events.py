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

CONFIDENCE = ("observed", "parsed", "inferred", "verified")


def canonical(obj: Any) -> str:
    """The canonical JSON of obj: sorted keys, no spaces, ASCII only."""
    # ASCII only, so no character in a value, such as U+2028, can split a line.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    """The time of an event: UTC, to the microsecond, ending in Z."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        return canonical(d)

    @classmethod
    def from_line(cls, line: str) -> EvidenceEvent:
        """The event of one line. Raises ValueError or TypeError if the line
        is not an event with fields of the right types."""
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
