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
        kind: logfire               # logfire, otlp, mlflow or live
        receives: usage             # names, usage or bodies
        endpoint: https://logfire-us.pydantic.dev
    hosts:
      - logfire-us.pydantic.dev     # a host that a destination may send to
      - .amazonaws.com              # a leading dot: any host under the domain

A run file's ``telemetry`` section can narrow the policy with ``narrow``, but
never widen it. ``attributes_for`` gives the span attributes that a
destination receives at its level, and ``split_body`` takes the bodies out of
a span's attributes, so that the evidence keeps them as a payload.
``match_destination`` finds the destination that a telemetry variable, such
as ``OTEL_EXPORTER_OTLP_ENDPOINT``, points at. ``otlp_traces_target`` gives
the protocol and the address to which provrun sends traces over OTLP.

The ``hosts`` list names the hosts that destinations may send to. The policy
checks each destination's endpoint against it, and the list is also a record
for the firewall. Neither provevi nor provrun limits a run's connections. That
belongs to the firewall or the container that runs it.

provrun applies the policy. This module only reads, checks and describes it.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, Collection, Mapping
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .errors import ProveviError
from .events import sha256_bytes

__all__ = [
    "BLOCKED", "LEVELS", "MLFLOW_DATABASES", "MODEL_KEYS", "PROBLEM", "RUN_MODEL_KEYS", "Destination", "Library",
    "TelemetryPolicy", "attributes_for", "blocked_data", "distribution_of", "load_yaml", "normal_endpoint",
    "otlp_traces_target", "plain_attribute", "plain_experiment", "plain_name", "plain_span_name", "span_libraries",
    "split_body",
]

#: The levels of what a destination receives, from least to most.
LEVELS = ("names", "usage", "bodies")
#: The kinds of destination that provrun can send spans to. A ``live``
#: destination is a viewer of the live feed of a run, such as a browser that a
#: program streams the run to. It has no endpoint, and no library feeds it.
DESTINATION_KINDS = ("logfire", "otlp", "mlflow", "live")
#: The policy file format that this provevi reads.
POLICY_VERSION = 1
#: The event type of an attempt that the policy blocked.
BLOCKED = "telemetry_blocked"
#: The event type of a problem that a run found after it started, such as a
#: library that set up its own telemetry.
PROBLEM = "telemetry_problem"
#: The database schemes that an mlflow destination may name, as SQLAlchemy
#: writes them, with or without a driver, as in postgresql+psycopg.
MLFLOW_DATABASES = ("postgresql", "mysql", "mssql", "sqlite")
_DEFAULT_PORTS = {"http": 80, "https": 443, "postgresql": 5432, "mysql": 3306, "mssql": 1433}

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


def attributes_for(
    attributes: Mapping[str, Any],  # A span's attributes
    level: str,  # The destination's level, one of `LEVELS`
) -> dict[str, Any]:  # The attributes it receives at that level
    """Cut a span's attributes to those that a destination at a level receives.

    Span events, such as an exception with its message, are bodies, and this function does not see them.
    Raises ProveviError for a level that is not one."""
    if level not in LEVELS:
        raise ProveviError(f"{level!r} is not a level; use one of {', '.join(LEVELS)}")
    top = LEVELS.index(level)
    return {k: v for k, v in attributes.items() if LEVELS.index(level_of(k, v)) <= top}


def distribution_of(
    module: str,  # A module's dotted name, such as `opentelemetry.sdk.trace`
) -> str | None:  # The distribution's name, as pip knows it, such as `opentelemetry-sdk`, or a guess; `None` for an empty name
    """Find the distribution that installed a module, to name a library in a report.

    When it cannot tell, it guesses: a module that no distribution installed gives its own top name, as a
    program may name its own. To decide what the policy approves, use ``span_libraries``, which does not
    guess."""
    top = module.split(".")[0]
    if not top:
        return None
    try:
        names = metadata.packages_distributions().get(top, [])
    except Exception:  # a broken install elsewhere; the module still has a name
        return top
    return _installed_by(module, names) or (names[0] if names else top)


