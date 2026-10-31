"""Whole runs through run_agent, with fake models, so no network and no keys."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from provrun.backends.litellm import LiteLLMBackend
from provrun.backends.pydantic_ai import PydanticAIBackend
from provrun.cli import main as provr
from provrun.evidence import read_events, verify
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec


def run(agent, catalog, runs, **kw):
    kw.setdefault("org", "acme")
    kw.setdefault("domain", "benefits")
    kw.setdefault("env", "dev")
    kw.setdefault("actor", "tester")
    return asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, **kw))


def types(outcome):
    return [e.type for e in read_events(outcome.folder / "evidence")]


# litellm

def fake_completion(text="Revenue was 42 million."):
    calls = []

    async def acompletion(**kw):
        calls.append(kw)
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=5)
        choice = SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage)

    return acompletion, calls


def test_a_litellm_run_records_every_step(catalog, runs, docs):
    acompletion, calls = fake_completion()
    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-x", prompt="What was revenue?",
                      instructions="Answer from the document.", inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = run(agent, catalog, runs, backend=LiteLLMBackend(acompletion))
    assert out.status == "completed", out.error
    assert types(out) == [
        "run_created", "access_requested", "access_granted", "context_resolved", "snapshot_materialized",
        "input_staged", "sandbox_started", "prompt_rendered", "model_called", "sandbox_completed",
        "input_verified", "output_artifact_created", "run_completed",
    ]
    assert (out.folder / "output" / "response.txt").read_text() == "Revenue was 42 million."
    user = calls[0]["messages"][1]["content"]
    assert "Revenue in 2025" in user[0]["text"] and user[-1]["text"] == "What was revenue?"
    model = next(e for e in read_events(out.folder / "evidence") if e.type == "model_called")
    assert model.data["input_tokens"] == 11 and model.data["model"] == "openai/gpt-x"
    assert verify(out.folder / "evidence") == []
    assert out.bundle.uri is None and "no store for runs" in out.bundle.note


def test_a_pdf_goes_in_as_a_file_block(catalog, runs, tmp_path):
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    acompletion, calls = fake_completion()
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="Summarize.",
                      inputs=[InputSpec(f"file://{pdf}")])
    out = run(agent, catalog, runs, backend=LiteLLMBackend(acompletion))
    assert out.status == "completed", out.error
    block = calls[0]["messages"][0]["content"][0]
    assert block["type"] == "file" and block["file"]["file_data"].startswith("data:application/pdf;base64,")
    prompt = next(e for e in read_events(out.folder / "evidence") if e.type == "prompt_rendered")
    assert "base64" not in (out.folder / "evidence" / "payloads" / prompt.payload).read_text()


def test_a_denied_input_fails_the_run_and_the_evidence_says_why(catalog, runs, docs):
    acompletion, calls = fake_completion()
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = run(agent, catalog, runs, env="prod", backend=LiteLLMBackend(acompletion), mode="strict")
    assert out.status == "failed" and not calls
    assert types(out)[-2:] == ["access_denied", "run_failed"]
    assert verify(out.folder / "evidence") == []


def test_a_model_error_is_a_failed_step_and_a_failed_run(catalog, runs):
    async def broken(**kw):
        raise ConnectionError("no route")

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p")
    out = run(agent, catalog, runs, backend=LiteLLMBackend(broken))
    events = read_events(out.folder / "evidence")
    assert out.status == "failed"
    failed = next(e for e in events if e.type == "step_failed")
    assert failed.data["step"] == "model_called" and "no route" in failed.data["error"]
    assert events[-1].type == "run_failed"


def test_a_timeout_fails_the_run(catalog, runs):
    async def slow(**kw):
        await asyncio.sleep(5)

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", limits={"timeout": 0.05})
    out = run(agent, catalog, runs, backend=LiteLLMBackend(slow))
    assert out.status == "failed" and isinstance(out.error, TimeoutError)
    done = next(e for e in read_events(out.folder / "evidence") if e.type == "sandbox_completed")
    assert done.data["status"] == "failed"


# pydantic-ai

def scripted(steps):
    """A FunctionModel that makes the tool calls in steps, one response each,
    and then answers with the last tool result."""
    def fn(messages, info):
        n = sum(1 for m in messages if isinstance(m, ModelResponse))
        if n < len(steps):
            name, args = steps[n]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{n}")])
        last = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        return ModelResponse(parts=[TextPart(f"done: {last[-1].content if last else ''}"[:200])])
    return FunctionModel(fn)


def test_a_pydantic_ai_run_reads_input_writes_output_and_records_tools(catalog, runs, docs):
    model = scripted([
        ("read_file", {"path": "/input/report.txt"}),
        ("grep_file", {"path": "/input/report.txt", "pattern": "costs"}),
        ("add_note", {"text": "revenue is on line 1", "refs": ["/input/report.txt"]}),
        ("write_file", {"path": "/output/answer.txt", "content": "42 million"}),
    ])
    agent = AgentSpec(name="reader", backend="pydantic-ai", model="test", prompt="Find revenue.",
                      inputs=[InputSpec(f"file://{docs}/report.txt")], tools=["grep_file", "add_note"])
    out = run(agent, catalog, runs, backend=PydanticAIBackend(model=model))
    assert out.status == "completed", out.error
    events = read_events(out.folder / "evidence")
    tools = [e.data["tool"] for e in events if e.type == "tool_called"]
    assert tools == ["read_file", "grep_file", "add_note", "write_file"]
    assert sum(e.type == "model_called" for e in events) == 5
    note = next(e for e in events if e.type == "note_added")
    assert note.component == "agent" and note.data["text"] == "revenue is on line 1"
    grep = next(e for e in events if e.type == "tool_called" and e.data["tool"] == "grep_file")
    assert "Costs were 30" in (out.folder / "evidence" / "payloads" / grep.payload).read_text()
    imported = next(e for e in events if e.type == "trace_imported")
    assert imported.confidence == "parsed"
    assert (out.folder / "output" / "answer.txt").read_text() == "42 million"
    outputs = sorted(e.data["path"] for e in events if e.type == "output_artifact_created")
    assert outputs == ["/output/answer.txt", "/output/response.txt"]
    assert provr(["--runs", str(runs), "verify", out.run_id]) == 0


def test_the_agent_cannot_write_its_inputs(catalog, runs, docs):
    model = scripted([("write_file", {"path": "/input/report.txt", "content": "forged"})])
    agent = AgentSpec(name="w", backend="pydantic-ai", model="test", prompt="p",
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = run(agent, catalog, runs, backend=PydanticAIBackend(model=model))
    assert (out.folder / "input" / "report.txt").read_text().startswith("Revenue in 2025")
    assert out.status == "completed", out.error
    events = read_events(out.folder / "evidence")
    refused = next(e for e in events if e.type == "step_failed")
    assert refused.data["step"] == "tool_called" and "read-only" in refused.data["error"]
    checks = [e for e in events if e.type == "input_verified"]
    assert checks and all(e.data["unchanged"] for e in checks)


def test_an_unknown_tool_fails_the_run(catalog, runs):
    agent = AgentSpec(name="x", backend="pydantic-ai", model="test", prompt="p", tools=["nope"])
    out = run(agent, catalog, runs, backend=PydanticAIBackend(model=scripted([])))
    assert out.status == "failed" and "unknown tools" in str(out.error)


def test_cancel_records_the_run(catalog, runs):
    async def slow(**kw):
        await asyncio.sleep(5)

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p")

    async def go():
        task = asyncio.create_task(run_agent(agent, catalog=catalog, runs_root=runs, org="acme",
                                             domain="b", env="dev", actor="t", backend=LiteLLMBackend(slow)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    [folder] = list(runs.iterdir())
    assert read_events(folder / "evidence")[-1].type == "run_failed"
    assert verify(folder / "evidence") == []


def test_provr_show_and_a_tampered_output(catalog, runs, docs, capsys):
    acompletion, _ = fake_completion()
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = run(agent, catalog, runs, backend=LiteLLMBackend(acompletion))
    assert provr(["--runs", str(runs), "show", out.run_id]) == 0
    assert "model_called" in capsys.readouterr().out
    (out.folder / "output" / "response.txt").write_text("tampered")
    assert provr(["--runs", str(runs), "verify", str(out.folder)]) == 1
    assert "was changed" in capsys.readouterr().err


def test_litellm_otel_loggers_get_their_own_span(catalog, runs, docs, monkeypatch):
    import warnings

    from provrun.backends.litellm import OTEL_SPAN_SETTING

    seen = []

    async def acompletion(**kw):
        import os
        seen.append(os.environ.get(OTEL_SPAN_SETTING))
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-x", prompt="p")
    monkeypatch.delenv(OTEL_SPAN_SETTING, raising=False)
    run(agent, catalog, runs, backend=LiteLLMBackend(acompletion))
    assert seen == ["true"]  # whatever loggers are on, such as "logfire" or a subclass
    monkeypatch.setenv(OTEL_SPAN_SETTING, "false")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run(agent, catalog, runs, backend=LiteLLMBackend(acompletion))
    assert seen[-1] == "false" and any(OTEL_SPAN_SETTING in str(w.message) for w in caught)
