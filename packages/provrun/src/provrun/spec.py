"""Specs: what an agent is, and what one run of it gets.

``AgentSpec`` is portable data: it says what the agent is, and nothing about
where it runs, so the same spec can go to an in-process backend now and to
Docker or Foundry later. ``RunSpec`` is what a stager makes for one run: the
staged inputs, where the agent sees them, and where it writes.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .context import check_scheme, check_segment
from .errors import RunFileError, RunSettingError

# A setting with one of these words in its name can carry a credential, for
# example extra_headers with an API key. The spec goes into the evidence, so
# such settings are refused; keys come from the environment of the provider.
_SECRET_SETTING = re.compile(r"key|secret|auth|header|password|credential|extra_body|base_url|api_base", re.I)


@dataclass
class InputSpec:
    uri: str
    name: str | None = None  # the name under /input; defaults to the last part of the URI
    mode: str = "mounted_snapshot"


@dataclass
class AgentSpec:
    name: str
    backend: str  # "litellm" or "pydantic-ai"
    model: str
    prompt: str
    instructions: str = ""
    inputs: list[InputSpec] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)  # model settings, for example temperature
    limits: dict[str, Any] = field(default_factory=dict)  # for example request_limit, timeout

    def __post_init__(self):
        refused = sorted(k for k in self.settings if _SECRET_SETTING.search(k))
        if refused:
            raise ValueError(f"agent spec: settings {refused} can carry a credential, and the spec goes into "
                             "the evidence; set the key in the environment of the provider instead")

    @classmethod
    def from_dict(cls, d: dict) -> AgentSpec:
        """Check the types of each key and the input URIs, and make the spec.
        Raises ValueError or TypeError."""
        d = dict(d)
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"agent spec: unknown keys {sorted(unknown)}")
        for k in ("name", "backend", "model", "prompt", "instructions"):
            if k in d and not isinstance(d[k], str):
                raise TypeError(f"agent spec: {k} must be a string")
        for k in ("settings", "limits"):
            if k in d and not isinstance(d[k], dict):
                raise TypeError(f"agent spec: {k} must be a mapping")
        if not isinstance(d.get("tools", []), list) or not all(isinstance(t, str) for t in d.get("tools", [])):
            raise TypeError("agent spec: tools must be a list of names")
        if not isinstance(d.get("inputs", []), list):
            raise TypeError("agent spec: inputs must be a list")
        d["inputs"] = [_input(i) for i in d.get("inputs", [])]
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


def check_uri(text, what: str) -> None:
    """Refuse text that is not a provcat URI."""
    from provcat import UriError, parse

    if not isinstance(text, str):
        raise TypeError(f"{what} must be a URI string; got {text!r}")
    try:
        parse(text)
    except UriError as e:
        raise ValueError(f"{what}: {e}") from e


def _input(i) -> InputSpec:
    if isinstance(i, dict):
        for k, v in i.items():
            if not isinstance(v, str) and not (k == "name" and v is None):
                raise TypeError(f"agent spec: input {k} must be a string")
        spec = InputSpec(**i)
    else:
        spec = InputSpec(uri=i)
    check_uri(spec.uri, "agent spec: input")
    return spec


@dataclass(frozen=True)
class StagedInput:
    name: str
    uri: str
    pinned: str
    digest: str
    kind: str
    host_path: Path  # where the backend reads it
    agent_path: str  # where the agent sees it, for example /input/report.txt


@dataclass
class RunSpec:
    run_id: str
    inputs: list[StagedInput]
    input_dir: Path
    output_dir: Path
    scratch_dir: Path
    agent_input: str = "/input"
    agent_output: str = "/output"
    agent_scratch: str = "/scratch"
    env: dict[str, str] = field(default_factory=dict)


def load_run_file(path: Path) -> tuple[dict, AgentSpec]:
    """Read a run file: a ``run`` section with org, domain, env and actor, and
    an ``agent`` section that is an agent spec. The run section comes back as
    it is written; ``RunFile.load`` also checks it."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise RunFileError(f"cannot read the run file {path}: {e}") from e
    if not isinstance(doc, dict):
        raise RunFileError(f"{path}: a run file is a mapping with run and agent sections")
    unknown = set(doc) - {"run", "agent"}
    if unknown:
        raise RunFileError(f"{path}: unknown sections {sorted(unknown)}")
    if "agent" not in doc:
        raise RunFileError(f"{path}: no agent section")
    run = doc.get("run") or {}
    if not isinstance(run, dict):
        raise RunFileError(f"{path}: the run section must be a mapping")
    if not isinstance(doc["agent"], dict):
        raise RunFileError(f"{path}: the agent section must be a mapping")
    try:
        agent = AgentSpec.from_dict(doc["agent"])
    except (TypeError, ValueError) as e:
        raise RunFileError(f"{path}: the agent section: {e}") from e
    return dict(run), agent


@dataclass(frozen=True)
class RunSettings:
    """The run section of a run file: who runs, where, and what else the stub
    broker allows."""

    org: str
    domain: str
    env: str
    actor: str
    allow: tuple[str, ...] = ()
    bundle_scheme: str | None = None

    KEYS = ("org", "domain", "env", "actor", "allow", "bundle_scheme")

    @classmethod
    def from_dict(cls, d: dict, where: str = "the run section") -> RunSettings:
        unknown = set(d) - set(cls.KEYS)
        if unknown:
            raise RunFileError(f"{where}: unknown keys {sorted(unknown)}")
        missing = [k for k in ("org", "domain", "env", "actor") if not d.get(k)]
        if missing:
            raise RunFileError(f"{where} needs {missing}")
        for k in ("org", "domain", "env", "actor", "bundle_scheme"):
            if d.get(k) is not None and not isinstance(d[k], str):
                raise RunFileError(f"{where}: {k} must be a string")
        allow = d.get("allow") or []
        if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
            raise RunFileError(f"{where}: allow must be a list of URIs")
        try:
            for k in ("org", "domain", "env"):
                check_segment(k, d[k])
            if d.get("bundle_scheme") is not None:
                check_scheme(d["bundle_scheme"])
            for a in allow:
                check_uri(a, "allow")
        except (RunSettingError, TypeError, ValueError) as e:
            raise RunFileError(f"{where}: {e}") from e
        return cls(d["org"], d["domain"], d["env"], d["actor"], tuple(allow), d.get("bundle_scheme"))


@dataclass(frozen=True)
class RunFile:
    """A checked run file: its run settings and its agent spec."""

    settings: RunSettings
    agent: AgentSpec

    @classmethod
    def load(cls, path: str | Path) -> RunFile:
        """Read and check a run file. Raises RunFileError."""
        run, agent = load_run_file(Path(path))
        return cls(RunSettings.from_dict(run, f"{path}: the run section"), agent)
