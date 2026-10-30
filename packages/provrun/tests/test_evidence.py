"""The evidence recorder: the chain, the payloads, the manifest and the modes."""
import json

import pytest

from provrun.evidence import EvidenceError, EvidenceRecorder, Sink, read_events, verify


def recorder(tmp_path, **kw):
    return EvidenceRecorder("run-1", "tester", tmp_path / "ev", **kw)


def three_steps(rec):
    rec.emit("run_created", "test")
    with rec.step("model_called", "test", data={"model": "m"}, payload="the prompt") as step:
        step.data["input_tokens"] = 3
    rec.emit("run_completed", "test")
    return rec.close()


def test_a_closed_run_verifies(tmp_path):
    manifest = three_steps(recorder(tmp_path))
    assert manifest["events"] == 3 and manifest["complete"]
    events = read_events(tmp_path / "ev")
    assert [e.type for e in events] == ["run_created", "model_called", "run_completed"]
    assert events[0].prev is None and all(e.prev for e in events[1:])
    assert events[1].data == {"model": "m", "input_tokens": 3}
    assert (tmp_path / "ev" / "payloads" / events[1].payload).read_text() == "the prompt"
    assert verify(tmp_path / "ev") == []


def lines(tmp_path):
    return (tmp_path / "ev" / "events.jsonl").read_text().splitlines()


def write(tmp_path, ls):
    (tmp_path / "ev" / "events.jsonl").write_text("\n".join(ls) + "\n")


def test_a_changed_event_fails(tmp_path):
    three_steps(recorder(tmp_path))
    ls = lines(tmp_path)
    ls[1] = ls[1].replace('"input_tokens":3', '"input_tokens":4')
    write(tmp_path, ls)
    problems = verify(tmp_path / "ev")
    assert any("chain is broken" in p for p in problems)


def test_a_removed_event_fails(tmp_path):
    three_steps(recorder(tmp_path))
    ls = lines(tmp_path)
    write(tmp_path, [ls[0], ls[2]])
    assert verify(tmp_path / "ev")


def test_a_removed_last_event_fails(tmp_path):
    three_steps(recorder(tmp_path))
    write(tmp_path, lines(tmp_path)[:2])
    assert any("manifest" in p for p in verify(tmp_path / "ev"))


def test_a_changed_payload_fails(tmp_path):
    three_steps(recorder(tmp_path))
    e = read_events(tmp_path / "ev")[1]
    p = tmp_path / "ev" / "payloads" / e.payload
    p.write_text("another prompt")
    assert any("was changed" in x for x in verify(tmp_path / "ev"))


def test_a_run_that_did_not_close_fails(tmp_path):
    rec = recorder(tmp_path)
    rec.emit("run_created", "test")
    assert any("did not close" in p for p in verify(tmp_path / "ev"))


def test_a_failed_step_writes_step_failed_and_raises(tmp_path):
    rec = recorder(tmp_path)
    with pytest.raises(RuntimeError):
        with rec.step("tool_called", "test", data={"tool": "t"}):
            raise RuntimeError("boom")
    [e] = read_events(tmp_path / "ev")
    assert e.type == "step_failed" and e.data["step"] == "tool_called"
    assert "boom" in e.data["error"]


def test_unknown_types_and_confidence_are_refused(tmp_path):
    rec = recorder(tmp_path)
    with pytest.raises(ValueError):
        rec.emit("model_calld", "test")
    with pytest.raises(ValueError):
        rec.emit("run_created", "test", confidence="sure")


def test_no_events_after_close(tmp_path):
    rec = recorder(tmp_path)
    rec.close()
    with pytest.raises(EvidenceError):
        rec.emit("run_created", "test")


class BrokenSink(Sink):
    def append(self, line):
        raise OSError("disk full")


def test_strict_mode_stops_on_a_lost_event(tmp_path):
    rec = recorder(tmp_path, sinks=[BrokenSink()])
    with pytest.raises(EvidenceError, match="disk full"):
        rec.emit("run_created", "test")


def test_lenient_mode_warns_and_marks_the_evidence(tmp_path):
    rec = recorder(tmp_path, sinks=[BrokenSink()], mode="lenient")
    with pytest.warns(UserWarning, match="disk full"):
        rec.emit("run_created", "test")
    manifest = rec.close()
    assert not manifest["complete"]
    assert any("lost events" in p for p in verify(tmp_path / "ev"))


def test_redaction_runs_before_the_hash(tmp_path):
    rec = recorder(tmp_path, redact=lambda d: {k: ("***" if k == "secret" else v) for k, v in d.items()})
    rec.emit("run_created", "test", data={"secret": "s3cr3t", "org": "acme"})
    rec.emit("run_completed", "test")
    rec.close()
    text = (tmp_path / "ev" / "events.jsonl").read_text()
    assert "s3cr3t" not in text and verify(tmp_path / "ev") == []


def test_spans_carry_the_ids_into_the_event(tmp_path):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("provrun")
    from provrun import otel
    old = otel._tracer
    otel._tracer = tracer
    try:
        rec = recorder(tmp_path)
        with rec.step("model_called", "test", data={"model": "m"}):
            pass
    finally:
        otel._tracer = old
    [span] = exporter.get_finished_spans()
    [e] = read_events(tmp_path / "ev")
    assert span.name == "model_called"
    assert span.attributes["gen_ai.operation.name"] == "chat"
    assert span.attributes["gen_ai.request.model"] == "m"
    assert e.data["span_id"] == format(span.context.span_id, "016x")