def _installed_by(module: str, names: list[str], *, strict: bool = False) -> str | None:
    """Which of names, the distributions that ship module's top package,
    installed module, or None if none of them can be shown to. strict asks
    that the distribution's files hold module itself, not only a package
    above it, as pydantic_ai holds no module pydantic_ai.evil."""
    if len(names) == 1 and not strict:
        return names[0]
    # A namespace package, such as opentelemetry, comes from many
    # distributions. The one whose files hold the longest part of the module's
    # path installed it, so mlflow.sklearn goes to the one that ships it.
    parts = module.split(".")
    held = {}
    for name in names:
        held[name] = _files(name)
    for n in range(len(parts), len(parts) - 1 if strict else 0, -1):
        stem = "/".join(parts[:n])
        for name in names:
            if f"{stem}.py" in held[name] or f"{stem}/__init__.py" in held[name]:
                return name
    return None


@functools.cache
def _files(name: str) -> frozenset[str]:
    """The files of an installed distribution, with / between parts. A
    process reads them once, as reading them takes a while."""
    try:
        return frozenset(str(f).replace("\\", "/") for f in metadata.files(name) or ())
    except Exception:  # a broken install names no files
        return frozenset()


@functools.cache
def _shipped() -> Mapping[str, list[str]] | None:
    """Each top package, and the distributions that ship it, read once in a
    process; None if the installed packages cannot be read."""
    try:
        return metadata.packages_distributions()
    except Exception:  # a broken install elsewhere
        return None


@functools.lru_cache(maxsize=1024)  # a library may make many scope names
def span_libraries(
    scope: str,  # A span's instrumentation scope name, such as `pydantic_ai`
) -> tuple[str, ...]:  # The library names, as canonical distribution names; empty when none can be shown
    """Find the names that the policy may know a span's library by.

    It gives the scope name only if a distribution of that name is installed and the name has no dot, and the
    distribution whose files hold the scope's module. It does not guess: a scope whose module no installed
    distribution holds, or any scope when the installed packages cannot be read, gives no name, so the policy
    approves none of its spans. A process reads the installed packages once, and does not see a change to them
    after that; ``span_libraries.cache_clear()`` and the same call on ``_shipped`` and ``_files`` make it read
    them again."""
    if not isinstance(scope, str) or not scope:
        return ()
    module = scope.replace("-", "_")
    shipped = _shipped()
    if shipped is None:  # a broken install: nothing can be shown
        return ()
    found = []
    # A dotted scope is a module path; pydantic.ai would else find the
    # distribution pydantic-ai, as its lookup reads . as -.
    if "." not in scope:
        try:
            found.append(canonicalize_name(metadata.distribution(scope).metadata["Name"]))
        except Exception:  # no distribution of that name
            pass
    dist = _installed_by(module, shipped.get(module.split(".")[0], []), strict=True)
    if dist is not None and canonicalize_name(dist) not in found:
        found.append(canonicalize_name(dist))
    return tuple(found)


# What a plain name may not hold: a URL, a user and password, a setting such
# as token=..., or a path from the root. Nor may any part before a "/" be a
# URL's scheme, a host or an email, such as https:/host/x, x/host.com/a or
# x/user@host.com/a, nor a host and a port, such as db:5432/x. hf.co,
# huggingface.co, modelscope.cn and registry.ollama.ai start model names in
# Ollama, so they may. A list of schemes cannot hold every one. PydanticAI's groq:meta-llama/x is not a scheme, as
# its part does not end in ":" and groq is not a scheme of storage or the web.
_NOT_PLAIN = re.compile(
    r"://|(^|/)[^/@]*:[^/@]*@|(^|/)[^/@]*@[^/@]*:|=|^/|(^|/)\.\.?(/|$)"
    r"|(^|/)[A-Za-z][A-Za-z0-9+.-]*:/|(^|/)(https?|s3a?|gs|gcs|az|abfss?|wasbs?|adls?|file|ftps?|sftp|ssh|git"
    r"|hdfs|dbfs|r2|oss|cos|b2|lakefs[a-z0-9-]*):"
    r"|(^|/)(?!(hf\.co|huggingface\.co|modelscope\.cn|registry\.ollama\.ai)/)([A-Za-z0-9_-]+\.)+[A-Za-z]{2,}\.?"
    r"(:\d+)?/|(^|/)[0-9.]*\.[0-9.]*(:\d+)?/|(^|/)[0-9A-F]*:[0-9A-F:]*:[0-9A-F]*/|(^|/)[^/:]+:\d+/"
    r"|(^|/)localhost\.?(:\d+)?/|(^|/)[^/@]+@[^/]+/",
    re.IGNORECASE)
