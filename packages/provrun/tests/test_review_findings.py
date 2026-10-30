"""One test for each finding of the adversarial review of provrun."""
import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest
import yaml
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from provcat import Catalog
from provcat.errors import StoreError
from provrun import evidence
from provrun.backends.litellm import LiteLLMBackend
from provrun.backends.pydantic_ai import PydanticAIBackend
from provrun.cli import main as provr
from provrun.evidence import EvidenceError, read_events, verify
from provrun.resolve import ProvcatResolver
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec
from provrun.stagers import InProcessStager, InputCheckError

from .test_bundle import RUN_SCHEME, aws  # noqa: F401  (the moto fixture)


async def answer(**kw):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def litellm_agent(docs):
    return AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                     inputs=[InputSpec(f"file://{docs}/report.txt")])


def go(agent, catalog, runs, **kw):
    kw.setdefault("backend", LiteLLMBackend(answer))
    return asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                 env="dev", actor="t", **kw))


def test_2_a_line_separator_in_a_value_keeps_one_event_on_one_line(catalog, runs, docs, capsys):
    def fn(messages, info):
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(parts=[ToolCallPart("add_note", {"text": "line sep x\x85y"})])
        return ModelResponse(parts=[TextPart("ok")])

    agent = AgentSpec(name="n", backend="pydantic-ai", model="test", prompt="p", tools=["add_note"])
    out = go(agent, catalog, runs, backend=PydanticAIBackend(model=FunctionModel(fn)), mode="strict")
    assert out.status == "completed", out.error
    assert verify(out.folder / "evidence") == []
    note = next(e for e in read_events(out.folder / "evidence") if e.type == "note_added")
    assert note.data["text"] == "line sep x\x85y"
    assert provr(["--runs", str(runs), "show", out.run_id]) == 0
    assert provr(["--runs", str(runs), "verify", out.run_id]) == 0


def test_3_a_removed_tail_with_a_forged_manifest_fails(catalog, runs, docs):
    out = go(litellm_agent(docs), catalog, runs)
    ev = out.folder / "evidence"
    lines = (ev / "events.jsonl").read_text().split("\n")[:-1]
    kept = lines[:-2]
    (ev / "events.jsonl").write_text("\n".join(kept) + "\n")
    manifest = json.loads((ev / "manifest.json").read_text())
    manifest["last"] = evidence.sha256_bytes(kept[-1].encode())
    (ev / "manifest.json").write_text(json.dumps(manifest))
    problems = verify(ev)
    assert "the last event is not run_completed or run_failed" in problems
    assert any("the manifest counts" in p for p in problems)


def test_4_the_evidence_of_another_run_fails(catalog, runs, docs):
    a = go(litellm_agent(docs), catalog, runs)
    b = go(litellm_agent(docs), catalog, runs)
    for sub in ("evidence", "output"):
        shutil.rmtree(a.folder / sub)
        shutil.copytree(b.folder / sub, a.folder / sub)
    assert provr(["--runs", str(runs), "verify", a.run_id]) == 1


def test_5_a_planted_output_fails(catalog, runs, docs, capsys):
    out = go(litellm_agent(docs), catalog, runs)
    (out.folder / "output" / "extra.txt").write_text("planted")
    assert provr(["--runs", str(runs), "verify", out.run_id]) == 1
    assert "extra.txt is not in the evidence" in capsys.readouterr().err


class FailingCheck(InProcessStager):
    def verify_inputs(self, ctx, run):
        raise InputCheckError("forced")


def test_6_a_failed_run_records_what_it_left_in_output(catalog, runs, docs):
    out = go(litellm_agent(docs), catalog, runs, stager=FailingCheck())
    assert out.status == "failed"
    recorded = [e.data["path"] for e in read_events(out.folder / "evidence") if e.type == "output_artifact_created"]
    assert recorded == ["/output/response.txt"]
    assert provr(["--runs", str(runs), "verify", out.run_id]) == 0


class TamperingResolver(ProvcatResolver):
    def snapshot(self, ctx, grant):
        snap = super().snapshot(ctx, grant)
        snap.path.chmod(0o644)
        snap.path.write_text("Revenue was 1 million.")
        return snap


