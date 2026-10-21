"""Catalog URIs.

A URI has the form ``scheme://authority/seg/seg?at=kind:value``.

- The scheme picks a resolver (logical URIs) or a store driver (physical URIs).
- The authority is the first name after ``//``. It may be empty, as in
  ``file:///tmp``. Each store driver and the resolver say whether they need one.
  It is percent-decoded like a segment.
- Segments are percent-decoded. Empty, ``.`` and ``..`` segments are refused, so a
  URI can never climb out of a cache folder. One trailing slash is allowed and
  dropped, so ``s3://b/dir/`` is ``s3://b/dir``.
- The only query parameter is ``at``, the selector. A fragment is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote

from .errors import UriError

_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")
_KIND = re.compile(r"[a-z]+")
_SAFE = "-._~!$&'()*+,;=:@"
_SAFE_QUERY = _SAFE.replace("&", "") + "/"


@dataclass(frozen=True)
class Selector:
    """The ``?at=kind:value`` part of a URI, for example ``branch:main``."""

    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_selector(text: str) -> Selector:
    kind, sep, value = text.partition(":")
    if not sep or not _KIND.fullmatch(kind) or not value:
        raise UriError(f"selector must look like kind:value, got {text!r}")
    return Selector(kind, value)


@dataclass(frozen=True)
class Uri:
    scheme: str
    authority: str
    segments: tuple[str, ...] = ()
    selector: Selector | None = None

    @property
    def path(self) -> str:
        """The segments joined with ``/``, without a leading slash."""
        return "/".join(self.segments)

    def without_selector(self) -> Uri:
        return replace(self, selector=None)

    def with_selector(self, selector: Selector | None) -> Uri:
        return replace(self, selector=selector)

    def child(self, name: str) -> Uri:
        return replace(self, segments=self.segments + (_check_segment(name),))

    def __str__(self) -> str:
        text = f"{self.scheme}://{quote(self.authority, safe=_SAFE)}"
        if self.segments:
            text += "/" + "/".join(quote(s, safe=_SAFE) for s in self.segments)
        if self.selector:
            text += "?at=" + quote(str(self.selector), safe=_SAFE_QUERY)
        return text


def _check_segment(seg: str) -> str:
    if seg in ("", ".", "..") or "/" in seg or "\\" in seg or "\0" in seg:
        raise UriError(f"bad path segment {seg!r}")
    return seg


def parse(text: str | Uri) -> Uri:
    """Parse a URI string. A Uri passes through unchanged."""
    if isinstance(text, Uri):
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        raise UriError(f"no scheme in {text!r}; write scheme://...")
    if not _SCHEME.fullmatch(scheme):
        raise UriError(f"bad scheme {scheme!r}; use lower case letters, digits, + . -")
    if "#" in rest:
        raise UriError(f"fragments are not allowed: {text!r}")
    rest, qsep, query = rest.partition("?")
    selector = None
    if qsep:
        name, eq, value = query.partition("=")
        if name != "at" or not eq or "&" in value:
            raise UriError(f"the only query allowed is ?at=kind:value, got ?{query}")
        selector = parse_selector(unquote(value))
    authority, _, path = rest.partition("/")
    authority = unquote(authority)
    if authority:
        _check_segment(authority)
    if path.endswith("/"):
        path = path[:-1]
    if (path == "" and rest.endswith("//")) or (path and "" in path.split("/")):
        raise UriError(f"empty path segment in {text!r}")
    segments = tuple(_check_segment(unquote(s)) for s in path.split("/")) if path else ()
    return Uri(scheme, authority, segments, selector)
