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
import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context import RunContext
    from ..spec import AgentSpec, RunSpec

__all__ = ["Backend", "InProcessBackend", "Pieces", "RunHandle", "RunResult"]


@dataclass
class RunResult:
    """What a backend returns from one run."""

    output: str  # The agent's answer, as text
    duration: float  # The run's time in seconds; an in-process backend sets it
    usage: dict[str, int] = field(default_factory=dict)  # The token counts, such as input_tokens and output_tokens
    data: dict[str, Any] = field(default_factory=dict)  # More that a backend reports; the built-in backends leave it empty


class Pieces:
    """The pieces of one streamed model request: when the first came, and the text so far.

    If the request fails partway, its ``step_failed`` holds the text that was
    already handed on, so the evidence has all that a viewer saw. A backend
    sets ``RunContext.pieces`` to the request's Pieces."""

    def __init__(self):
        self.started, self.first_ms, self.text = time.monotonic(), None, []

    def add(
        self,
        piece: str,  # One piece of the reply's text
    ) -> None:  # Nothing
        "Keep one piece, and the time to it in milliseconds if it is the first."
        if self.first_ms is None:
            self.first_ms = round((time.monotonic() - self.started) * 1000)
        self.text.append(piece)

    def record(
        self,
        data: dict,  # A step's data, which gets first_token_ms
    ) -> None:  # Nothing
        """Put the time to the first piece, if any came, into a step's data."""
        if self.first_ms is not None:
            data["first_token_ms"] = self.first_ms

    def failed(
        self,
        step: Any,  # The evidence Step of the request that failed
    ) -> None:  # Nothing
        """Put what came before a failure into the step that failed.

        The step's data gets first_token_ms and the number of pieces, and its
        payload the partial response, when there was text."""
        self.record(step.data)
        step.data["pieces"] = len(self.text)
        if self.text:
            step.payload = json.dumps({"partial_response": "".join(self.text)}, ensure_ascii=False)


@dataclass
class RunHandle:
    """A run that a backend has started, as start returns it."""

    run_id: str  # The run's ID
    backend: str  # The backend's name
    task: asyncio.Task | None = None  # The task of an in-process run
    remote_id: str | None = None  # A remote backend's own ID for the run


class Backend(ABC):
    """Runs an agent spec in one place, with the lifecycle the module shows.

    A spec names its backend by the name it is registered under in the entry
    point group provrun.backends."""

    name: str = ""  # The name the backend is registered under, such as "litellm"

    @abstractmethod
    async def start(
        self,
        ctx: RunContext,  # The run; its recorder takes the evidence
        agent: AgentSpec,  # The agent to run
        run: RunSpec,  # The staged inputs and the run's folders
    ) -> RunHandle:  # The handle of the started run
        "Start one run."

    @abstractmethod
    async def poll(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> str:  # "running", "completed", "failed" or "cancelled"
        "Tell how a run stands."

    @abstractmethod
    async def result(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> RunResult:  # What the run gave back; it raises what the run raised
        "Wait for a run to end, and give its result."

    @abstractmethod
    async def cancel(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> None:  # Nothing, once the run is stopped
        "Stop a run."

    async def stream(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> AsyncIterator[dict]:  # Each event as the run makes it
        """Events as the run makes them. A backend without a stream gives none."""
        return
        yield  # pragma: no cover

    async def run(
        self,
        ctx: RunContext,  # The run
        agent: AgentSpec,  # The agent to run
        run: RunSpec,  # The staged inputs and the run's folders
    ) -> RunResult:  # What the run gave back
        "Start a run and wait for its result; a cancel while it waits cancels the run, and goes on."
        handle = await self.start(ctx, agent, run)
        try:
            return await self.result(handle)
        except asyncio.CancelledError:
            await self.cancel(handle)
            raise


class InProcessBackend(Backend):
    """A backend whose agent runs in this process."""

    @abstractmethod
    async def execute(
        self,
        ctx: RunContext,  # The run, active while execute runs, so current_run gives it
        agent: AgentSpec,  # The agent to run
        run: RunSpec,  # The staged inputs and the run's folders
    ) -> RunResult:  # What the agent gave back; the duration is set after
        "Run the agent, between sandbox_started and sandbox_completed, within the timeout in agent.limits."

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

    async def start(
        self,
        ctx: RunContext,  # The run; its recorder takes sandbox_started and sandbox_completed
        agent: AgentSpec,  # The agent to run
        run: RunSpec,  # The staged inputs and the run's folders
    ) -> RunHandle:  # The handle, which holds the task
        "Start execute as an asyncio task, and return at once."
        task = asyncio.get_running_loop().create_task(self._supervise(ctx, agent, run))
        return RunHandle(ctx.run_id, self.name, task=task)

    async def poll(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> str:  # "running", "completed", "failed" or "cancelled", from the task
        "Tell how the task stands."
        t = handle.task
        if not t.done():
            return "running"
        if t.cancelled():
            return "cancelled"
        return "failed" if t.exception() else "completed"

    async def result(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> RunResult:  # What execute returned; it raises what execute raised
        "Wait for the task."
        return await handle.task

    async def cancel(
        self,
        handle: RunHandle,  # The handle that start returned
    ) -> None:  # Nothing, once the task has stopped; its error is dropped
        "Cancel the task if it is going on, and wait for it to stop."
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