# A path that does not start with "/" but is still a path: one with a hidden
# part, such as home/someone/.ssh/id_rsa; one from a drive, such as C:/x; one
# from a folder of the root, such as etc/passwd or run/secrets/x; one through
# a home, such as data/home/someone/x; and one that ends in a file of keys or
# settings, such as x/key.pem, x/key.pem.bak or x/id_rsa. A model's name is
# none of these.
_PATH = re.compile(
    r"(^|/)\.[^/]*/|/\.|^[A-Za-z]:.*/"
    r"|^(home|users|root|etc|private|volumes|tmp|var)/|(^|/)(home|users|cygdrive|windows)/"
    r"|^(opt|srv|mnt|run|proc|dev|sys|usr|snap|nix|media|boot)/[^/]*/"
    r"|/(id_(rsa|ed25519|ecdsa|dsa)[^/]*|credentials[^/]*|creds[^/]*|authorized_keys|known_hosts|passwd|shadow|sam"
    r"|htpasswd|kubeconfig|secrets?)$"
    r"|/[^/]*\.(pem|key|p12|pfx|ppk|crt|der|gpg|asc|env|json|ya?ml|toml|ini|cfg|conf|properties|txt|netrc|kdbx"
    r"|db|sqlite|tfstate|csv)(\.[^/]*)?$",
    re.IGNORECASE)
# A common API key or token, by its prefix and the run of characters that
# follows it, so that a name such as hf_transfer or sk-learn is not one:
# OpenAI and Anthropic (sk-), Stripe (sk_, rk_, pk_ with live or test),
# GitHub (ghp_ and the like, github_pat_), GitLab (glpat-), Slack (xox?-,
# xapp-), AWS access keys (AKIA, ASIA), Google (AIza), Hugging Face (hf_),
# npm (npm_), webhook secrets (whsec_) and JSON web tokens (eyJ). A shape
# test cannot catch every secret.
_KEY = re.compile(r"(^|[^A-Za-z0-9])(sk-[A-Za-z0-9_-]{16,}|[srp]k_(live|test)_[A-Za-z0-9]{10,}"
                  r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
                  r"|xox[abposre]-[A-Za-z0-9-]{10,}|xapp-[A-Za-z0-9-]{10,}|(AKIA|ASIA)[A-Z0-9]{16}"
                  r"|AIza[A-Za-z0-9_-]{30,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{30,}|whsec_[A-Za-z0-9]{20,}"
                  r"|eyJ[A-Za-z0-9_-]{10,})")
# A span name below bodies: one word, or two, such as "chat gpt-4o".
_SPAN_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,200}( [A-Za-z0-9_.:-]{1,200})?")


def plain_name(
    value: Any,  # A value from a span or an event: a text, a whole number, a yes or no, or a list of texts
    *,
    declared: Collection[str] = (),  # Texts that may have more than one `/`, such as a model's name; not one text
) -> bool:  # Whether a place below bodies may get the value
    """Tell whether a value is a plain name, which a place below bodies may get.

    A plain name is a name, as the levels say, that holds no URL, no user and password, no setting such as
    ``token=x``, no path from ``/``, no path that looks like one of a machine's files, such as
    ``home/someone/.ssh/id_rsa`` or ``x/key.pem``, no part before a ``/`` that is a URL's scheme, a host or an
    email, such as ``https:/host/x`` or ``x/host.com/a``, and no more than one ``/``, and none that starts
    like a common API key, such as ``sk-`` or ``ghp_``. A secret or a path can pass the levels' test of a
    name, so a place below bodies should get only plain names. A name such as ``ns:model@tag`` looks like
    ``user:password@host``, so it is not plain. A whole number and a yes or no are plain, as they are names; a
    number with a fraction is not, nor is an empty list.

    ``org/model`` is plain, and ``tmp/a/b`` is not. A model name such as ``openrouter/meta-llama/llama-3-70b``
    has two ``/`` too, so a text in declared may have more. It must pass every other test. Raises TypeError
    when declared is one text."""
    if isinstance(declared, str):
        raise TypeError("declared must be a list of texts, not one text")
    if not _is_name(value) or (isinstance(value, (list, tuple)) and not value):
        return False
    texts = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
    return not any(_NOT_PLAIN.search(t) or _PATH.search(t) or _KEY.search(t)
                   or (t.count("/") > 1 and t not in declared) for t in texts)


