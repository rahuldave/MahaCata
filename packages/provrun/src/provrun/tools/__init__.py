"""Tools that an agent can call, by name.

A tool is a function in the PydanticAI form: an async function whose first
argument is ``RunContext[RunDeps]``. Each tool has an execution mode. Only
``in_process`` runs now; ``client_round_trip`` is for Foundry function tools,
which provrun runs when Foundry asks, and ``remote_mcp`` is for tools that run
elsewhere and are imported into the evidence.

The registry finds tools in two places:

- the entry point group ``provrun.tools``, one function for each entry
- a tools folder, in which each module lists its tools in ``TOOLS``

The PydanticAI backend wraps every tool, so each call writes ``tool_called``.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai_filesystem_sandbox import Sandbox

    from ..context import RunContext as ProvrunContext

TOOL_MODES = ("in_process", "client_round_trip", "remote_mcp")


@dataclass
class RunDeps:
    """What a tool gets through ``ctx.deps``: the sandbox and the run."""

    sandbox: Sandbox
    run: ProvrunContext


@dataclass(frozen=True)
class Tool:
    name: str
    fn: Callable[..., Any]
    mode: str = "in_process"


class ToolRegistry:
    def __init__(self, folders: list[Path] | None = None):
        self.tools: dict[str, Tool] = {}
        for ep in entry_points(group="provrun.tools"):
            self.add(Tool(ep.name, ep.load()))
        for folder in folders or []:
            self.load_folder(folder)

    def add(self, tool: Tool) -> None:
        if tool.mode not in TOOL_MODES:
            raise ValueError(f"tool {tool.name}: mode must be one of {TOOL_MODES}")
        self.tools[tool.name] = tool

    def load_folder(self, folder: Path) -> None:
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"provrun_tools_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            listed = getattr(mod, "TOOLS", None)
            if listed is None:
                raise ValueError(f"{path} has no TOOLS list; list the tool functions in it")
            for fn in listed:
                self.add(fn if isinstance(fn, Tool) else Tool(fn.__name__, fn))

    def get(self, names: list[str]) -> list[Tool]:
        missing = [n for n in names if n not in self.tools]
        if missing:
            raise ValueError(f"unknown tools {missing}; known: {', '.join(sorted(self.tools))}")
        return [self.tools[n] for n in names]
