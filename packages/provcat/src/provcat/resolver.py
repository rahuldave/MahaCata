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
  which commit a branch had at TIME, so the lakefs and github drivers resolve
  it. A target without ``{ref}`` loses the branch, so the resolution keeps it
  as ``ts_branch``. The catalog gives a GitHub store ``?at=ts:BRANCH@TIME``.

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

Entries send one logical name, or every name under a prefix, somewhere other
than the template of its scheme::

    entries:
      "code://acme/tokenizer/prod":
        target: "github://acme/tokenizer/{path}?at=branch:prod"
      "code://acme/tokenizer/prod/weights":
        target: "hf://models/acme/tokenizer/{path}"
      "data+table://acme/shared/prod/corpora":
        target: "lakefs-acme://acme-corpora/{ref}/{path}"
        ref: main

- The longest key that is a prefix of the URI wins, by whole parts. A URI
  that no key matches goes to the template of its scheme.
- ``{path}`` is the rest of the URI after the key, each part encoded. A target
  without ``{path}`` serves only the URI that is its key.
- ``{ref}`` is for a store that keeps the branch in the path, such as lakeFS.
  ``branch:``, ``tag:`` and ``commit:`` on the URI fill it, and ``ref`` is the
  default. A target without ``{ref}`` takes the selector of the URI in place of
  its own ``?at=``.
- ``ts:TIME`` keeps the branch of the target. A target that names a branch in
  ``?at=`` gives it as ``ts_branch``, as a template does. A target fixed to a
  tag or a commit refuses ``ts:``. ``version:`` is refused.
- A stage of the URI comes first, and its selector then applies as above.
- A key must be a URI of a logical scheme, with no ``?at=``. Its parts must
  fit the ``parts`` and ``match`` of the scheme as far as the key goes. The
  store of a target is fixed text.
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
    ts_branch: str | None = None  # the branch of a ts: that the physical URI has no place for


class Resolver(ABC):
    @abstractmethod
    def handles(self, scheme: str) -> bool: ...

    @abstractmethod
    def resolve(self, uri: Uri) -> Resolution: ...


def _uses_ref(target: str) -> bool:
    return any(f == "ref" for _, f, _, _ in string.Formatter().parse(target))


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


@dataclass(frozen=True)
class EntrySpec:
    key: Uri  # the logical prefix, with no selector
    target: str
    ref: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        return (self.key.authority,) + self.key.segments

    @property
    def has_path(self) -> bool:
        return "path" in _fields(self.target)

    @property
    def has_ref(self) -> bool:
        return "ref" in _fields(self.target)

    @property
    def store(self) -> str:
        return self.target.split("://", 1)[0]

    def format(self, path: str, ref: str | None) -> str:
        target = self.target if path else self.target.replace("/{path}", "{path}")
        return target.format_map({"path": path, "ref": ref or ""})


def _fields(template: str) -> list[str]:
    return [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]


def _check_parts(spec: SchemeSpec, uri: Uri, given: tuple[str, ...]) -> None:
    """Refuse a URI, or the key of an entry, whose parts do not fit the scheme."""
    if len(given) > len(spec.parts) and not spec.rest:
        shape = "/".join(spec.parts)
        raise ResolveError(f"{uri} does not match {spec.name}://{shape}")
    named = dict(zip(spec.parts, given))
    for part, pattern in spec.match:
        if part in named and not pattern.fullmatch(named[part]):
            raise ResolveError(f"{uri}: {part} {named[part]!r} does not match {pattern.pattern}")


def _entry(key: object, raw: object, specs: dict[str, SchemeSpec]) -> EntrySpec:
    if not isinstance(key, str):
        raise ResolveError(f"entries: key {key!r} must be a URI string")
    where = f"entries.{key}"
    try:
        uri = parse(key)
    except UriError as e:
        raise ResolveError(f"entries: bad key {key!r}: {e}") from e
    if uri.selector is not None:
        raise ResolveError(f"entries: key {key!r} must not have ?at=")
    spec = specs.get(uri.scheme)
    if spec is None:
        raise ResolveError(f"entries: key {key!r} is not a URI of a logical scheme")
    if not uri.authority:
        raise ResolveError(f"entries: key {key!r} needs a first part after //")
    _check_parts(spec, uri, (uri.authority,) + uri.segments)
    if not isinstance(raw, dict):
        raise ResolveError(f"{where} must be a mapping with a target")
    unknown = set(raw) - {"target", "ref"}
    if unknown:
        raise ResolveError(f"{where}: unknown keys {sorted(unknown)}")
    target, ref = raw.get("target"), raw.get("ref")
    if not isinstance(target, str) or "://" not in target:
        raise ResolveError(f"{where}.target must be a URI template")
    try:
        fields = list(string.Formatter().parse(target))
    except ValueError as e:
        raise ResolveError(f"{where}.target is not a valid template: {e}") from None
    for _, name, spec_text, conv in fields:
        if name is None:
            continue
        if name not in ("path", "ref") or spec_text or conv:
            raise ResolveError(f"{where}.target: the only fields are {{path}} and {{ref}}, got {{{name}}}")
    entry = EntrySpec(uri, target, ref)
    place = target.split("://", 1)[1].split("/", 1)[0]
    if _fields(entry.store) or _fields(place):
        raise ResolveError(f"{where}.target: the store name and the first part after // must be fixed text")
    if entry.has_ref != (ref is not None):
        raise ResolveError(f"{where}: a target with {{ref}} needs a ref, the default branch, "
                           "and a ref needs {ref} in the target")
    if ref is not None and (not isinstance(ref, str) or not ref or "{" in ref or "/" in ref
                            or ref in (".", "..")):
        raise ResolveError(f"{where}.ref must be a branch name without /, as it fills one part of the path")
    for path in ("", "x/y"):
        try:
            got = parse(entry.format(path, ref))
        except (UriError, ValueError, KeyError) as e:
            raise ResolveError(f"{where}.target does not make a valid URI: {e}") from None
        if entry.has_ref and got.selector is not None:
            raise ResolveError(f"{where}.target: a target with {{ref}} cannot also have ?at=")
    return entry