#: The keys of a library's span whose value is a model's name, which may have
#: more than one ``/``. A library's plain ``model`` key may hold anything, such
#: as a folder of weights, so it is not one of them.
MODEL_KEYS = frozenset({"gen_ai.request.model", "gen_ai.response.model", "mlflow.llm.model"})

#: The keys of provrun's own events and settings whose value is a model's
#: name: MODEL_KEYS, ``model``, which a run file names, and ``answered_by``,
#: the model that the provider says answered.
RUN_MODEL_KEYS = MODEL_KEYS | {"model", "answered_by"}


def plain_attribute(
    key: str,  # The attribute's or the data's key
    value: Any,  # Its value
    *,
    model_keys: Collection[str] = MODEL_KEYS,  # The keys whose value is a model's name; RUN_MODEL_KEYS for provrun's own events
) -> bool:  # Whether a place below bodies may get the value
    """Tell whether a value is a plain name for its key.

    A model's name, under one of model_keys, may have more than one ``/``, as
    ``openrouter/meta-llama/llama-3-70b`` or a Vertex model's path has, and so may each name in a list of
    them. Any other value is a plain name as plain_name says."""
    declared: list[str] = []
    if key in model_keys:
        if isinstance(value, str):
            declared = [value]
        elif isinstance(value, (list, tuple)):
            declared = [v for v in value if isinstance(v, str)]
    return plain_name(value, declared=declared)


# An experiment's URI, or its pin: experiment://, then parts of name
# characters, then perhaps ?at=commit: and a hash.
_EXPERIMENT = re.compile(r"experiment://([A-Za-z0-9_][A-Za-z0-9_.-]{0,199}/){0,15}[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}"
                         r"(\?at=commit:[0-9a-f]{7,128})?")


def plain_experiment(
    value: Any,  # An experiment's URI or pin, from an event's data
) -> bool:  # Whether a place below bodies may get the value
    """Tell whether an experiment's URI or pin is one that a place below bodies may get.

    One such is ``experiment://acme/risk/dev/credit/q?at=commit:7311e8...``. Its path, and each part of it, is
    a plain name, though the path may have more than one ``/``, and the pin names a commit. A URI with a
    branch, a host or any other setting is not plain."""
    if not isinstance(value, str) or _EXPERIMENT.fullmatch(value) is None:
        return False
    path = value[len("experiment://"):].split("?", 1)[0]
    # A host's name is caught only before a "/", so each part is tested with one after it.
    return (plain_name(path, declared=[path]) and plain_name(path + "/x", declared=[path + "/x"])
            and all(plain_name(p) for p in path.split("/")))


def plain_span_name(
    name: Any,  # A span's name
) -> bool:  # Whether a place below bodies may get the name
    """Tell whether a span's name is one that a place below bodies may get.

    It must be one or two words of name characters, such as ``chat gpt-4o``, each a plain name."""
    return (isinstance(name, str) and _SPAN_NAME.fullmatch(name) is not None
            and all(plain_name(w) for w in name.split(" ")))


def split_body(
    attributes: Mapping[str, Any],  # A span's attributes
) -> tuple[dict[str, Any], dict[str, Any]]:  # The names and usage, then the bodies
    """Split a span's attributes into what an event may keep and the bodies.

    An event keeps the names and the usage; the evidence keeps the bodies as a payload."""
    kept, body = {}, {}
    for k, v in attributes.items():
        (body if level_of(k, v) == "bodies" else kept)[k] = v
    return kept, body


