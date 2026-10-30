"""Resolvers turn a logical URI into a physical URI.

Resolver is the interface. TemplateResolver is the default implementation. It
reads the ``schemes`` and ``stages`` sections of the configuration::

    schemes:
      data+table:
        parts: [org, domain, env, family, name]   # names for authority + segments
        target: "lakefs://{org}-{domain}/{ref}/tables/{family}/{name}"
        ref: "{env}"                               # default for {ref}; may name parts
        match: {org: "[a-z0-9]+", domain: "[a-z0-9]+"}   # each part must match
      code:
        parts: [org, repo]
        rest: path              # extra segments are joined into {path}
        target: "github://{org}/{repo}/{path}"
        pass_selector: true     # hand ?at= on to the physical URI
      # tenant: org             # the part that names the tenant (see below)

    stages:
      "data+table://acme/shared/prod/agreements/qa_outputs":
        production: "commit:b8a2f9c"

Selectors on a logical URI:

- ``branch:X``, ``tag:X``, ``commit:X`` set ``{ref}`` to X.
- ``version:N`` sets ``{version}`` to N. ``{ref}`` keeps its default.
- ``stage:NAME`` looks up NAME in ``stages`` and uses the selector stored there.
- ``ts:TIME`` keeps ``{ref}`` at its default, and goes on to the physical URI
  as ``?at=ts:TIME``, with or without ``pass_selector``. Only the store can say
  which commit a branch had at TIME, so the lakefs driver resolves it.

Template variables are the part names, the rest name, ``ref``, ``version`` and
``at`` (the whole selector as ``kind:value``). Each value is percent-encoded
before it goes into the template, so a value can never add a path segment or a
``?at=`` to the physical URI.

With ``pass_selector``, the physical URI gets the selector that the resolver
used: a stage becomes the selector it points at, and a scheme with a ``ref``
default passes ``branch:<ref>`` when the URI has no selector. With ``version:``,
only the version is passed.

``tenant`` names the part that says whose a URI is, for example ``org``. A
resolver made for one tenant refuses a URI of another tenant of such a scheme.
A scheme without ``tenant`` serves every tenant.

``match`` gives a regular expression for a part. A URI whose part does not
match the whole expression is refused, so that names such as lakeFS
repository names stay valid and cannot run together.
"""
from __future__ import annotations

import re
import string
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import quote

from .errors import ResolveError, UriError
from .timestamps import parse_ts
from .uri import _SAFE, Selector, Uri, parse, parse_selector