def test_8_a_changed_snapshot_is_found_before_the_agent_runs(catalog, runs, docs):
    calls = []

    async def record(**kw):
        calls.append(kw)
        return await answer(**kw)

    out = go(litellm_agent(docs), catalog, runs, resolver=TamperingResolver(catalog),
             backend=LiteLLMBackend(record))
    assert out.status == "failed" and isinstance(out.error, InputCheckError) and not calls
    assert "staged copy" in str(out.error)


def failing_append(monkeypatch, event_type):
    real = evidence.JsonlSink.append

    def append(self, line):
        if f'"type":"{event_type}"' in line:
            raise OSError(28, "No space left on device")
        real(self, line)

    monkeypatch.setattr(evidence.JsonlSink, "append", append)


def test_10_a_lost_line_in_strict_mode_does_not_break_the_chain(catalog, runs, docs, monkeypatch):
    failing_append(monkeypatch, "sandbox_started")
    with pytest.raises(EvidenceError, match="sandbox_started"):
        go(litellm_agent(docs), catalog, runs, mode="strict")
    [folder] = list(runs.iterdir())
    problems = verify(folder / "evidence")
    assert not any("chain is broken" in p or "seq is" in p for p in problems)
    assert any("lost events" in p and "sandbox_started" in p for p in problems)
    assert read_events(folder / "evidence")[-1].type == "run_failed"


def test_10_a_lost_ending_event_still_closes_the_evidence(catalog, runs, docs, monkeypatch):
    failing_append(monkeypatch, "run_completed")
    with pytest.raises(EvidenceError, match="run_completed"):
        go(litellm_agent(docs), catalog, runs, mode="strict")
    [folder] = list(runs.iterdir())
    assert (folder / "evidence" / "manifest.json").exists()
    assert (folder / "bundle.json").exists()
    assert not json.loads((folder / "evidence" / "manifest.json").read_text())["complete"]


def test_11_a_tool_call_that_fails_validation_is_still_recorded(catalog, runs):
    def fn(messages, info):
        n = sum(isinstance(m, ModelResponse) for m in messages)
        if n == 0:
            return ModelResponse(parts=[ToolCallPart("delete_everything", {}, tool_call_id="c0")])
        if n == 1:
            return ModelResponse(parts=[ToolCallPart("read_file", {"nonsense": 1}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("ok")])

    agent = AgentSpec(name="t", backend="pydantic-ai", model="test", prompt="p")
    out = go(agent, catalog, runs, backend=PydanticAIBackend(model=FunctionModel(fn)))
    asked = [c["tool"] for e in read_events(out.folder / "evidence") if e.type == "model_called"
             for c in e.data["tool_calls"]]
    assert asked == ["delete_everything", "read_file"]


def test_12_a_commit_taken_in_by_another_run_still_pins_the_bundle(aws, tmp_path, runs, docs, monkeypatch):  # noqa: F811
    catalog = Catalog({"schemes": RUN_SCHEME}, cache_root=tmp_path / "cache")
    real = catalog.commit

    def commit_then_nothing(uri, message, metadata=None):
        real(uri, "another run's commit", metadata)  # another run commits first, with this bundle in it
        raise StoreError(f"{uri}: nothing to commit")

    monkeypatch.setattr(catalog, "commit", commit_then_nothing)
    out = go(litellm_agent(docs), catalog, runs, bundle_scheme="run", mode="strict")
    assert out.status == "completed", out.error
    assert out.bundle.pinned and "took in this bundle" in out.bundle.note


def test_3_verify_compares_with_the_committed_bundle(aws, tmp_path, runs, docs, capsys):  # noqa: F811
    cfg = tmp_path / "provcat.yaml"
    cfg.write_text(yaml.safe_dump({"schemes": RUN_SCHEME, "cache": {"root": str(tmp_path / "cache")}}))
    catalog = Catalog.open(str(cfg))
    out = go(litellm_agent(docs), catalog, runs, bundle_scheme="run")
    assert provr(["--runs", str(runs), "verify", out.run_id, "--config", str(cfg)]) == 0
    assert "match the committed bundle" in capsys.readouterr().out
    m = out.folder / "evidence" / "manifest.json"
    m.write_text(json.dumps(json.loads(m.read_text())))  # the same facts, other bytes
    assert provr(["--runs", str(runs), "verify", out.run_id, "--config", str(cfg)]) == 1
    assert "differs from the committed bundle" in capsys.readouterr().err
