"""Resolvers turn a logical URI into a physical URI.

Resolver is the interface. TemplateResolver is the default implementation. It
reads the ``schemes`` and ``stages`` sections of the configuration::

    schemes:
      data+table:
        parts: [org, domain, env, family, name]   # names for authority + segments
        target: "lakefs://{org}-{family}/{ref}/{domain}/{env}/{name}"
        ref: main                                  # default for {ref}
      code:
        parts: [owner, repo]
        rest: path              # extra segments are joined into {path}
        target: "github://{owner}/{repo}/{path}"
        pass_selector: true     # hand ?at= on to the physical URI

    stages:
      "data+table://acme/shared/prod/agreements/qa_outputs":
        production: "commit:b8a2f9c"

Selectors on a logical URI:

- ``branch:X``, ``tag:X``, ``commit:X`` set ``{ref}`` to X.
- ``version:N`` sets ``{version}`` to N.
- ``stage:NAME`` looks up NAME in ``stages`` and uses the selector stored there.
- ``ts:...`` is refused for now.

Template variables are the part names, the rest name, ``ref``, ``version`` and
``at`` (the whole selector as ``kind:value``). Each value is percent-encoded
before it goes into the template, so a value can never add a path segment or a
``?at=`` to the physical URI.

With ``pass_selector``, the physical URI gets the selector that the resolver
used: a stage becomes the selector it points at, and a scheme with a ``ref``
default passes ``branch:<ref>`` when the URI has no selector.
"""
from __future__ import annotations

import re
import string
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import quote

from .errors import ResolveError, UriError
from .uri import _SAFE, Selector, Uri, parse, parse_selector

_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_REF_KINDS = ("branch", "tag", "commit")


@dataclass(frozen=True)
class Resolution:
    logical: Uri
    physical: Uri
    via: tuple[str, ...] = field(default=())  # the steps taken, for people to read


class Resolver(ABC):
    @abstractmethod
    def handles(self, scheme: str) -> bool: ...

    @abstractmethod
    def resolve(self, uri: Uri) -> Resolution: ...


@dataclass(frozen=True)
class SchemeSpec:
    name: str
    parts: tuple[str, ...]
    target: str
    rest: str | None = None
    ref: str | None = None
    pass_selector: bool = False

    @classmethod
    def from_config(cls, name: str, raw: object) -> SchemeSpec:
        where = f"schemes.{name}"
        if not isinstance(raw, dict):
            raise ResolveError(f"{where} must be a mapping")
        unknown = set(raw) - {"parts", "target", "rest", "ref", "pass_selector"}
        if unknown:
            raise ResolveError(f"{where}: unknown keys {sorted(unknown)}")
        parts = raw.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ResolveError(f"{where}.parts must be a non-empty list of names")
        names = list(parts) + ([raw["rest"]] if raw.get("rest") else [])
        for n in names:
            if not isinstance(n, str) or not _NAME.fullmatch(n) or n in ("ref", "version", "at"):
                raise ResolveError(f"{where}: bad part name {n!r}")
        if len(set(names)) != len(names):
            raise ResolveError(f"{where}: part names repeat")
        target = raw.get("target")
        if not isinstance(target, str) or "://" not in target:
            raise ResolveError(f"{where}.target must be a URI template")
        try:
            fields = list(string.Formatter().parse(target))
        except ValueError as e:
            raise ResolveError(f"{where}.target is not a valid template: {e}") from None
        allowed = set(names) | {"ref", "version", "at"}
        for _, field_name, spec, conv in fields:
            if field_name is None:
                continue
            if not _NAME.fullmatch(field_name) or spec or conv:
                raise ResolveError(f"{where}.target: write fields as {{name}}, got {{{field_name}}}")
            if field_name not in allowed:
                raise ResolveError(f"{where}.target: unknown field {{{field_name}}}; known: {sorted(allowed)}")
        if not isinstance(raw.get("pass_selector", False), bool):
            raise ResolveError(f"{where}.pass_selector must be true or false")
        if raw.get("ref") is not None and not isinstance(raw["ref"], str):
            raise ResolveError(f"{where}.ref must be a string")
        return cls(name, tuple(parts), target, raw.get("rest"), raw.get("ref"),
                   bool(raw.get("pass_selector", False)))