_NAME = re.compile(r"[a-z_][a-z0-9_]*")
_REF_KINDS = ("branch", "tag", "commit")
_STORE_VALUE = re.compile(r"[a-z0-9]+")


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
    match: tuple[tuple[str, re.Pattern], ...] = ()
    store_field: str | None = None  # the part that picks the store, as in lakefs-{org}
    tenant: str | None = None  # the part that names the tenant

    @classmethod
    def from_config(cls, name: str, raw: object) -> SchemeSpec:
        where = f"schemes.{name}"
        if not isinstance(raw, dict):
            raise ResolveError(f"{where} must be a mapping")
        unknown = set(raw) - {"parts", "target", "rest", "ref", "pass_selector", "match", "tenant"}
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
        ref = raw.get("ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref:
                raise ResolveError(f"{where}.ref must be a non-empty string")
            try:
                ref_fields = list(string.Formatter().parse(ref))
            except ValueError as e:
                raise ResolveError(f"{where}.ref is not a valid template: {e}") from None
            for _, field_name, spec, conv in ref_fields:
                if field_name is None:
                    continue
                if not _NAME.fullmatch(field_name) or spec or conv or field_name not in parts:
                    raise ResolveError(f"{where}.ref: fields must be part names, got {{{field_name}}}")
        match = raw.get("match")
        if match is None:
            match = {}
        if not isinstance(match, dict):
            raise ResolveError(f"{where}.match must be a mapping of part name to pattern")
        patterns = []
        # A field in the store name picks the store, for example lakefs-{org}
        # gives each org its own store and keys. The rules keep that safe:
        # one field, next to fixed text with a "-", so no value can name a
        # built-in store such as file or s3, and two values never give one
        # name. At resolve time the value must also be lowercase letters and
        # digits only (see _STORE_VALUE).
        scheme_part = target.split("://", 1)[0]
        scheme_fields = [f for _, f, _, _ in string.Formatter().parse(scheme_part) if f is not None]
        store_field = None
        if scheme_fields:
            if len(scheme_fields) > 1:
                raise ResolveError(f"{where}.target: the store name can hold one field, not {len(scheme_fields)}")
            store_field = scheme_fields[0]
            if store_field not in parts:
                raise ResolveError(f"{where}.target: the store name can use only part names, not {{{store_field}}}")
            if store_field not in match:
                raise ResolveError(f"{where}.target: {{{store_field}}} picks the store, so it needs a match pattern")
            if "-" not in scheme_part.replace("{" + store_field + "}", ""):
                raise ResolveError(f"{where}.target: write the store name with fixed text and a '-', "
                                   f"for example lakefs-{{{store_field}}}")
        for part, pattern in match.items():
            if part == raw.get("rest"):
                raise ResolveError(f"{where}.match: {part!r} is the rest name, which cannot take a pattern")
            if part not in parts or not isinstance(pattern, str):
                raise ResolveError(f"{where}.match: {part!r} must be a part name with a pattern")
            try:
                patterns.append((part, re.compile(pattern)))
            except re.error as e:
                raise ResolveError(f"{where}.match.{part}: bad pattern: {e}") from None
        tenant = raw.get("tenant")
        if tenant is not None and (tenant not in parts or not isinstance(tenant, str)):
            raise ResolveError(f"{where}.tenant must be one of the parts {list(parts)}")
        if tenant is not None and store_field is not None and tenant != store_field:
            # Else acme's URI could name another tenant's store.
            raise ResolveError(f"{where}: the tenant part {tenant!r} must be the part that picks "
                               f"the store, {store_field!r}")
        return cls(name, tuple(parts), target, raw.get("rest"), raw.get("ref"),
                   bool(raw.get("pass_selector", False)), tuple(patterns), store_field, tenant)


class TemplateResolver(Resolver):
    def __init__(self, schemes: dict | None = None, stages: dict | None = None, tenant: str | None = None):
        self.tenant = tenant
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
            if canon in self.stages:
                raise ResolveError(f"stages: {key!r} is {canon!r}, which another key also names")
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

        named = dict(zip(spec.parts, given))
        if self.tenant is not None and spec.tenant and named[spec.tenant] != self.tenant:
            raise ResolveError(f"{uri}: {spec.tenant} {named[spec.tenant]!r} is another tenant; "
                               f"this catalog is for {self.tenant}")
        for part, pattern in spec.match:
            if not pattern.fullmatch(named[part]):
                raise ResolveError(f"{uri}: {part} {named[part]!r} does not match {pattern.pattern}")
        if spec.store_field and not _STORE_VALUE.fullmatch(named[spec.store_field]):
            raise ResolveError(f"{uri}: {spec.store_field} {named[spec.store_field]!r} picks the store, "
                               "so it must be lowercase letters and digits only")
        values: dict[str, str] = {k: q(v) for k, v in named.items()}
        if spec.rest:
            values[spec.rest] = "/".join(q(v) for v in given[n:])

        via: list[str] = []
        sel = uri.selector
        if sel and sel.kind == "stage":
            target = self._stage(uri, sel.value)
            via.append(f"stage:{sel.value} -> {target}")
            sel = target
        if sel and sel.kind not in _REF_KINDS and sel.kind not in ("version", "ts"):
            raise ResolveError(f"selector kind {sel.kind!r} is not supported")
        if sel and sel.kind == "ts":
            try:
                parse_ts(sel.value)
            except UriError as e:
                raise ResolveError(f"{uri}: {e}") from None
        # The default branch can name parts, for example "{env}", so that each
        # environment is its own branch. A version or ts selector keeps the default.
        default = Selector("branch", spec.ref.format_map(named)) if spec.ref is not None else None
        ref_sel = sel if sel and sel.kind in _REF_KINDS else default
        if ref_sel:
            values["ref"] = q(ref_sel.value)
        if sel and sel.kind == "version":
            values["version"] = q(sel.value)
        sel = sel or default
        if sel:
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
        if sel is not None and (spec.pass_selector or sel.kind == "ts"):
            if physical.selector is not None:
                raise ResolveError(f"{spec.name}: target already has ?at=, so the selector {sel} cannot be added")
            physical = physical.with_selector(sel)
        via.append(f"{spec.name} template -> {physical}")
        return Resolution(uri, physical, tuple(via))