def blocked_data(
    path: str,  # What was blocked, such as `litellm.callbacks` or `network`; not empty
    *,
    library: str | None = None,  # The distribution that tried it, when known; it is stored in its canonical form
    detail: str = "",  # More about the attempt
) -> dict:  # `path`, `detail`, and `library` when it is given
    """The data of a ``telemetry_blocked`` event. Raises ProveviError for an empty path."""
    if not isinstance(path, str) or not path:
        raise ProveviError("a blocked attempt needs a path, such as litellm.callbacks")
    data: dict = {"path": path, "detail": str(detail)}
    if library is not None:
        data["library"] = canonicalize_name(library)
    return data


@dataclass(frozen=True)
class Library:
    """A library whose telemetry the policy approves."""

    name: str  # The distribution's canonical name, such as `pydantic-ai`
    versions: str  # The tested versions, as a specifier such as `>=2.64,<2.65`
    telemetry: tuple[str, ...] = ()  # The names of the destinations its instrumentation may feed

    def allows_version(
        self,
        version: str,  # An installed version, such as `2.64.0`
    ) -> bool:  # Whether `versions` names it
        """Tell whether the policy names a version of the library.

        A local build, such as 2.64.0+patched, passes only if the specifier names it. A version that is not
        one gives False."""
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

    name: str  # The destination's name in the policy
    kind: str  # `logfire`, `otlp`, `mlflow` or `live`
    receives: str  # Its level, one of `LEVELS`
    endpoint: str | None = None  # The URL it sends to; `None` for a live viewer, or an mlflow copy in the store's own file


