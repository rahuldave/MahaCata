"""Backends: run an agent spec in one place.

Every backend has the same asynchronous lifecycle, because a remote backend,
such as Foundry, starts a run, is asked about it later, and can cancel it:

    handle = await backend.start(ctx, agent, run)
    await backend.poll(handle)       # "running", "completed", "failed" or "cancelled"
    result = await backend.result(handle)
    await backend.cancel(handle)

``InProcessBackend`` runs the agent as an asyncio task in this process, so
start returns at once and result waits for the task. It writes
``sandbox_started`` and ``sandbox_completed`` around the run.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import RunContext
    from ..spec import AgentSpec, RunSpec


@dataclass
class RunResult:
    output: str
    duration: float
    usage: dict[str, int] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunHandle:
    run_id: str
    backend: str
    task: asyncio.Task | None = None
    remote_id: str | None = None  # a remote backend's own ID for the run


class Backend(ABC):
    name: str = ""

    @abstractmethod
    async def start(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunHandle: ...

    @abstractmethod
    async def poll(self, handle: RunHandle) -> str: ...

    @abstractmethod
    async def result(self, handle: RunHandle) -> RunResult: ...

    @abstractmethod
    async def cancel(self, handle: RunHandle) -> None: ...

    async def stream(self, handle: RunHandle) -> AsyncIterator[dict]:
        """Events as the run makes them. A backend without a stream gives none."""
        return
        yield  # pragma: no cover

    async def run(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunResult:
        handle = await self.start(ctx, agent, run)
        try:
            return await self.result(handle)
        except asyncio.CancelledError:
            await self.cancel(handle)
            raise


class InProcessBackend(Backend):
    """A backend whose agent runs in this process."""

    @abstractmethod
    async def execute(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunResult: ...

    async def _supervise(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunResult:
        name = self.__class__.__name__
        ctx.recorder.emit("sandbox_started", name, refs=[ctx.run_uri],
                          data={"agent": agent.name, "backend": self.name, "model": agent.model,
                                "inputs": [i.agent_path for i in run.inputs]})
        start = time.monotonic()
        with ctx.active():
            timeout = agent.limits.get("timeout")
            try:
                result = await asyncio.wait_for(self.execute(ctx, agent, run), timeout)
            except BaseException as e:
                ctx.recorder.emit("sandbox_completed", name, refs=[ctx.run_uri],
                                  data={"status": "failed", "error": f"{e.__class__.__name__}: {e}",
                                        "duration": time.monotonic() - start})
                raise
        result.duration = time.monotonic() - start
        ctx.recorder.emit("sandbox_completed", name, refs=[ctx.run_uri],
                          data={"status": "completed", "duration": result.duration, "usage": result.usage})
        return result

    async def start(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunHandle:
        task = asyncio.get_running_loop().create_task(self._supervise(ctx, agent, run))
        return RunHandle(ctx.run_id, self.name, task=task)

    async def poll(self, handle: RunHandle) -> str:
        t = handle.task
        if not t.done():
            return "running"
        if t.cancelled():
            return "cancelled"
        return "failed" if t.exception() else "completed"

    async def result(self, handle: RunHandle) -> RunResult:
        return await handle.task

    async def cancel(self, handle: RunHandle) -> None:
        if handle.task and not handle.task.done():
            handle.task.cancel()
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass


def load_backend(name: str, **options) -> Backend:
    """The backend registered under name in the entry point group provrun.backends."""
    found = {ep.name: ep for ep in entry_points(group="provrun.backends")}
    if name not in found:
        raise ValueError(f"no backend {name!r}; known: {', '.join(sorted(found))}")
    return found[name].load()(**options)
