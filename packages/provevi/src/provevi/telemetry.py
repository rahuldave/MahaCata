"""The telemetry policy: what may record or send data from a run, besides the evidence.

During a run, provrun turns off each path by which a library records or
sends data, such as MLflow's autolog or LiteLLM's callbacks. The operator
approves exceptions in a policy file, which this module reads and checks::

    version: 1
    libraries:
      pydantic-ai:
        versions: ">=2.64,<2.65"    # the versions that were tested
        telemetry: [logfire]        # the destinations its instrumentation may feed
    destinations:
      logfire:
        kind: logfire               # logfire, otlp or mlflow
        receives: usage             # names, usage or bodies
    hosts:
      - api.anthropic.com           # a host a run may connect to
      - .amazonaws.com              # a leading dot: any host under the domain

A run file's ``telemetry`` section can narrow the policy with ``narrow``, but
never widen it. ``attributes_for`` gives the span attributes that a
destination receives at its level, and ``split_body`` takes the bodies out of
a span's attributes, so that the evidence keeps them as a payload.

provrun applies the policy. This module only reads, checks and describes it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .errors import ProveviError
from .events import sha256_bytes

#: The levels of what a destination receives, from least to most.
LEVELS = ("names", "usage", "bodies")
#: The kinds of destination that provrun can send spans to.
DESTINATION_KINDS = ("logfire", "otlp", "mlflow")
#: The policy file format that this provevi reads.
POLICY_VERSION = 1
#: The event type of an attempt that the policy blocked.
BLOCKED = "telemetry_blocked"

# A destination at the level "names" receives only these attributes. Every
# attribute not named here, or under usage, counts as a body, so a new
# attribute that a library adds never leaves the evidence by mistake.
_NAMES = frozenset({
    "gen_ai.operation.name", "gen_ai.system", "gen_ai.provider.name", "gen_ai.request.model",
    "gen_ai.response.model", "gen_ai.response.id", "gen_ai.response.finish_reasons", "gen_ai.agent.name",
    "gen_ai.tool.name", "gen_ai.tool.call.id", "mlflow.spanType", "mlflow.llm.model", "mlflow.llm.provider",
    "provrun.run_id", "provrun.step", "provrun.component", "error.type", "server.address", "server.port",
})
_USAGE = frozenset({
    "gen_ai.request.temperature", "gen_ai.request.max_tokens", "gen_ai.request.top_p",
    "mlflow.chat.tokenUsage", "mlflow.llm.cost",
})
_USAGE_PREFIXES = ("gen_ai.usage.",)
# A name is one word of these characters. A value with a space, or longer than
# this, may hold text from a prompt, so it counts as a body.
_NAME = re.compile(r"[A-Za-z0-9_.:/@+=-]{1,200}")
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _is_name(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return _NAME.fullmatch(value) is not None
    if isinstance(value, (list, tuple)):
        return len(value) <= 20 and all(isinstance(v, str) and _NAME.fullmatch(v) for v in value)
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_usage(value: Any) -> bool:
    if _is_number(value):
        return True
    if isinstance(value, Mapping):
        return all(isinstance(k, str) and _NAME.fullmatch(k) and _is_number(v) for k, v in value.items())
    if isinstance(value, str):
        # MLflow keeps a token count or a cost as a JSON object in a string.
        try:
            return _is_usage(json.loads(value)) and not isinstance(json.loads(value), str)
        except ValueError:
            return False
    return False


def level_of(key: Any, value: Any = None) -> str:
    """The lowest level at which a destination receives the attribute. With
    value, a names attribute must hold a name, and a usage attribute must hold
    numbers, or else the attribute counts as a body."""
    if not isinstance(key, str):
        return "bodies"
    if key in _NAMES:
        return "names" if value is None or _is_name(value) else "bodies"
    if key in _USAGE or key.startswith(_USAGE_PREFIXES):
        return "usage" if value is None or _is_usage(value) else "bodies"
    return "bodies"


def attributes_for(attributes: Mapping[str, Any], level: str) -> dict[str, Any]:
    """The attributes that a destination at level receives. Span events, such
    as an exception with its message, are bodies, and this function does not
    see them."""
    if level not in LEVELS:
        raise ProveviError(f"{level!r} is not a level; use one of {', '.join(LEVELS)}")
    top = LEVELS.index(level)
    return {k: v for k, v in attributes.items() if LEVELS.index(level_of(k, v)) <= top}


def split_body(attributes: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The attributes in two parts: those an event may keep, which are names
    and usage, and the bodies, which the evidence keeps as a payload."""
    kept, body = {}, {}
    for k, v in attributes.items():
        (body if level_of(k, v) == "bodies" else kept)[k] = v
    return kept, body


