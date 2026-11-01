"""The evidence recorder: the chain, the payloads, the manifest and the modes."""
import json

import pytest

from provrun.evidence import EvidenceError, EvidenceRecorder, Sink, read_events, sha256_bytes, verify


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


def test_the_tracer_claims_no_schema_url(monkeypatch):
    """The GenAI names have no released schema, so provrun names none."""
    import importlib

    from opentelemetry import trace

    from provrun import otel

    seen = {}
    real = trace.get_tracer

    def spy(*a, **kw):
        seen.update(kw, name=a[0])
        return real(*a, **kw)

    monkeypatch.setattr(trace, "get_tracer", spy)
    try:
        importlib.reload(otel)
    finally:
        monkeypatch.undo()
        importlib.reload(otel)
    assert seen["name"] == "provrun" and seen.get("schema_url") is None


# Each test below is one finding of the review of provevi's Phase 1.

def test_a_store_that_is_falsy_is_still_used(tmp_path):
    from provevi import FileStore

    class Store(FileStore):
        runs: list = []

        def __len__(self):
            return len(self.runs)

        def open_run(self, run_id, folder):
            self.runs.append(run_id)
            return super().open_run(run_id, folder)

    s = Store()
    EvidenceRecorder("run-x", "t", tmp_path / "evidence", store=s)
    assert s.runs == ["run-x"]


def _failing_store(where):
    import sqlite3

    from provevi import FileStore
    from provevi.store import FileRunWriter

    class Writer(FileRunWriter):
        def append(self, line):
            if where == "append":
                raise sqlite3.OperationalError("database is locked")
            super().append(line)

        def seal(self, manifest):
            if where == "seal":
                raise sqlite3.OperationalError("database is locked")
            super().seal(manifest)

    class Store(FileStore):
        def open_run(self, run_id, folder):
            return Writer(folder)

    return Store()


def test_any_store_error_is_lost_in_lenient_mode_and_recorded(tmp_path):
    r = EvidenceRecorder("run-x", "t", tmp_path / "evidence", mode="lenient", store=_failing_store("append"))
    with pytest.warns(UserWarning, match="database is locked"):
        assert r.emit("run_created", "test") is None
    assert r.problems and "database is locked" in r.problems[0]
    with pytest.raises(EvidenceError, match="database is locked"):
        EvidenceRecorder("run-y", "t", tmp_path / "e2", store=_failing_store("append")).emit("run_created", "t")


def test_a_seal_that_fails_is_reported(tmp_path):
    r = EvidenceRecorder("run-x", "t", tmp_path / "evidence", store=_failing_store("seal"))
    r.emit("run_created", "test")
    with pytest.raises(EvidenceError, match="cannot seal the evidence"):
        r.close()
    r = EvidenceRecorder("run-y", "t", tmp_path / "e2", mode="lenient", store=_failing_store("seal"))
    with pytest.raises(EvidenceError, match="cannot seal"):
        r.close()


def test_a_store_that_keeps_no_files_is_refused_at_close(tmp_path):
    from provevi import EvidenceStore, RunWriter

    class Memory(RunWriter):
        def __init__(self):
            self.lines, self.blobs, self.sealed = [], {}, None

        def append(self, line):
            self.lines.append(line)

        def close(self):
            pass

        def put_payload(self, data):
            raw = data.encode() if isinstance(data, str) else data
            self.blobs[sha256_bytes(raw)] = raw
            return sha256_bytes(raw)

        def get_payload(self, digest):
            return self.blobs[digest]

        def payload_names(self):
            return sorted(self.blobs)

        def seal(self, manifest):
            self.sealed = manifest

        def manifest(self):
            return self.sealed

    class Store(EvidenceStore):
        def open_run(self, run_id, folder):
            return Memory()

    for mode in ("strict", "lenient"):
        r = EvidenceRecorder("run-x", "t", tmp_path / mode, mode=mode, store=Store())
        r.emit("run_created", "test")
        with pytest.raises(EvidenceError, match="kept no events.jsonl"):
            r.close()


def test_the_recorder_still_has_its_payload_store(tmp_path):
    r = EvidenceRecorder("run-x", "t", tmp_path / "evidence")
    digest = r.writer.put_payload(b"x")
    assert r.payloads.get(digest) == b"x"
