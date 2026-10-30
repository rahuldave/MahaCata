"""Specs: what an agent is, and what one run of it gets.

``AgentSpec`` is portable data: it says what the agent is, and nothing about
where it runs, so the same spec can go to an in-process backend now and to
Docker or Foundry later. ``RunSpec`` is what a stager makes for one run: the
staged inputs, where the agent sees them, and where it writes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


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

    @classmethod
    def from_dict(cls, d: dict) -> AgentSpec:
        d = dict(d)
        unknown = set(d) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"agent spec: unknown keys {sorted(unknown)}")
        d["inputs"] = [InputSpec(**i) if isinstance(i, dict) else InputSpec(uri=i) for i in d.get("inputs", [])]
        return cls(**d)

    def to_dict(self) -> dict:
        return asdict(self)


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
    an ``agent`` section that is an agent spec."""
    doc = yaml.safe_load(path.read_text()) or {}
    unknown = set(doc) - {"run", "agent"}
    if unknown:
        raise ValueError(f"{path}: unknown sections {sorted(unknown)}")
    if "agent" not in doc:
        raise ValueError(f"{path}: no agent section")
    return dict(doc.get("run") or {}), AgentSpec.from_dict(doc["agent"])
