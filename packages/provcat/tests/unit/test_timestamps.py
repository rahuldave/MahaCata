from datetime import UTC, datetime, timedelta, timezone

import pytest

from provcat.errors import UriError
from provcat.timestamps import parse_ts


@pytest.mark.parametrize("text, want", [
    ("2026-10-01", datetime(2026, 10, 1, tzinfo=UTC)),
    ("2026-10-01T12:30:00", datetime(2026, 10, 1, 12, 30, tzinfo=UTC)),
    ("2026-10-01T12:30:00Z", datetime(2026, 10, 1, 12, 30, tzinfo=UTC)),
    ("2026-10-01T18:00:00+05:30", datetime(2026, 10, 1, 12, 30, tzinfo=UTC)),
    ("1793200661", datetime.fromtimestamp(1793200661, UTC)),
])
def test_parse_ts(text, want):
    got = parse_ts(text)
    assert got == want and got.tzinfo == UTC


@pytest.mark.parametrize("text", ["yesterday", "2026-13-01", "12", "2026-10-01 12:30 PST"])
def test_bad_ts(text):
    with pytest.raises(UriError):
        parse_ts(text)


def test_offset_times_compare_as_utc():
    assert parse_ts("2026-10-01T00:00:00-01:00") - parse_ts("2026-10-01T00:00:00Z") == timedelta(hours=1)
    assert parse_ts("2026-10-01T00:00:00-01:00").tzinfo == UTC != timezone(timedelta(hours=-1))
