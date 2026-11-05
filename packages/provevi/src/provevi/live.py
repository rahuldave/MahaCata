"""What a viewer of the live feed of a run may see, and the feed of a finished run.

provrun can give a live feed of a run, which a program such as a web app
streams to a browser. The policy names the viewer as a destination of kind
``live``, with a level, and ``TelemetryPolicy.live_level`` gives that level.
``shown_at`` cuts one evidence event to what a viewer at that level may see.
``replay`` gives the feed of a run that has ended, from its evidence, so a
browser that connects after the end sees the same items.

The feed is a list of items, each a JSON object with a ``type``:

- ``event``: one evidence event, as ``shown_at`` cuts it;
- ``text``: text of the model's answer, only at ``bodies``. Live, it comes in
  pieces of any size; replayed, it is the whole answer of each model call. A
  viewer adds the pieces in order;
- ``done``: the end, with the run's ``status``, ``completed``, ``failed`` or
  ``cancelled``, and its ``run_id``.

Each item has ``n``, its number in the feed, from 1, as provrun's live feed
numbers its items.

Below ``bodies``, an event shows its type, its time, and only the data under
keys that hold names or counts, such as ``model``, ``status`` and
``input_tokens``. Any other key, such as a note's text, a purpose or a file's
name, stays out. At ``names``, a value shows if it is a plain name, as
``plain_attribute`` says. ``usage`` adds the counts and timings. ``bodies``
adds everything: all the data, the refs and the payload.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterator

from .checks import verify
from .errors import ProveviError
from .events import EvidenceEvent
from .store import EvidenceStore, RunRecord
from .telemetry import LEVELS, attributes_for, plain_attribute, plain_name, plain_span_name

# A library's span, whose data holds its own attributes.
_SPAN = "span_recorded"


# The keys of an event's data that a viewer below bodies may see, if the
# value is a plain name. Any other key may hold what a person or a model
# wrote, such as a note, a purpose or a file's name, so it stays out.
_NAME_KEYS = frozenset({
    "agent", "backend", "model", "answered_by", "tool", "org", "domain", "env", "mode", "decision", "status",
    "kind", "sha256", "finish_reason", "streamed", "unchanged", "library", "grant_id", "policy",
    "trace_id", "span_id", "parent_span_id", "started_at",
})
# The keys whose numbers a viewer at usage may see: counts and timings.
_USAGE_KEYS = frozenset({"input_tokens", "output_tokens", "first_token_ms", "duration", "size", "messages",
                         "pieces", "start_time", "end_time"})


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _plain_value(key: str, value: Any) -> bool:
    # A mapping, such as MLflow's token usage, shows only if each of its keys
    # is a plain name, as a key can hold a secret too.
    if isinstance(value, dict):
        return all(plain_name(k) for k in value)
    return not isinstance(value, (str, list, tuple)) or plain_attribute(key, value)


def _shown_data(event: EvidenceEvent, level: str) -> dict:
    data = {}
    for k, v in event.data.items():
        if event.type == _SPAN and k == "attributes" and isinstance(v, dict):
            data[k] = {a: x for a, x in attributes_for(v, level).items() if plain_name(a) and _plain_value(a, x)}
        elif event.type == _SPAN and k == "name":
            if plain_span_name(v):
                data[k] = v
        elif k in _USAGE_KEYS:
            if level == "usage" and _number(v):
                data[k] = v
        elif k in _NAME_KEYS and not _number(v) and plain_attribute(k, v):
            data[k] = v
    return data


def shown_at(event: EvidenceEvent, level: str | None, record: RunRecord | None = None) -> dict | None:
    """The ``event`` item that a viewer at level sees of event, or None if
    level is None, as for a policy with no live viewer. At ``bodies`` the
    item holds the event's refs, and its payload if record can read it."""
    if level is None:
        return None
    if level not in LEVELS:
        raise ProveviError(f"{level!r} is not a level; use one of {', '.join(LEVELS)}")
    item = {"type": "event", "run_id": event.run_id, "seq": event.seq, "time": event.time, "event": event.type}
    if level == "bodies":
        item.update(component=event.component, actor=event.actor, data=dict(event.data), refs=list(event.refs))
        if event.payload and record is not None:
            item["payload"] = _payload(record, event.payload)
        return item
    # provrun names some components after a tool that the model asked for.
    if plain_name(event.component):
        item["component"] = event.component
    if plain_name(event.actor):
        item["actor"] = event.actor
    item["data"] = _shown_data(event, level)
    return item


def _payload(record: RunRecord, digest: str) -> Any:
    raw = record.payload_bytes(digest)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _answer(payload: Any) -> str | None:
    """The text of a model call's answer, from its payload: a text from
    LiteLLM, or PydanticAI's messages, whose text parts are joined. A stream
    that failed part way keeps the text so far as ``partial_response``."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("partial_response"), str):
        return payload["partial_response"]
    response = payload.get("response")
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        parts = [p.get("content") for m in response if isinstance(m, dict) for p in m.get("parts") or []
                 if isinstance(p, dict) and p.get("part_kind") == "text" and isinstance(p.get("content"), str)]
        return "".join(parts) if parts else None
    return None


def replay(store: EvidenceStore, run_id: str, level: str, *, folder: Path | None = None) -> Iterator[dict]:
    """The feed of a run that has ended, at level, from its evidence: an
    ``event`` item for each event, a ``text`` item at ``bodies`` after each
    model call and after a stream that failed part way, and a ``done`` item.
    folder is the run's evidence folder, which the file store finds by itself
    if it is left out. Raises
    ProveviError, before the first item, if the run does not verify, as a
    run that has not ended does not."""
    if level not in LEVELS:
        raise ProveviError(f"{level!r} is not a level; use one of {', '.join(LEVELS)}")
    record = store.read_run(run_id, folder)
    problems = verify(record, run_id)
    if problems:
        raise ProveviError(f"{run_id} does not verify, so it is not replayed: " + "; ".join(problems[:3]))
    events = [EvidenceEvent.from_line(line) for line in record.lines()]
    return _items(events, record, level, run_id)


def _items(events: list[EvidenceEvent], record: RunRecord, level: str, run_id: str) -> Iterator[dict]:
    items = []
    for e in events:
        items.append(shown_at(e, level, record))
        if level == "bodies" and e.type in ("model_called", "step_failed") and e.payload:
            text = _answer(_payload(record, e.payload))
            if text:
                items.append({"type": "text", "text": text})
    items.append({"type": "done", "status": _status(events[-1]), "run_id": run_id})
    for n, item in enumerate(items, 1):
        yield {"n": n, **item}


def _status(ending: EvidenceEvent) -> str:
    """completed, failed, or cancelled for a run that provrun cancelled, whose
    run_failed names the error CancelledError."""
    if ending.type == "run_completed":
        return "completed"
    error = ending.data.get("error")
    return "cancelled" if isinstance(error, str) and error.startswith("CancelledError") else "failed"
