"""A PydanticAI agent with a filesystem sandbox, in this process.

The agent sees three folders:

    /input     the staged inputs, read-only
    /output    what the run hands back, writable
    /scratch   working space, writable, not part of the bundle

provrun, not the agent, writes the evidence. One PydanticAI capability,
``evidence_capability``, sees every step of the run:

- each model request, streamed or not, writes ``model_called``;
- each call of a function tool, from any toolset or capability, writes
  ``tool_called``;
- a call that waits for approval, or is deferred, writes ``tool_called`` with
  ``deferred`` set, and writes it again when it runs;
- the output tool, when the agent answers through one, writes ``tool_called``
  with ``output_tool`` set. In a streamed request it is written before the
  ``model_called`` whose response asked for it, because PydanticAI checks the
  output while the stream is still open;
- each tool that the provider ran itself, and returned in a response, writes
  ``tool_called`` with ``provider_run`` set and confidence ``parsed``, because
  provrun sees only the provider's account of it.

The backend itself has text output and no provider tools, so it never makes
the last two; they matter to other callers of the capability.

After the run, the whole message history is imported as one
``trace_imported`` event with confidence ``parsed``.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import InProcessBackend, RunResult

if TYPE_CHECKING:
    from ..context import RunContext
    from ..spec import AgentSpec, RunSpec
    from ..tools import ToolRegistry


def _needs():
    try:
        import pydantic_ai  # noqa: F401
        import pydantic_ai_filesystem_sandbox  # noqa: F401
    except ImportError as e:
        raise ImportError("the pydantic-ai backend needs provrun[pydantic-ai]") from e


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def evidence_capability(ctx: RunContext):
    """The PydanticAI capability that writes provrun's evidence for one run."""
    from pydantic_ai import ApprovalRequired, CallDeferred, ModelRetry
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import (BaseToolCallPart, ModelMessagesTypeAdapter, NativeToolCallPart,
                                      NativeToolReturnPart)
    from pydantic_ai_filesystem_sandbox import SandboxError

    def provider_tools(response) -> None:
        results = {p.tool_call_id: p for p in response.parts if isinstance(p, NativeToolReturnPart)}
        for part in response.parts:
            if not isinstance(part, NativeToolCallPart):
                continue
            result = results.get(part.tool_call_id)
            ctx.recorder.emit("tool_called", f"provider:{part.tool_name}", confidence="parsed",
                              data={"tool": part.tool_name, "id": part.tool_call_id, "provider_run": True,
                                    "provider": part.provider_name},
                              payload=_dump({"args": part.args,
                                             "result": result.content if result is not None else None}))

    class EvidenceCapability(AbstractCapability):
        async def wrap_model_request(self, run_ctx, *, request_context, handler):
            with ctx.recorder.step("model_called", "PydanticAIBackend",
                                   data={"model": request_context.model.model_name,
                                         "streamed": bool(request_context.streaming)}) as step:
                response = await handler(request_context)
                usage = response.usage
                if response.model_name:
                    step.data["answered_by"] = response.model_name  # a FallbackModel names the one that answered
                step.data.update(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                                 finish_reason=response.finish_reason)
                # Every tool call the model asks for, valid or not. PydanticAI checks the
                # arguments before a tool runs, so tool_called misses a bad one.
                step.data["tool_calls"] = [{"tool": part.tool_name, "id": part.tool_call_id}
                                           for part in response.parts if isinstance(part, BaseToolCallPart)]
                step.payload = json.dumps({
                    "request": json.loads(ModelMessagesTypeAdapter.dump_json(request_context.messages)),
                    "response": json.loads(ModelMessagesTypeAdapter.dump_json([response])),
                }, ensure_ascii=False)
            provider_tools(response)
            return response

        async def wrap_tool_execute(self, run_ctx, *, call, tool_def, args, handler):
            name = call.tool_name
            try:
                deferred = None
                with ctx.recorder.step("tool_called", f"tool:{name}", data={"tool": name}) as step:
                    step.payload = _dump({"args": args})
                    try:
                        result = await handler(args)
                    except (ApprovalRequired, CallDeferred) as e:
                        # Not a failure: the call waits, and runs again once approved.
                        deferred = e
                        step.data["deferred"] = type(e).__name__
                    else:
                        step.payload = _dump({"args": args, "result": result})
                if deferred is not None:
                    raise deferred
            except SandboxError as e:
                # The refusal is in the evidence as step_failed; the model is told, and can try another way.
                raise ModelRetry(str(e)) from e
            return result

        async def wrap_output_process(self, run_ctx, *, output_context, output, handler):
            call = output_context.tool_call
            if call is None or run_ctx.partial_output:
                return await handler(output)
            with ctx.recorder.step("tool_called", f"tool:{call.tool_name}",
                                   data={"tool": call.tool_name, "output_tool": True}) as step:
                step.payload = _dump({"args": call.args})
                result = await handler(output)
                step.payload = _dump({"args": call.args, "result": result})
            return result

    return EvidenceCapability()