@dataclass(frozen=True)
class TelemetryPolicy:
    """What a run may record or send besides its evidence.

    A library or a destination that the policy does not name gets nothing. Each destination's endpoint must be
    on a host that ``hosts`` names."""

    libraries: Mapping[str, Library] = field(default_factory=dict)  # The approved libraries, by canonical name
    destinations: Mapping[str, Destination] = field(default_factory=dict)  # The destinations, by name
    hosts: tuple[str, ...] = ()  # The hosts that destinations may send to; a leading dot names each host under a domain
    source: str = "<none>"  # Where the policy came from, such as its file's path, for messages

    @classmethod
    def deny_all(cls) -> TelemetryPolicy:  # A policy with no libraries, destinations or hosts
        """The policy that approves nothing, for a run with no policy file."""
        return cls(source="<deny all>")

    @classmethod
    def load(
        cls,
        path: str | Path,  # The policy file; `~` is the home folder
    ) -> TelemetryPolicy:  # The policy, with the path as its source
        """Read a policy file, in YAML or JSON.

        Raises ProveviError, with every problem the file has."""
        import yaml

        path = Path(path).expanduser()
        try:
            doc = load_yaml(path.read_text())
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            raise ProveviError(f"cannot read the telemetry policy {path}: {e}") from e
        return cls.from_dict(doc, source=str(path))

    @classmethod
    def from_dict(
        cls,
        doc: Any,  # The parsed policy document: a mapping with `version`, `libraries`, `destinations` and `hosts`
        source: str = "<dict>",  # Where the document came from, for messages
    ) -> TelemetryPolicy:  # The policy
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
        for lib in libraries.values():
            for d in lib.telemetry:
                if destinations[d].kind == "live":
                    problems.append(f"library {lib.name!r} feeds {d!r}, a live viewer; a library's spans reach "
                                    f"the viewer as span_recorded events")
        if problems:
            raise ProveviError(f"{source}: " + "; ".join(problems))
        return cls(libraries, destinations, hosts, source)

    def to_dict(self) -> dict:  # `version`, `libraries` and `destinations` in name order, and `hosts` in the order given
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

    def digest(self) -> str:  # 64 lowercase hex digits
        """The SHA-256 of the policy's canonical JSON, for a run to record which policy it ran under."""
        return sha256_bytes(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode())

    def library(
        self,
        name: str,  # A distribution's name, in any form pip accepts, such as `pydantic_ai`
    ) -> Library | None:  # The library; `None` when the policy does not approve it
        """The approved library of that name."""
        return self.libraries.get(canonicalize_name(name))

    def destinations_for(
        self,
        library: str,  # A distribution's name
    ) -> list[Destination]:  # The destinations; empty for a library the policy does not approve
        """The destinations that the library's instrumentation may feed."""
        lib = self.library(library)
        return [] if lib is None else [self.destinations[d] for d in lib.telemetry]

    def live_level(self) -> str | None:  # One of `LEVELS`, or `None`
        """The level that a viewer of a run's live feed receives.

        None if the policy names no live viewer, so a run shows nothing live."""
        return next((d.receives for d in self.destinations.values() if d.kind == "live"), None)

    def allows_host(
        self,
        host: str,  # A host name; case and a trailing dot are ignored
    ) -> bool:  # Whether the host may be sent to
        """Tell whether the policy's hosts name a host.

        An entry with a leading dot names each host under that domain, but not the domain itself."""
        return _allowed(host, self.hosts)

    def match_destination(
        self,
        kind: str,  # The destination's kind, such as `otlp` or `mlflow`
        url: str | None,  # The address a telemetry variable names; `None` for the store's own file
    ) -> Destination | None:  # The destination; `None` when none matches
        """Find the destination of a kind whose endpoint is a URL.

        Both are made regular first by ``normal_endpoint``. An mlflow destination with no endpoint, the copy
        in the store's own file, matches url None. A url that cannot be made regular matches nothing."""
        if url is None:
            return next((d for d in self.destinations.values() if d.kind == kind and d.endpoint is None), None)
        try:
            want = normal_endpoint(url)
        except ProveviError:
            return None
        for d in self.destinations.values():
            if d.kind == kind and d.endpoint is not None and normal_endpoint(d.endpoint) == want:
                return d
        return None

    def check_installed(
        self,
        versions: Mapping[str, str] | None = None,  # Each distribution's version; `None` for the versions installed in this Python
    ) -> list[str]:  # One problem each; empty when every approved library is at a version the policy names, or is not installed
        """Find each approved library that is installed at a version the policy does not name."""
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

    def narrow(
        self,
        section: Any,  # A run file's parsed `telemetry` section, with `libraries`, `destinations` and `hosts`; `None` keeps the policy
        source: str = "the run file",  # Where the section came from, for messages and the new source
    ) -> TelemetryPolicy:  # The narrowed policy
        """Narrow the policy by a run file's telemetry section.

        A key that the section leaves out keeps the policy's value. Raises ProveviError if the section would
        widen the policy in any way."""
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
            # A destination on a host that the section cut goes too, as it
            # would send past the run's hosts; its libraries no longer feed it.
            cut = {n for n, d in destinations.items()
                   if d.endpoint is not None and _endpoint_problem(d.name, d.kind, d.endpoint, hosts) is not None}
            # One that the section names itself is a mistake, not a cut.
            named = set(section.get("destinations") or {}) if isinstance(section.get("destinations"), dict) else set()
            for spec in (section.get("libraries") or {}).values() if isinstance(section.get("libraries"), dict) else ():
                if isinstance(spec, dict) and isinstance(spec.get("telemetry"), list):
                    named.update(d for d in spec["telemetry"] if isinstance(d, str))
            for n in sorted(cut & named):
                problems.append(f"destination {n!r} sends to a host that the section cuts from hosts")
            destinations = {n: d for n, d in destinations.items() if n not in cut}
            libraries = {n: Library(n, lib.versions, tuple(d for d in lib.telemetry if d in destinations))
                         for n, lib in libraries.items()}
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


def load_yaml(
    text: str,  # A YAML or JSON document
) -> Any:  # The parsed document
    """Parse YAML safely, and refuse a key that a mapping names twice.

    Plain YAML loaders allow such a key, and keep the last value."""
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
        elif kind == "live":
            if endpoint is not None:
                problems.append(f"destination {name!r} is a live viewer, which has no endpoint")
                ok = False
        elif not isinstance(endpoint, str):
            what = "an http, https or database" if kind == "mlflow" else "an http or https"
            problems.append(f"destination {name!r} needs {what} endpoint, not {endpoint!r}")
            ok = False
        else:
            problem = _endpoint_problem(str(name), kind, endpoint, hosts)
            if problem:
                problems.append(problem)
                ok = False
        if ok:
            out[str(name)] = Destination(str(name), kind, receives, endpoint)
    live = sorted(n for n, d in out.items() if d.kind == "live")
    if len(live) > 1:
        problems.append(f"destinations {', '.join(map(repr, live))} are both live viewers; name one")
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