class TemplateResolver(Resolver):
    def __init__(self, schemes: dict | None = None, stages: dict | None = None):
        self.specs = {n: SchemeSpec.from_config(n, s) for n, s in (schemes or {}).items()}
        self.stages = {}
        for key, pointers in (stages or {}).items():
            if not isinstance(key, str):
                raise ResolveError(f"stages: key {key!r} must be a URI string")
            try:
                parsed = parse(key)
            except UriError as e:
                raise ResolveError(f"stages: bad key {key!r}: {e}") from e
            if parsed.selector is not None:
                raise ResolveError(f"stages: key {key!r} must not have ?at=")
            canon = str(parsed)
            if not isinstance(pointers, dict):
                raise ResolveError(f"stages.{key} must be a mapping of stage name to selector")
            self.stages[canon] = pointers

    def handles(self, scheme: str) -> bool:
        return scheme in self.specs

    def _stage(self, uri: Uri, name: str) -> Selector:
        key = str(uri.without_selector())
        pointers = self.stages.get(key)
        if not isinstance(pointers, dict) or name not in pointers:
            raise ResolveError(f"no stage {name!r} for {key}")
        try:
            sel = parse_selector(str(pointers[name]))
        except UriError as e:
            raise ResolveError(f"stage {name!r} for {key}: {e}") from e
        if sel.kind == "stage":
            raise ResolveError(f"stage {name!r} for {key} points at another stage")
        return sel

    def resolve(self, uri: Uri) -> Resolution:
        spec = self.specs.get(uri.scheme)
        if spec is None:
            raise ResolveError(f"no logical scheme {uri.scheme!r} in the configuration")
        if not uri.authority:
            raise ResolveError(f"{uri}: a logical URI needs a first part after //")
        given = (uri.authority,) + uri.segments
        n = len(spec.parts)
        if len(given) < n or (len(given) > n and not spec.rest):
            shape = "/".join(spec.parts) + (f"/<{spec.rest}...>" if spec.rest else "")
            raise ResolveError(f"{uri} does not match {spec.name}://{shape}")
        def q(v: str) -> str:
            return quote(v, safe=_SAFE.replace("&", ""))

        values: dict[str, str] = {k: q(v) for k, v in zip(spec.parts, given)}
        if spec.rest:
            values[spec.rest] = "/".join(q(v) for v in given[n:])

        via: list[str] = []
        sel = uri.selector
        if sel and sel.kind == "stage":
            target = self._stage(uri, sel.value)
            via.append(f"stage:{sel.value} -> {target}")
            sel = target
        if sel is None and spec.ref is not None:
            sel = Selector("branch", spec.ref)
        if sel:
            if sel.kind in _REF_KINDS:
                values["ref"] = q(sel.value)
            elif sel.kind == "version":
                values["version"] = q(sel.value)
            else:
                raise ResolveError(f"selector kind {sel.kind!r} is not supported")
            values["at"] = q(str(sel))

        try:
            text = spec.target.format_map(values)
        except KeyError as e:
            raise ResolveError(
                f"{spec.name} target needs {{{e.args[0]}}}, which {uri} does not give"
            ) from None
        except (ValueError, IndexError, AttributeError) as e:
            raise ResolveError(f"{spec.name} target is not a valid template: {e}") from None
        try:
            physical = parse(text)
        except UriError as e:
            raise ResolveError(f"{uri} resolves to a bad URI {text!r}: {e}") from e
        if spec.pass_selector and sel is not None:
            if physical.selector is not None:
                raise ResolveError(f"{spec.name}: target already has ?at=, so pass_selector cannot add one")
            physical = physical.with_selector(sel)
        via.append(f"{spec.name} template -> {physical}")
        return Resolution(uri, physical, tuple(via))