class PydanticAIBackend(InProcessBackend):
    name = "pydantic-ai"

    def __init__(self, model: Any = None, registry: ToolRegistry | None = None):
        self._model = model  # for tests: a PydanticAI Model to use instead of agent.model
        self._registry = registry

    async def execute(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunResult:
        _needs()
        from pydantic_ai import Agent, UsageLimits
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        from pydantic_ai.models import infer_model
        from pydantic_ai.toolsets import FunctionToolset
        from pydantic_ai_filesystem_sandbox import FileSystemToolset, Mount, Sandbox, SandboxConfig

        from ..tools import RunDeps, ToolRegistry

        sandbox = Sandbox(SandboxConfig(mounts=[
            Mount(host_path=run.input_dir, mount_point=run.agent_input, mode="ro"),
            Mount(host_path=run.output_dir, mount_point=run.agent_output, mode="rw",
                  write_approval=False, read_approval=False),
            Mount(host_path=run.scratch_dir, mount_point=run.agent_scratch, mode="rw",
                  write_approval=False, read_approval=False),
        ]))
        registry = self._registry or ToolRegistry()
        toolsets = [FileSystemToolset(sandbox)]
        tools = registry.get(agent.tools)
        if any(t.mode != "in_process" for t in tools):
            raise ValueError("the pydantic-ai backend runs in_process tools only")
        if tools:
            toolsets.append(FunctionToolset([t.fn for t in tools]))
        model = self._model if self._model is not None else infer_model(agent.model)
        pa = Agent(model, instructions=agent.instructions or None, deps_type=RunDeps, toolsets=toolsets,
                   capabilities=[evidence_capability(ctx)], retries=agent.limits.get("retries", 2))
        listing = ", ".join(i.agent_path for i in run.inputs) or "none"
        prompt = (f"[Inputs, read-only: {listing}]\n[Write results under {run.agent_output}; "
                  f"work under {run.agent_scratch}]\n\n{agent.prompt}")
        ctx.recorder.emit("prompt_rendered", self.__class__.__name__, refs=[i.uri for i in run.inputs],
                          payload={"instructions": agent.instructions, "prompt": prompt},
                          data={"tools": agent.tools})
        result = await pa.run(prompt, deps=RunDeps(sandbox, ctx),
                              model_settings=agent.settings or None,
                              usage_limits=UsageLimits(request_limit=agent.limits.get("request_limit", 25)))
        output = result.output if isinstance(result.output, str) else json.dumps(result.output, default=str)
        messages = result.all_messages()
        ctx.recorder.emit("trace_imported", "PydanticAIImporter", confidence="parsed",
                          payload=ModelMessagesTypeAdapter.dump_json(messages).decode(),
                          data={"messages": len(messages), "source": "pydantic_ai.all_messages"})
        response = run.output_dir / "response.txt"
        if not response.exists():
            response.write_text(output, encoding="utf-8")
        usage = result.usage
        return RunResult(output, 0.0, usage={"input_tokens": usage.input_tokens or 0,
                                             "output_tokens": usage.output_tokens or 0,
                                             "requests": usage.requests or 0})
