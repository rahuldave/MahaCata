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
from provevi import ProveviError, check_store_uri
from provevi.telemetry import load_yaml

from .context import check_scheme, check_segment
from .errors import RunFileError, RunSettingError

__all__ = ["AgentSpec", "InputSpec", "RunFile", "RunSettings", "RunSpec", "StagedInput"]

# A setting with one of these words in its name can carry a credential, for
# example extra_headers with an API key. The spec goes into the evidence, so
# such settings are refused; keys come from the environment of the provider.
_SECRET_SETTING = re.compile(r"key|secret|auth|header|password|credential|extra_body|base_url|api_base", re.I)


@dataclass
class InputSpec:
    """One input of an agent: a catalog URI, staged for the agent under /input."""

    uri: str  # The catalog URI of the input
    name: str | None = None  # The name under /input; defaults to the last part of the URI
    mode: str = "mounted_snapshot"  # The access mode the run asks the broker for; run_agent refuses pinned_read with RunSettingError, before the run folder is made


@dataclass
class AgentSpec:
    """What an agent is: its backend, model, prompt, inputs and tools.

    It says nothing of where the agent runs. A setting whose name can carry a
    credential, such as api_key, raises ValueError."""

    name: str  # The agent's name; the evidence and the access requests record it
    backend: str  # "litellm" or "pydantic-ai", the name of a backend in the entry point group provrun.backends
    model: str  # The model, as the backend names it, such as openai:gpt-4o for pydantic-ai
    prompt: str  # The user prompt
    instructions: str = ""  # The system instructions
    inputs: list[InputSpec] = field(default_factory=list)  # The catalog inputs the agent reads
    tools: list[str] = field(default_factory=list)  # The names of the tools it may call, such as grep_file
    settings: dict[str, Any] = field(default_factory=dict)  # Model settings, for example temperature
    limits: dict[str, Any] = field(default_factory=dict)  # For example request_limit, retries, timeout in seconds
    stream: bool = False  # Ask for the reply in pieces; the evidence still records each whole response

    def __post_init__(self):
        refused = sorted(k for k in self.settings if _SECRET_SETTING.search(k))
        if refused:
            raise ValueError(f"agent spec: settings {refused} can carry a credential, and the spec goes into "
                             "the evidence; set the key in the environment of the provider instead")

    @classmethod
    def from_dict(
        cls,
        d: dict,  # The agent section of a run file; an input is a URI or a mapping of InputSpec's fields
    ) -> AgentSpec:  # The checked spec
        """Check the types of each key and the input URIs, and make the spec.

        Raises ValueError or TypeError."""
        d = dict(d)
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"agent spec: unknown keys {sorted(unknown)}")
        for k in ("name", "backend", "model", "prompt", "instructions"):
            if k in d and not isinstance(d[k], str):
                raise TypeError(f"agent spec: {k} must be a string")
        if not isinstance(d.get("stream", False), bool):
            raise TypeError("agent spec: stream must be true or false")
        for k in ("settings", "limits"):
            if k in d and not isinstance(d[k], dict):
                raise TypeError(f"agent spec: {k} must be a mapping")
        if not isinstance(d.get("tools", []), list) or not all(isinstance(t, str) for t in d.get("tools", [])):
            raise TypeError("agent spec: tools must be a list of names")
        if not isinstance(d.get("inputs", []), list):
            raise TypeError("agent spec: inputs must be a list")
        d["inputs"] = [_input(i) for i in d.get("inputs", [])]
        return cls(**d)

    def to_dict(
        self,
    ) -> dict:  # Every field, the inputs as mappings too
        "The spec as plain values, as run_created records it."
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
    """One input as a stager staged it for one run."""

    name: str  # Its name under the input folder
    uri: str  # The URI as asked for
    pinned: str  # The physical URI at a fixed version, as the snapshot pinned it
    digest: str  # The SHA-256 of the snapshot, which verify_inputs checks after the run
    kind: str  # "file" or "folder"
    host_path: Path  # Where the backend reads it
    agent_path: str  # Where the agent sees it, for example /input/report.txt


@dataclass
class RunSpec:
    """What a stager makes for one run, and a backend runs.

    It holds the staged inputs, where the agent sees them, and where it writes."""

    run_id: str  # The run's ID
    inputs: list[StagedInput]  # The staged inputs
    input_dir: Path  # The folder that holds the staged inputs, read-only
    output_dir: Path  # The folder the agent writes its results to
    scratch_dir: Path  # The agent's working folder, not part of the bundle
    agent_input: str = "/input"  # Where the agent sees input_dir
    agent_output: str = "/output"  # Where the agent sees output_dir
    agent_scratch: str = "/scratch"  # Where the agent sees scratch_dir
    env: dict[str, str] = field(default_factory=dict)  # Environment variables for the agent; no backend reads them yet


