"""The time in a ``ts:`` selector.

``?at=ts:TIME`` picks the version that a branch had at TIME. TIME is one of:

- a date, ``2026-10-01``, which means 00:00 UTC that day;
- a date and time, ``2026-10-01T12:30:00``, with an optional zone such as
  ``Z`` or ``+05:30``; a time without a zone is UTC;
- Unix seconds, ``1793200661``.

Write a ``+`` in a zone as ``%2B`` in a URI, because a URI can turn ``+`` into
a space on the way.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from .errors import UriError

_SECONDS = re.compile(r"\d{9,11}")


def parse_ts(value: str) -> datetime:
    """The UTC moment that a ts: value names."""
    text = value.strip()
    if _SECONDS.fullmatch(text):
        return datetime.fromtimestamp(int(text), UTC)
    try:
        moment = datetime.fromisoformat(text.replace("z", "Z"))
    except ValueError:
        raise UriError(f"ts:{value} is not a date, a date and time, or Unix seconds") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)