def blocked_data(path: str, *, library: str | None = None, detail: str = "") -> dict:
    """The data of a ``telemetry_blocked`` event. path names what was blocked,
    such as ``litellm.callbacks`` or ``network``; library is the distribution
    that tried it, when known."""
    if not isinstance(path, str) or not path:
        raise ProveviError("a blocked attempt needs a path, such as litellm.callbacks")
    data: dict = {"path": path, "detail": str(detail)}
    if library is not None:
        data["library"] = canonicalize_name(library)
    return data


@dataclass(frozen=True)
class Library:
    """A library whose telemetry the policy approves: the versions that were
    tested, and the destinations its instrumentation may feed."""

    name: str
    versions: str
    telemetry: tuple[str, ...] = ()

    def allows_version(self, version: str) -> bool:
        """True if version is one that the policy names. A local build, such
        as 2.64.0+patched, passes only if the specifier names it."""
        try:
            v = Version(version)
        except InvalidVersion:
            return False
        if v.local is not None and "+" not in self.versions:
            return False
        return v in SpecifierSet(self.versions)


@dataclass(frozen=True)
class Destination:
    """A place outside the evidence that spans may go to, and what it receives."""

    name: str
    kind: str
    receives: str
    endpoint: str | None = None


@dataclass(frozen=True)
class TelemetryPolicy:
    """What a run may record or send besides its evidence. A library or a
    destination that the policy does not name gets nothing, and a run may
    connect only to the hosts it names."""

    libraries: Mapping[str, Library] = field(default_factory=dict)
    destinations: Mapping[str, Destination] = field(default_factory=dict)
    hosts: tuple[str, ...] = ()
    source: str = "<none>"

    @classmethod
    def deny_all(cls) -> TelemetryPolicy:
        """The policy that approves nothing, for a run with no policy file."""
        return cls(source="<deny all>")

    @classmethod
    def load(cls, path: str | Path) -> TelemetryPolicy:
        """Read a policy file, in YAML or JSON. Raises ProveviError, with every
        problem the file has."""
        import yaml

        path = Path(path).expanduser()
        try:
            doc = load_yaml(path.read_text())
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise ProveviError(f"cannot read the telemetry policy {path}: {e}") from e
        return cls.from_dict(doc, source=str(path))

    @classmethod
    def from_dict(cls, doc: Any, source: str = "<dict>") -> TelemetryPolicy:
        """A policy from a parsed document. Raises ProveviError, with every problem."""
        problems: list[str] = []
        if not isinstance(doc, dict):
            raise ProveviError(f"{source}: a telemetry policy is a mapping")
        _keys(doc, {"version", "libraries", "destinations", "hosts"}, "the policy", problems)
        if type(doc.get("version")) is not int or doc.get("version") != POLICY_VERSION:
            problems.append(f"version is {doc.get('version')!r}; this provevi reads version {POLICY_VERSION}")
        hosts = _hosts(doc.get("hosts"), "hosts", problems)
        destinations = _destinations(doc.get("destinations"), hosts, problems)
        libraries = _libraries(doc.get("libraries"), set(destinations), problems)
        if problems:
            raise ProveviError(f"{source}: " + "; ".join(problems))
        return cls(libraries, destinations, hosts, source)

    def to_dict(self) -> dict:
        """The policy as a document that from_dict reads back."""
        return {
            "version": POLICY_VERSION,
            "libraries": {n: {"versions": lib.versions, "telemetry": list(lib.telemetry)}
                          for n, lib in sorted(self.libraries.items())},
            "destinations": {n: {k: v for k, v in (("kind", d.kind), ("receives", d.receives),
                                                    ("endpoint", d.endpoint)) if v is not None}
                             for n, d in sorted(self.destinations.items())},
            "hosts": list(self.hosts),
        }

    def digest(self) -> str:
        """The SHA-256 of the policy's canonical JSON, for a run to record
        which policy it ran under."""
        return sha256_bytes(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode())

    def library(self, name: str) -> Library | None:
        return self.libraries.get(canonicalize_name(name))

    def destinations_for(self, library: str) -> list[Destination]:
        """The destinations that the library's instrumentation may feed."""
        lib = self.library(library)
        return [] if lib is None else [self.destinations[d] for d in lib.telemetry]

    def allows_host(self, host: str) -> bool:
        """True if a run may connect to host. An entry with a leading dot
        allows each host under that domain, but not the domain itself."""
        return _allowed(host, self.hosts)

    def check_installed(self, versions: Mapping[str, str] | None = None) -> list[str]:
        """A problem for each approved library that is installed at a version
        the policy does not name. versions maps a distribution to its version;
        by default, the versions installed in this Python."""
        problems = []
        for name, lib in sorted(self.libraries.items()):
            if versions is not None:
                found = {canonicalize_name(k): v for k, v in versions.items()}.get(name)
            else:
                try:
                    found = metadata.version(name)
                except metadata.PackageNotFoundError:
                    found = None
            if found is not None and not lib.allows_version(found):
                problems.append(f"{name} {found} is installed, but the policy approves {lib.versions}")
        return problems

    def narrow(self, section: Any, source: str = "the run file") -> TelemetryPolicy:
        """The policy narrowed by a run file's telemetry section. A key that the
        section leaves out keeps the policy's value. Raises ProveviError if the
        section would widen the policy in any way."""
        if section is None:
            return self
        if not isinstance(section, dict):
            raise ProveviError(f"{source}: the telemetry section is a mapping")
        problems: list[str] = []
        _keys(section, {"libraries", "destinations", "hosts"}, "the telemetry section", problems)
        destinations = dict(self.destinations)
        if "destinations" in section:
            destinations = {}
            given = section["destinations"]
            if not isinstance(given, dict):
                problems.append("destinations is a mapping of names")
                given = {}
            for name, spec in given.items():
                base = self.destinations.get(name)
                if base is None:
                    problems.append(f"destination {name!r} is not in the policy")
                    continue
                spec = {} if spec is None else spec
                if not isinstance(spec, dict):
                    problems.append(f"destination {name!r} is a mapping")
                    continue
                _keys(spec, {"receives"}, f"destination {name!r}", problems)
                receives = spec.get("receives", base.receives)
                if receives not in LEVELS:
                    problems.append(f"destination {name!r} receives {receives!r}; use one of {', '.join(LEVELS)}")
                elif LEVELS.index(receives) > LEVELS.index(base.receives):
                    problems.append(f"destination {name!r} would receive {receives}, more than the policy's "
                                    f"{base.receives}")
                else:
                    destinations[name] = Destination(name, base.kind, receives, base.endpoint)
        libraries = {n: lib for n, lib in self.libraries.items()}
        if "libraries" in section:
            libraries = {}
            given = section["libraries"]
            if not isinstance(given, dict):
                problems.append("libraries is a mapping of names")
                given = {}
            for raw, spec in given.items():
                name = canonicalize_name(str(raw))
                if name in libraries:
                    problems.append(f"library {raw!r} is named twice")
                    continue
                base = self.libraries.get(name)
                if base is None:
                    problems.append(f"library {raw!r} is not in the policy")
                    continue
                spec = {} if spec is None else spec
                if not isinstance(spec, dict):
                    problems.append(f"library {raw!r} is a mapping")
                    continue
                _keys(spec, {"telemetry"}, f"library {raw!r}", problems)
                telemetry = _names(spec.get("telemetry", list(base.telemetry)), f"library {raw!r} telemetry",
                                   problems)
                for d in telemetry:
                    if d not in base.telemetry:
                        problems.append(f"library {raw!r} may not feed {d!r} under the policy")
                libraries[name] = Library(name, base.versions, tuple(d for d in telemetry if d in base.telemetry))
        # A library keeps only the destinations that are still there.
        libraries = {n: Library(n, lib.versions, tuple(d for d in lib.telemetry if d in destinations))
                     for n, lib in libraries.items()}
        hosts = self.hosts
        if "hosts" in section:
            hosts = _hosts(section["hosts"], "hosts", problems)
            for h in hosts:
                if not self._covers(h):
                    problems.append(f"host {h!r} is not allowed by the policy")
        if problems:
            raise ProveviError(f"{source}: " + "; ".join(problems))
        return TelemetryPolicy(libraries, destinations, hosts, f"{self.source}, narrowed by {source}")

    def _covers(self, entry: str) -> bool:
        if entry.startswith("."):
            return any(e.startswith(".") and (entry == e or entry.endswith(e)) for e in self.hosts)
        return self.allows_host(entry)


