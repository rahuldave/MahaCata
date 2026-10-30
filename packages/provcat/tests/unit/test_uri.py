import pytest

from provcat import Selector, Uri, UriError, parse


def test_parse_logical():
    u = parse("data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production")
    assert u.scheme == "data+table"
    assert u.authority == "acme"
    assert u.segments == ("shared", "prod", "agreements", "qa_outputs")
    assert u.selector == Selector("stage", "production")


def test_round_trip_with_escapes():
    text = "s3://bucket/a%20b/c%3Fd?at=branch:feat/x"
    u = parse(text)
    assert u.segments == ("a b", "c?d")
    assert u.selector.value == "feat/x"
    assert parse(str(u)) == u


def test_file_uri_has_empty_authority():
    u = parse("file:///tmp/x/y")
    assert u.authority == "" and u.segments == ("tmp", "x", "y")


def test_selector_value_may_hold_colons():
    assert parse("x://a/b?at=ts:2026-01-01T10:00:00").selector.value == "2026-01-01T10:00:00"


@pytest.mark.parametrize("bad", [
    "noscheme",
    "S3://bucket/key",
    "s3://bucket/../etc",
    "s3://bucket/a/./b",
    "s3://bucket/a%2Fb",
    "s3://bucket/a/%2E%2E/b",
    "s3://bucket/a//b",
    "s3://bucket/a#frag",
    "s3://bucket/a?x=1",
    "s3://bucket/a?at=nokind",
    "s3://bucket/a?at=branch:x&y=2",
])
def test_rejects(bad):
    with pytest.raises(UriError):
        parse(bad)


def test_trailing_slash_is_dropped_but_other_empties_fail():
    assert parse("s3://b/k/") == parse("s3://b/k")
    assert parse("s3://b/") == parse("s3://b")
    for bad in ["s3://b//k", "s3://b/k//", "s3://b//"]:
        with pytest.raises(UriError):
            parse(bad)


def test_selector_with_ampersand_round_trips():
    # Finding 2: str() wrote a bare & that parse() then refused.
    u = parse("github://o/r/f?at=branch:a%26b")
    assert u.selector.value == "a&b"
    assert parse(str(u)) == u


def test_child_refuses_traversal():
    with pytest.raises(UriError):
        Uri("s3", "b").child("..")