class TemplateResolver(Resolver):
    def __init__(self, schemes: dict | None = None, stages: dict | None = None, tenant: str | None = None,
                 entries: dict | None = None):
        self.tenant = tenant
        self.specs = {n: SchemeSpec.from_config(n, s) for n, s in (schemes or {}).items()}
        self.entries: dict[tuple[str, ...], EntrySpec] = {}
        for key, raw in (entries or {}).items():
            entry = _entry(key, raw, self.specs)
            name = (entry.key.scheme,) + entry.parts
            if name in self.entries:
                raise ResolveError(f"entries: {key!r} is {str(entry.key)!r}, which another key also names")
            self.entries[name] = entry
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

    def serves(self, uri: Uri) -> bool:
        """False for a URI that names another tenant, which this resolver refuses."""
        spec = self.specs.get(uri.scheme)
        if self.tenant is None or spec is None or not spec.tenant:
            return True
        named = dict(zip(spec.parts, (uri.authority,) + uri.segments))
        return named.get(spec.tenant, self.tenant) == self.tenant

    def _match(self, uri: Uri, given: tuple[str, ...]) -> EntrySpec | None:
        """The entry with the longest key that is a prefix of the URI."""
        for k in range(len(given), 0, -1):
            entry = self.entries.get((uri.scheme,) + given[:k])
            if entry is not None and (entry.has_path or k == len(given)):
                return entry
        return None

    def resolve(self, uri: Uri) -> Resolution:
        spec = self.specs.get(uri.scheme)
        if spec is None:
            raise ResolveError(f"no logical scheme {uri.scheme!r} in the configuration")
        if not uri.authority:
            raise ResolveError(f"{uri}: a logical URI needs a first part after //")
        given = (uri.authority,) + uri.segments
        n = len(spec.parts)
        named = dict(zip(spec.parts, given))
        if self.tenant is not None and spec.tenant and spec.tenant not in named:
            raise ResolveError(f"{uri} is too short to name its {spec.tenant}; this catalog is for {self.tenant}")
        if self.tenant is not None and spec.tenant and named[spec.tenant] != self.tenant:
            raise ResolveError(f"{uri}: {spec.tenant} {named[spec.tenant]!r} is another tenant; "
                               f"this catalog is for {self.tenant}")
        _check_parts(spec, uri, given)
        entry = self._match(uri, given)
        if entry is not None:
            return self._resolve_entry(uri, given, entry)
        if len(given) < n:
            shape = "/".join(spec.parts) + (f"/<{spec.rest}...>" if spec.rest else "")
            raise ResolveError(f"{uri} does not match {spec.name}://{shape}")

        def q(v: str) -> str:
            return quote(v, safe=_SAFE.replace("&", ""))

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
        # A ts: keeps the default branch, but a target without {ref} drops it.
        # The store that reads the time may need it, so it goes along.
        ts_branch = default.value if sel and sel.kind == "ts" and default and not _uses_ref(spec.target) else None
        return Resolution(uri, physical, tuple(via), ts_branch)

    def _selector(self, uri: Uri, via: list[str]) -> Selector | None:
        """The selector of the URI, with a stage looked up."""
        sel = uri.selector
        if sel and sel.kind == "stage":
            target = self._stage(uri, sel.value)
            via.append(f"stage:{sel.value} -> {target}")
            sel = target
        if sel and sel.kind == "ts":
            try:
                parse_ts(sel.value)
            except UriError as e:
                raise ResolveError(f"{uri}: {e}") from None
        return sel

    def _resolve_entry(self, uri: Uri, given: tuple[str, ...], entry: EntrySpec) -> Resolution:
        via: list[str] = []
        sel = self._selector(uri, via)
        if sel and sel.kind not in _REF_KINDS and sel.kind != "ts":
            raise ResolveError(f"{uri}: an entry takes branch:, tag:, commit:, ts: or stage:, not {sel.kind}:")
        safe = _SAFE.replace("&", "")
        path = "/".join(quote(v, safe=safe) for v in given[len(entry.parts):])
        ref = sel.value if sel and sel.kind in _REF_KINDS else entry.ref
        try:
            physical = parse(entry.format(path, quote(ref, safe=safe) if ref else None))
        except UriError as e:
            raise ResolveError(f"{uri} resolves to a bad URI through the entry {entry.key}: {e}") from e
        own, ts_branch = physical.selector, None
        if sel and own is not None and own.kind != "branch" and sel.kind != "commit":
            # A target fixed to a tag or a commit names one version. A run may
            # still fix another commit, but a branch or a time would move it.
            raise ResolveError(f"{uri}: the entry {entry.key} is fixed to {own}, so it takes only commit:, "
                               f"not {sel.kind}:")
        if sel and sel.kind in _REF_KINDS and not entry.has_ref:
            physical = physical.with_selector(sel)
        elif sel and sel.kind == "ts":
            physical = physical.with_selector(sel)
            ts_branch = own.value if own is not None else None
        via.append(f"entry {entry.key} -> {physical}")
        return Resolution(uri, physical, tuple(via), ts_branch)
