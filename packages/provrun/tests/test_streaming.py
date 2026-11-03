"""A spec with stream: true asks for the reply in pieces, with no network and no keys.

The run hands each piece on as it comes, and the evidence records each whole
response once, as for a run that does not stream."""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai.models.function import FunctionModel

from provrun.backends.litellm import LiteLLMBackend
from provrun.backends.pydantic_ai import PydanticAIBackend
from provrun.context import RunContext
from provrun.evidence import read_events, verify
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec


@pytest.fixture
def pieces(monkeypatch):
    got = []
    monkeypatch.setattr(RunContext, "text", lambda self, piece: got.append(piece))
    return got


def run(agent, catalog, runs, backend):
    return asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="b", env="dev",
                                 actor="t", backend=backend))


def model_called(out):
    [event] = [e for e in read_events(out.folder / "evidence") if e.type == "model_called"]
    return event


def test_a_streamed_litellm_run_hands_on_pieces_and_records_the_whole_reply(catalog, runs, docs, pieces):
    # LiteLLM's own code makes the chunks from mock_response, so their shape is real.
    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-4o-mini", prompt="p", stream=True,
                      settings={"mock_response": "Revenue was 42 million."},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = run(agent, catalog, runs, LiteLLMBackend())
    assert out.status == "completed", out.error
    assert len(pieces) > 1 and "".join(pieces) == "Revenue was 42 million."
    assert (out.folder / "output" / "response.txt").read_text() == "Revenue was 42 million."
    event = model_called(out)
    assert event.data["streamed"] is True and event.data["finish_reason"] == "stop"
    assert event.data["input_tokens"] > 0 and event.data["output_tokens"] > 0
    assert isinstance(event.data["first_token_ms"], int)
    assert out.result.output == "Revenue was 42 million."
    assert verify(out.folder / "evidence") == []


def test_a_litellm_run_that_does_not_stream_says_so(catalog, runs, pieces):
    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-4o-mini", prompt="p",
                      settings={"mock_response": "42"})
    out = run(agent, catalog, runs, LiteLLMBackend())
    assert out.status == "completed", out.error
    event = model_called(out)
    assert event.data["streamed"] is False and "first_token_ms" not in event.data and pieces == []


def test_a_streamed_pydantic_ai_run_hands_on_pieces_and_records_the_whole_reply(catalog, runs, pieces):
    async def stream(messages, info):
        yield "Revenue "
        yield "was 42 million."

    agent = AgentSpec(name="q", backend="pydantic-ai", model="test", prompt="p", stream=True)
    out = run(agent, catalog, runs, PydanticAIBackend(model=FunctionModel(stream_function=stream)))
    assert out.status == "completed", out.error
    assert "".join(pieces) == "Revenue was 42 million." and len(pieces) == 2
    event = model_called(out)
    assert event.data["streamed"] is True and isinstance(event.data["first_token_ms"], int)
    assert (out.folder / "output" / "response.txt").read_text() == "Revenue was 42 million."
    assert verify(out.folder / "evidence") == []


def test_stream_must_be_true_or_false():
    base = {"name": "q", "backend": "litellm", "model": "m", "prompt": "p"}
    assert AgentSpec.from_dict({**base, "stream": True}).stream is True
    with pytest.raises(TypeError, match="stream"):
        AgentSpec.from_dict({**base, "stream": "yes"})


def _failed(out):
    [event] = [e for e in read_events(out.folder / "evidence") if e.type == "step_failed"]
    return event


def _partial(out, event):
    import json

    return json.loads((out.folder / "evidence" / "payloads" / event.payload).read_text())["partial_response"]


def test_a_litellm_stream_that_fails_partway_keeps_what_it_handed_on(catalog, runs, pieces):
    async def breaks(**kw):
        async def chunks():
            for text in ("Rev", "enue "):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)])
            raise ConnectionError("the connection dropped")
        return chunks()

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", stream=True)
    out = run(agent, catalog, runs, LiteLLMBackend(breaks))
    assert out.status == "failed" and pieces == ["Rev", "enue "]
    event = _failed(out)
    assert event.data["pieces"] == 2 and isinstance(event.data["first_token_ms"], int)
    assert _partial(out, event) == "Revenue "  # all that a viewer saw is in the evidence
    assert verify(out.folder / "evidence") == []


def test_a_pydantic_ai_stream_that_fails_partway_keeps_what_it_handed_on(catalog, runs, pieces):
    async def stream(messages, info):
        yield "Rev"
        raise ConnectionError("the connection dropped")

    agent = AgentSpec(name="q", backend="pydantic-ai", model="test", prompt="p", stream=True)
    out = run(agent, catalog, runs, PydanticAIBackend(model=FunctionModel(stream_function=stream)))
    assert out.status == "failed" and pieces == ["Rev"]
    event = _failed(out)
    assert event.data["pieces"] == 1 and _partial(out, event) == "Rev"
    assert verify(out.folder / "evidence") == []


def test_the_run_hands_each_piece_to_on_text(catalog, runs):
    got = []

    class Watched(LiteLLMBackend):
        async def execute(self, ctx, agent, run):
            ctx.on_text = got.append
            return await super().execute(ctx, agent, run)

    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-4o-mini", prompt="p", stream=True,
                      settings={"mock_response": "Revenue was 42 million."})
    out = run(agent, catalog, runs, Watched())
    assert out.status == "completed", out.error
    assert len(got) > 1 and "".join(got) == "Revenue was 42 million."


def test_the_capability_works_with_a_ctx_that_takes_no_new_attributes(tmp_path):
    # Other code passes its own ctx to evidence_capability; one with __slots__ must still work.
    from pydantic_ai import Agent

    from provrun.backends.pydantic_ai import evidence_capability
    from provrun.evidence import EvidenceRecorder

    class Slotted:
        __slots__ = ("recorder",)

    ctx = Slotted()
    ctx.recorder = EvidenceRecorder("r1", "t", tmp_path / "evidence")

    async def stream(messages, info):
        yield "42"

    agent = Agent(FunctionModel(stream_function=stream), capabilities=[evidence_capability(ctx)])

    async def go():
        async with agent.run_stream("go") as s:
            return await s.get_output()

    assert asyncio.run(go()) == "42"
    ctx.recorder.close()
    assert [e.type for e in read_events(tmp_path / "evidence")] == ["model_called"]
