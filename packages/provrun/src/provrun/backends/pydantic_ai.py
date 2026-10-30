"""A PydanticAI agent with a filesystem sandbox, in this process.

The agent sees three folders:

    /input     the staged inputs, read-only
    /output    what the run hands back, writable
    /scratch   working space, writable, not part of the bundle

provrun, not the agent, writes the evidence. The model is wrapped, so each
model request writes ``model_called``, and every toolset is wrapped, so each
tool call writes ``tool_called``. After the run, the whole message history is
imported as one ``trace_imported`` event with confidence ``parsed``.

The wrappers see requests made through ``Model.request``. A streamed request
is not wrapped yet; provrun does not stream.
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


def evidence_model(ctx: RunContext, wrapped: Any):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart
    from pydantic_ai.models.wrapper import WrapperModel

    class EvidenceModel(WrapperModel):
        async def request(self, messages, model_settings, model_request_parameters):
            with ctx.recorder.step("model_called", "PydanticAIBackend",
                                   data={"model": self.model_name}) as step:
                response = await super().request(messages, model_settings, model_request_parameters)
                usage = response.usage
                step.data.update(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                                 finish_reason=response.finish_reason)
                # Every tool call the model asks for, valid or not. PydanticAI checks the
                # arguments before the toolset sees a call, so tool_called misses a bad one.
                step.data["tool_calls"] = [{"tool": part.tool_name, "id": part.tool_call_id}
                                           for part in response.parts if isinstance(part, ToolCallPart)]
                step.payload = json.dumps({
                    "request": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
                    "response": json.loads(ModelMessagesTypeAdapter.dump_json([response])),
                }, ensure_ascii=False)
            return response

    return EvidenceModel(wrapped)


def evidence_toolset(ctx: RunContext, wrapped: Any):
    from pydantic_ai import ModelRetry
    from pydantic_ai.toolsets import WrapperToolset
    from pydantic_ai_filesystem_sandbox import SandboxError

    class EvidenceToolset(WrapperToolset):
        async def call_tool(self, name, tool_args, run_ctx, tool):
            try:
                with ctx.recorder.step("tool_called", f"tool:{name}", data={"tool": name}) as step:
                    step.payload = json.dumps({"args": tool_args}, ensure_ascii=False, default=str)
                    result = await super().call_tool(name, tool_args, run_ctx, tool)
                    step.payload = json.dumps({"args": tool_args, "result": result},
                                              ensure_ascii=False, default=str)
            except SandboxError as e:
                # The refusal is in the evidence as step_failed; the model is told, and can try another way.
                raise ModelRetry(str(e)) from e
            return result

    return EvidenceToolset(wrapped)


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
        model = evidence_model(ctx, self._model if self._model is not None else infer_model(agent.model))
        pa = Agent(model, instructions=agent.instructions or None, deps_type=RunDeps,
                   toolsets=[evidence_toolset(ctx, t) for t in toolsets],
                   retries=agent.limits.get("retries", 2))
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