def _allowed(host: str, entries: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return bool(host) and any(host == e or (e.startswith(".") and host.endswith(e)) for e in entries)


def _no_duplicates(loader, node, deep=False):
    import yaml

    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(None, None, f"the key {key!r} is named twice",
                                                    key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


def load_yaml(text: str) -> Any:
    """Parse YAML safely, and refuse a key that a mapping names twice, which
    plain YAML loaders allow, keeping the last value."""
    import yaml

    class StrictLoader(yaml.SafeLoader):
        pass

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)
    return yaml.load(text, Loader=StrictLoader)


def _keys(doc: dict, allowed: set[str], what: str, problems: list[str]) -> None:
    for k in doc:
        if k not in allowed:
            problems.append(f"{what} has an unknown key {k!r}")


def _names(value: Any, what: str, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{what} is a list of names")
        return []
    return value


def _hosts(value: Any, what: str, problems: list[str]) -> tuple[str, ...]:
    hosts = []
    for raw in _names(value, what, problems):
        h = raw.lower()
        dotted = h.startswith(".")
        labels = (h[1:] if dotted else h).split(".")
        if (not all(_LABEL.fullmatch(label) for label in labels)) or (dotted and len(labels) < 2):
            problems.append(f"{what}: {raw!r} is not a host name; write api.example.com, or .example.com "
                            "for each host under a domain")
        else:
            hosts.append(h)
    return tuple(hosts)


def _destinations(value: Any, hosts: tuple[str, ...], problems: list[str]) -> dict[str, Destination]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append("destinations is a mapping of names")
        return {}
    out = {}
    for name, spec in value.items():
        if not isinstance(spec, dict):
            problems.append(f"destination {name!r} is a mapping")
            continue
        _keys(spec, {"kind", "receives", "endpoint"}, f"destination {name!r}", problems)
        kind, receives, endpoint = spec.get("kind"), spec.get("receives"), spec.get("endpoint")
        ok = True
        if kind not in DESTINATION_KINDS:
            problems.append(f"destination {name!r} has kind {kind!r}; use one of {', '.join(DESTINATION_KINDS)}")
            ok = False
        if receives not in LEVELS:
            problems.append(f"destination {name!r} receives {receives!r}; use one of {', '.join(LEVELS)}")
            ok = False
        if kind == "mlflow" and endpoint is None:
            pass  # the copy in MLflow goes into the store's own file
        elif not isinstance(endpoint, str) or urlsplit(endpoint).scheme not in ("http", "https"):
            problems.append(f"destination {name!r} needs an http or https endpoint, not {endpoint!r}")
            ok = False
        elif not _allowed(urlsplit(endpoint).hostname or "", hosts):
            problems.append(f"destination {name!r} sends to {urlsplit(endpoint).hostname!r}, which is not in hosts")
            ok = False
        if ok:
            out[str(name)] = Destination(str(name), kind, receives, endpoint)
    return out


def _libraries(value: Any, destinations: set[str], problems: list[str]) -> dict[str, Library]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append("libraries is a mapping of names")
        return {}
    out: dict[str, Library] = {}
    for raw, spec in value.items():
        name = canonicalize_name(str(raw))
        if name in out:
            problems.append(f"library {raw!r} is named twice")
            continue
        if not isinstance(spec, dict):
            problems.append(f"library {raw!r} is a mapping")
            continue
        _keys(spec, {"versions", "telemetry"}, f"library {raw!r}", problems)
        versions = spec.get("versions")
        try:
            if not isinstance(versions, str) or not versions.strip():
                raise InvalidSpecifier(repr(versions))
            SpecifierSet(versions)
        except InvalidSpecifier:
            problems.append(f"library {raw!r} needs versions, such as \">=2.64,<2.65\", not {versions!r}")
            continue
        telemetry = _names(spec.get("telemetry"), f"library {raw!r} telemetry", problems)
        for d in telemetry:
            if d not in destinations:
                problems.append(f"library {raw!r} feeds {d!r}, which is not a destination")
        out[name] = Library(name, versions, tuple(d for d in telemetry if d in destinations))
    return out