def _read_run_file(path: Path) -> tuple[dict, AgentSpec, Any, str | None]:
    path = Path(path)
    try:
        doc = load_yaml(path.read_text()) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise RunFileError(f"cannot read the run file {path}: {e}") from e
    if not isinstance(doc, dict):
        raise RunFileError(f"{path}: a run file is a mapping with run and agent sections")
    unknown = set(doc) - {"run", "agent", "telemetry", "experiment"}
    if unknown:
        raise RunFileError(f"{path}: unknown sections {sorted(unknown)}")
    if "agent" not in doc:
        raise RunFileError(f"{path}: no agent section")
    run = doc.get("run") or {}
    if not isinstance(run, dict):
        raise RunFileError(f"{path}: the run section must be a mapping")
    if not isinstance(doc["agent"], dict):
        raise RunFileError(f"{path}: the agent section must be a mapping")
    telemetry = doc.get("telemetry")
    if telemetry is not None and not isinstance(telemetry, dict):
        raise RunFileError(f"{path}: the telemetry section must be a mapping")
    experiment = doc.get("experiment")
    if experiment is not None and (not isinstance(experiment, str) or not experiment.startswith("experiment://")):
        raise RunFileError(f"{path}: experiment is the URI of an experiment, which starts with experiment://")
    try:
        agent = AgentSpec.from_dict(doc["agent"])
    except (TypeError, ValueError) as e:
        raise RunFileError(f"{path}: the agent section: {e}") from e
    return dict(run), agent, telemetry, experiment


def load_run_file(path: Path) -> tuple[dict, AgentSpec]:
    """Read a run file: a ``run`` section with org, domain, env and actor, an
    ``agent`` section that is an agent spec, and an optional ``telemetry``
    section and ``experiment``, which ``RunFile.load`` keeps. A key written twice is refused. The
    run section comes back as it is written; ``RunFile.load`` also checks it."""
    run, agent, _, _ = _read_run_file(path)
    return run, agent


@dataclass(frozen=True)
class RunSettings:
    """The run section of a run file: who runs, where, and what else the stub broker allows."""

    org: str  # The org, one part of the run URI
    domain: str  # The domain, one part of the run URI
    env: str  # The environment, one part of the run URI, such as dev or prod
    actor: str  # Who runs it
    allow: tuple[str, ...] = ()  # More URIs the stub broker allows, besides the agent's inputs
    bundle_scheme: str | None = None  # A logical scheme to write the bundle through; None keeps it in the run folder
    store: str | None = None  # The evidence store, such as sqlite:///evidence.db; the file store if None

    KEYS = ("org", "domain", "env", "actor", "allow", "bundle_scheme", "store")

    @classmethod
    def from_dict(
        cls,
        d: dict,  # The run section of a run file
        where: str = "the run section",  # What to call it in an error
    ) -> RunSettings:  # The checked settings
        "Check the run section and make the settings. Raises RunFileError."
        unknown = set(d) - set(cls.KEYS)
        if unknown:
            raise RunFileError(f"{where}: unknown keys {sorted(unknown)}")
        missing = [k for k in ("org", "domain", "env", "actor") if not d.get(k)]
        if missing:
            raise RunFileError(f"{where} needs {missing}")
        for k in ("org", "domain", "env", "actor", "bundle_scheme", "store"):
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
            if d.get("store") is not None:
                check_store_uri(d["store"])
        except (RunSettingError, ProveviError, TypeError, ValueError) as e:
            raise RunFileError(f"{where}: {e}") from e
        return cls(d["org"], d["domain"], d["env"], d["actor"], tuple(allow), d.get("bundle_scheme"),
                   d.get("store"))


@dataclass(frozen=True)
class RunFile:
    """A checked run file: its run settings and its agent spec."""

    settings: RunSettings  # The run section
    agent: AgentSpec  # The agent section
    telemetry: dict | None = None  # The telemetry section, which narrows the operator's policy
    experiment: str | None = None  # The experiment the run belongs to, experiment://org/team/env/project/name

    @classmethod
    def load(
        cls,
        path: str | Path,  # The run file, in YAML
    ) -> RunFile:  # The checked run file
        """Read and check a run file.

        Raises RunFileError. The telemetry section is checked against the policy, and the experiment is pinned,
        when the run starts."""
        run, agent, telemetry, experiment = _read_run_file(Path(path))
        return cls(RunSettings.from_dict(run, f"{path}: the run section"), agent, telemetry, experiment)
