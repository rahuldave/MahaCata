"""Tools that an agent can call, by name.

A tool is a function in the PydanticAI form: an async function whose first
argument is ``RunContext[RunDeps]``. Each tool has an execution mode. Only
``in_process`` runs now; ``client_round_trip`` is for Foundry function tools,
which provrun runs when Foundry asks, and ``remote_mcp`` is for tools that run
elsewhere and are imported into the evidence.

The registry finds tools in two places:

- the entry point group ``provrun.tools``, one function for each entry
- a tools folder, in which each module lists its tools in ``TOOLS``

The PydanticAI backend records every tool through its evidence capability, so each
call writes ``tool_called``.
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

__all__ = ["RunDeps", "Tool", "ToolRegistry"]

TOOL_MODES = ("in_process", "client_round_trip", "remote_mcp")


@dataclass
class RunDeps:
    """What a tool gets through ``ctx.deps``: the sandbox and the run."""

    sandbox: Sandbox  # The agent's filesystem sandbox, over /input, /output and /scratch
    run: ProvrunContext  # The provrun run context; a tool writes evidence through run.recorder


@dataclass(frozen=True)
class Tool:
    """One tool: the name an agent spec gives it, its function and its execution mode."""

    name: str  # The name in an agent spec's tools
    fn: Callable[..., Any]  # The function, in the PydanticAI form
    mode: str = "in_process"  # One of TOOL_MODES; only in_process runs now


class ToolRegistry:
    """The tools that agents can call, by name."""

    def __init__(
        self,
        folders: list[Path] | None = None,  # Tools folders to load as well as the entry points
    ):
        self.tools: dict[str, Tool] = {}
        for ep in entry_points(group="provrun.tools"):
            self.add(Tool(ep.name, ep.load()))
        for folder in folders or []:
            self.load_folder(folder)

    def add(
        self,
        tool: Tool,  # The tool; one with the same name is replaced
    ) -> None:  # Nothing
        "Add one tool. Raises ValueError for a mode not in TOOL_MODES."
        if tool.mode not in TOOL_MODES:
            raise ValueError(f"tool {tool.name}: mode must be one of {TOOL_MODES}")
        self.tools[tool.name] = tool

    def load_folder(
        self,
        folder: Path,  # A folder of modules, each with a TOOLS list of functions or Tools
    ) -> None:  # Nothing
        """Import each module in folder, and add the tools in its TOOLS list.

        A module whose name starts with _ is skipped. Raises ValueError for a
        module with no TOOLS."""
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

    def get(
        self,
        names: list[str],  # The tool names, as an agent spec lists them
    ) -> list[Tool]:  # The tools, in the order named
        "Find tools by name. Raises ValueError for a name that is not here."
        missing = [n for n in names if n not in self.tools]
        if missing:
            raise ValueError(f"unknown tools {missing}; known: {', '.join(sorted(self.tools))}")
        return [self.tools[n] for n in names]
