"""The PydanticAI evidence capability: every model request and every tool, with fake models."""
import asyncio
from types import SimpleNamespace

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (ModelResponse, NativeToolCallPart, NativeToolReturnPart, TextPart,
                                  ToolCallPart)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from provrun.backends.pydantic_ai import evidence_capability
from provrun.evidence import EvidenceRecorder, read_events


def recorder(tmp_path):
    return SimpleNamespace(recorder=EvidenceRecorder("r1", "tester", tmp_path / "evidence"))


def events(tmp_path, ctx):
    ctx.recorder.close()
    return read_events(tmp_path / "evidence")


def hello(n: int) -> str:
    return f"hi {n}"


def test_a_streamed_run_writes_one_model_called_per_request(tmp_path):
    async def stream(messages, info):
        if len(messages) == 1:
            yield {0: DeltaToolCall(name="hello", json_args='{"n": 1}', tool_call_id="c1")}
        else:
            yield "done"

    ctx = recorder(tmp_path)
    agent = Agent(FunctionModel(stream_function=stream), tools=[hello], capabilities=[evidence_capability(ctx)])

    async def go():
        async with agent.run_stream("go") as s:
            return await s.get_output()

    assert asyncio.run(go()) == "done"
    evs = events(tmp_path, ctx)
    models = [e for e in evs if e.type == "model_called"]
    assert len(models) == 2 and all(e.data["streamed"] for e in models)
    assert [e.data["tool"] for e in evs if e.type == "tool_called"] == ["hello"]


def test_a_tool_from_another_capability_is_recorded(tmp_path):
    class More(AbstractCapability):
        def get_toolset(self):
            return FunctionToolset([hello])

    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("hello", {"n": 2}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("ok")])

    ctx = recorder(tmp_path)
    Agent(FunctionModel(fn), capabilities=[More(), evidence_capability(ctx)]).run_sync("go")
    tools = [e for e in events(tmp_path, ctx) if e.type == "tool_called"]
    assert [e.data["tool"] for e in tools] == ["hello"]


def test_an_output_tool_is_recorded(tmp_path):
    class Answer(BaseModel):
        value: int

    def fn(messages, info):
        name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(name, {"value": 7}, tool_call_id="o1")])

    ctx = recorder(tmp_path)
    out = Agent(FunctionModel(fn), output_type=Answer, capabilities=[evidence_capability(ctx)]).run_sync("go")
    assert out.output.value == 7
    [tool] = [e for e in events(tmp_path, ctx) if e.type == "tool_called"]
    assert tool.data["output_tool"] is True
    assert '"value": 7' in (tmp_path / "evidence" / "payloads" / tool.payload).read_text()


def test_a_tool_the_provider_ran_is_recorded_as_parsed(tmp_path):
    def fn(messages, info):
        return ModelResponse(parts=[
            NativeToolCallPart("web_search", {"query": "q"}, tool_call_id="n1", provider_name="test"),
            NativeToolReturnPart("web_search", {"hits": 3}, tool_call_id="n1", provider_name="test"),
            TextPart("found 3"),
        ])

    ctx = recorder(tmp_path)
    Agent(FunctionModel(fn), capabilities=[evidence_capability(ctx)]).run_sync("go")
    evs = events(tmp_path, ctx)
    [tool] = [e for e in evs if e.type == "tool_called"]
    assert tool.confidence == "parsed" and tool.data["provider_run"] is True
    assert tool.component == "provider:web_search"
    assert '"hits": 3' in (tmp_path / "evidence" / "payloads" / tool.payload).read_text()
    model = next(e for e in evs if e.type == "model_called")
    assert model.data["tool_calls"] == [{"tool": "web_search", "id": "n1"}]
    assert not {"trace_id", "span_id", "parent_span_id", "started_at"} & tool.data.keys()  # no tracing here


def test_a_tool_the_provider_ran_is_a_child_of_its_model_call(tmp_path, spans):
    def fn(messages, info):
        return ModelResponse(parts=[
            NativeToolCallPart("web_search", {"query": "q"}, tool_call_id="n1", provider_name="test"),
            NativeToolReturnPart("web_search", {"hits": 3}, tool_call_id="n1", provider_name="test"),
            TextPart("found 3"),
        ])

    ctx = recorder(tmp_path)
    Agent(FunctionModel(fn), capabilities=[evidence_capability(ctx)]).run_sync("go")
    evs = events(tmp_path, ctx)
    model = next(e for e in evs if e.type == "model_called")
    [tool] = [e for e in evs if e.type == "tool_called"]
    assert evs.index(model) < evs.index(tool)  # the order of the events is kept
    assert tool.data["parent_span_id"] == model.data["span_id"]
    assert tool.data["trace_id"] == model.data["trace_id"]
    assert tool.data["span_id"] not in (model.data["span_id"], None)
    assert "started_at" not in tool.data  # provrun did not see when the provider ran it
    [span] = [s for s in spans.get_finished_spans() if s.name == "tool_called"]
    assert span.attributes["gen_ai.tool.name"] == "web_search"
    assert span.attributes["provrun.component"] == "provider:web_search"


def test_a_deferred_call_is_not_a_failure(tmp_path):
    from pydantic_ai import ApprovalRequired, DeferredToolRequests, DeferredToolResults

    def later(run_ctx: RunContext, n: int) -> str:
        if not run_ctx.tool_call_approved:
            raise ApprovalRequired()  # the tool itself asks for approval
        return f"ran {n}"

    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("later", {"n": 3}, tool_call_id="d1")])
        return ModelResponse(parts=[TextPart("ok")])

    ctx = recorder(tmp_path)
    agent = Agent(FunctionModel(fn), output_type=[str, DeferredToolRequests],
                  capabilities=[evidence_capability(ctx)])
    agent.tool(later)
    first = agent.run_sync("go")
    assert isinstance(first.output, DeferredToolRequests)
    agent.run_sync(message_history=first.all_messages(),
                   deferred_tool_results=DeferredToolResults(approvals={"d1": True}))
    evs = events(tmp_path, ctx)
    assert "step_failed" not in [e.type for e in evs]
    calls = [e.data for e in evs if e.type == "tool_called"]
    assert [c.get("deferred") for c in calls] == ["ApprovalRequired", None]


def test_model_called_names_the_model_that_answered(tmp_path):
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.models.fallback import FallbackModel

    def bad(messages, info):
        raise ModelHTTPError(500, "bad")

    good = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")]), model_name="good")
    ctx = recorder(tmp_path)
    Agent(FallbackModel(FunctionModel(bad, model_name="bad"), good),
          capabilities=[evidence_capability(ctx)]).run_sync("go")
    [model] = [e for e in events(tmp_path, ctx) if e.type == "model_called"]
    assert model.data["answered_by"] == "good"