def _scheme(url: str) -> str:
    scheme = urlsplit(url).scheme.lower()
    base = scheme.split("+", 1)[0]
    # Only a database URL names a driver, as postgresql+psycopg does.
    return base if base in MLFLOW_DATABASES else scheme


_BAD_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f#]")
_HOST = re.compile(r"[a-z0-9.-]+|\[[0-9a-f:.]+\]")
_PROTOCOLS = ("grpc", "http/protobuf", "http/json")
# The settings that a database URL may carry, each with the values it takes.
# None of them names a server, a socket or a file, so the URL's place stays
# its host. The normal form drops them.
_SAFE_QUERY = {
    "postgresql": {"sslmode": re.compile(r"disable|allow|prefer|require|verify-ca|verify-full"),
                   "connect_timeout": re.compile(r"[0-9]{1,4}"),
                   "application_name": re.compile(r"[A-Za-z0-9_.-]{1,64}")},
    "mysql": {"charset": re.compile(r"[a-z0-9_]{1,32}"), "connect_timeout": re.compile(r"[0-9]{1,4}")},
}


_KEPT_QUERY = ("sslmode=",)


def _safe_query(scheme: str, query: str) -> bool:
    allowed = _SAFE_QUERY.get(scheme, {})
    seen = set()
    for item in query.split("&"):
        key, eq, value = item.partition("=")
        if not eq or key in seen or key not in allowed or not allowed[key].fullmatch(value):
            return False
        seen.add(key)
    return True


def normal_endpoint(
    url: str,  # An http, https or database URL, such as a destination's endpoint
) -> str:  # The URL in its regular form, such as `https://host:443/path`
    """Write a URL in one regular form, so that two ways of writing one address are equal.

    The scheme and the host are lowercased, and the default port is added. A trailing / is dropped, except
    from a sqlite path. So are a database driver such as +psycopg, the user and password of a database URL,
    and the allowed settings of its query but sslmode, which stays, so that a policy can ask for TLS. A sqlite
    URL keeps only its path.

    Raises ProveviError for anything that two URL parsers could read as two different places: a backslash, a
    space, a control character or a #; a host that is not ASCII, because two standards of international names
    read some hosts differently; a user in an http or https URL; a % in the host; a port of 0 or an empty
    port; a path with . or .. segments or //; a query, which can name another server, as ?host= does, except
    the settings in _SAFE_QUERY, such as sslmode=require; a host list; a relative sqlite path; an mssql URL
    with no port or no database, which ODBC can read as the name of a data source. Also for a URL that is not
    http, https or a database."""
    if not isinstance(url, str) or _BAD_CHARS.search(url):
        raise ProveviError(f"{url!r}: an endpoint has no backslash, space, control character or #")
    if not url.isascii():
        raise ProveviError(f"{url!r}: write an endpoint in ASCII, with a host in its punycode form")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ProveviError(f"{url!r} is not a URL: {e}") from None
    scheme = _scheme(url)
    database = scheme in MLFLOW_DATABASES
    if scheme == "sqlite":
        path = parts.path
        if parts.netloc or parts.query or not path.startswith("//"):
            raise ProveviError(f"{url!r}: write a sqlite URL with an absolute path, as sqlite:////srv/mlflow.db")
        return "sqlite:///" + _path(url, path[1:], keep_slash=True)
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise ProveviError(f"{url!r} is not an http, https or database URL with a host")
    if "@" in parts.netloc and not database:
        raise ProveviError(f"{url!r}: an http or https endpoint names no user or password")
    if url.endswith("?") or (parts.query and not _safe_query(scheme, parts.query)):
        allowed = ", ".join(_SAFE_QUERY.get(scheme, ())) or "none"
        raise ProveviError(f"{url!r}: an endpoint has no query, because a query can name another server; "
                           f"the settings allowed for {scheme} are {allowed}")
    if scheme == "mssql" and (port is None or parts.path.strip("/") == ""):
        raise ProveviError(f"{url!r}: write an mssql URL with a port and a database, so ODBC cannot read it as a data source")
    hostport = parts.netloc.rpartition("@")[2]
    if hostport.endswith(":") or port == 0:
        raise ProveviError(f"{url!r}: write a port from 1 to 65535, or none")
    host = parts.hostname.rstrip(".")
    if ":" in host:
        host = f"[{host}]"  # an IPv6 address
    if not _HOST.fullmatch(host):
        raise ProveviError(f"{url!r}: {host!r} is not a host name")
    # sslmode stays, so that a policy can ask for TLS; the other settings name
    # no place and change nothing that a policy decides.
    kept = sorted(item for item in parts.query.split("&") if item.startswith(_KEPT_QUERY)) if parts.query else []
    query = "?" + "&".join(kept) if kept else ""
    return f"{scheme}://{host}:{port if port is not None else _DEFAULT_PORTS[scheme]}{_path(url, parts.path)}{query}"


def _path(url: str, path: str, keep_slash: bool = False) -> str:
    segments = path.split("/")
    if "%" in path or any(seg in (".", "..") for seg in segments) or "//" in path.rstrip("/"):
        raise ProveviError(f"{url!r}: write the path with no %, no . or .. segments and no //")
    return path if keep_slash else path.rstrip("/")


def _endpoint_problem(name: str, kind: str, endpoint: str, hosts: tuple[str, ...]) -> str | None:
    scheme = _scheme(endpoint)
    allowed = ("http", "https") + (MLFLOW_DATABASES if kind == "mlflow" else ())
    if scheme not in allowed:
        what = "an http, https or database" if kind == "mlflow" else "an http or https"
        return f"destination {name!r} needs {what} endpoint, not {endpoint!r}"
    if "@" in endpoint.split("//", 1)[-1].split("/", 1)[0]:
        return f"destination {name!r}: an endpoint names no user or password; the policy holds no secrets"
    try:
        normal = normal_endpoint(endpoint)
    except ProveviError as e:
        return f"destination {name!r}: {e}"
    host = urlsplit(normal).hostname or ""
    if scheme != "sqlite" and not _allowed(host, hosts):
        return f"destination {name!r} sends to {host!r}, which is not in hosts"
    return None


def otlp_traces_target(
    environ: Mapping[str, str],  # The environment variables, such as `os.environ`
) -> tuple[str, str] | None:  # The protocol and the address; `None` when neither endpoint variable is set
    """Find the protocol and the address to which provrun's OTLP exporter sends traces.

    It reads the variables in environ as the OpenTelemetry specification gives them. With neither endpoint
    variable set, provrun makes no OTLP exporter.

    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT wins, and is used as it is. Otherwise OTEL_EXPORTER_OTLP_ENDPOINT is
    used, with /v1/traces added over HTTP but not over gRPC. A variable that is set but empty counts as not
    set. The protocol comes from OTEL_EXPORTER_OTLP_TRACES_PROTOCOL, then OTEL_EXPORTER_OTLP_PROTOCOL, then
    OTEL_TRACES_EXPORTER (otlp_proto_grpc or otlp_proto_http), and is http/protobuf, the specification's
    default, otherwise. provrun must make the exporter of that protocol. A gRPC endpoint written as host:port
    gets https://, or http:// when OTEL_EXPORTER_OTLP_TRACES_INSECURE or OTEL_EXPORTER_OTLP_INSECURE is true.
    Raises ProveviError for a protocol that is not grpc, http/protobuf or http/json."""
    protocol = (environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or environ.get("OTEL_EXPORTER_OTLP_PROTOCOL")
                or {"otlp_proto_grpc": "grpc", "otlp_proto_http": "http/protobuf"}.get(
                    (environ.get("OTEL_TRACES_EXPORTER") or "").strip().lower(), "http/protobuf"))
    protocol = protocol.strip().lower()
    if protocol not in _PROTOCOLS:
        raise ProveviError(f"OTLP protocol {protocol!r} is not one of {', '.join(_PROTOCOLS)}")
    traces = environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or ""
    base = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    if traces:
        url = traces
    elif base:
        url = base if protocol == "grpc" else base.removesuffix("/") + "/v1/traces"
    else:
        return None
    if protocol == "grpc" and "://" not in url:
        insecure = (environ.get("OTEL_EXPORTER_OTLP_TRACES_INSECURE") or environ.get("OTEL_EXPORTER_OTLP_INSECURE")
                    or "").strip().lower() == "true"
        url = ("http://" if insecure else "https://") + url
    return protocol, url
