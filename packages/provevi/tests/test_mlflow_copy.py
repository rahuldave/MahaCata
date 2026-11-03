"""The copy in MLflow: what each level holds, the places, the rebuild and the refusals."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from provevi import EvidenceEvent, FileStore, ProveviError, SqliteStore, sha256_bytes
from provevi.cli import main
from provevi.mlflow_copy import RUN_TAG, copy_run, tracking_target, ui_command
from provevi.telemetry import TelemetryPolicy

mlflow = pytest.importorskip("mlflow")
from mlflow import MlflowClient  # noqa: E402

RUN = "run-20261102T000000Z-00000001"
TRACE = "0ef832e64a31e762a3a71db85aad1dfb"
ROOT, MODEL, STAGE = "5cbc834163472ee3", "5456c737b6e94dda", "a7801c8ddb80b82a"
DOC = "data://acme/demo/dev/reports/report.txt"
PINNED = DOC + "?version=v1"
SHA = "205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2"


@pytest.fixture(autouse=True)
def no_mlflow_variables(monkeypatch):
    for key in [k for k in os.environ if k.startswith(("MLFLOW_", "OTEL_"))]:
        monkeypatch.delenv(key)


def events_of(run_id, *, spans=True, failed=False):
    """A run as provrun writes it: (type, component, refs, data, payload, seconds)."""
    run_uri = f"run://acme/demo/dev/{run_id}"
    ids = (lambda span, start: {"trace_id": TRACE, "span_id": span, "parent_span_id": ROOT,
                                "started_at": f"2026-11-02T00:00:{start:02}.000000Z"}) if spans else (lambda *a: {})
    spec = {"name": "q", "backend": "litellm", "model": "openai/gpt-4o-mini", "tools": [], "limits": {"steps": 3},
            "settings": {"temperature": 0}}
    created = {"org": "acme", "domain": "demo", "env": "dev", "mode": "strict", "agent": "q", "backend": "litellm",
               "spec": spec}
    if spans:
        created.update(trace_id=TRACE, span_id=ROOT)
    out = [
        ("run_created", "RunContext", [run_uri], created, None, 0),
        ("access_granted", "StubBroker", [DOC], {"grant_id": "g1", "mode": "mounted_snapshot", "reason": "ok"},
         None, 1),
        ("input_staged", "Stager", [DOC, PINNED], {"sha256": SHA, "agent_path": "/input/report.txt",
                                                    "name": "report.txt", **ids(STAGE, 1)}, None, 2),
        ("prompt_rendered", "Backend", [DOC], {"messages": 2}, b'[{"role": "user", "content": "the prompt"}]', 3),
        ("model_called", "Backend", [], {"model": "openai/gpt-4o-mini", "input_tokens": 10, "output_tokens": 20,
                                         "finish_reason": "stop", **ids(MODEL, 3)},
         json.dumps({"request": [{"role": "user", "content": "the prompt"}], "response": "42"}).encode(), 5),
    ]
    if failed:
        out.append(("step_failed", "Backend", [], {"error": "boom", **ids("1111111111111111", 5)}, None, 6))
        out.append(("run_failed", "RunContext", [run_uri], {"error": "boom", **({"trace_id": TRACE, "span_id": ROOT}
                                                                               if spans else {})}, None, 7))
    else:
        out.append(("run_completed", "RunContext", [run_uri], {"trace_id": TRACE, "span_id": ROOT} if spans else {},
                    None, 7))
    return out


def write_run(store, root: Path, run_id: str = RUN, **kw) -> Path:
    folder = root / run_id
    (folder / "output").mkdir(parents=True)
    writer = store.open_run(run_id, folder / "evidence")
    prev = None
    events = events_of(run_id, **kw)
    for seq, (t, component, refs, data, payload, sec) in enumerate(events):
        ref = writer.put_payload(payload) if payload is not None else None
        line = EvidenceEvent(run_id, seq, f"2026-11-02T00:00:{sec:02}.500000Z", t, component, "you@example.com",
                             tuple(refs), data, ref, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": run_id, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    (folder / "bundle.json").write_text(json.dumps({"uri": None, "pinned": None, "note": "none"}))
    writer.record_bundle(None, None)
    return folder


def own(level="bodies"):
    return TelemetryPolicy.from_dict({"version": 1, "destinations": {"own": {"kind": "mlflow", "receives": level}}})


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "evidence.db")
    write_run(s, tmp_path / "runs")
    return s


def client_of(store):
    return MlflowClient(tracking_uri=f"sqlite:///{store.path}")


def read_copy(client, copy):
    run = client.get_run(copy.mlflow_run_id)
    trace = client.get_trace(copy.trace_id) if copy.trace_id else None
    spans = {s.name: s for s in trace.data.spans} if trace else {}
    return run, trace, spans


def test_a_copy_at_bodies_holds_the_run_its_inputs_and_its_trace(store):
    copy = copy_run(store, RUN, own("bodies"), environ={})
    assert (copy.destination, copy.level, copy.spans) == ("own", "bodies", 3)
    client = client_of(store)
    run, trace, spans = read_copy(client, copy)
    assert client.get_experiment(copy.experiment_id).name == "acme/demo/dev/q"
    assert run.info.run_name == RUN and run.data.tags[RUN_TAG] == RUN
    assert run.data.tags["provrun.actor"] == "you@example.com"
    assert run.info.status == "FINISHED"
    assert run.data.params == {"agent": "q", "backend": "litellm", "limits": '{"steps": 3}', "mode": "strict",
                               "model": "openai/gpt-4o-mini", "tools": "[]"}
    assert run.data.metrics == {"model_calls": 1, "tool_calls": 0, "input_tokens": 10, "output_tokens": 20}
    [d] = run.inputs.dataset_inputs
    assert len(d.dataset.digest) == 32
    assert json.loads(d.dataset.source) == {"uri": DOC, "pinned": PINNED, "sha256": SHA}
    # provrun's IDs and times are kept, and the steps hang under the run.
    assert copy.trace_id == f"tr-{TRACE}"
    assert spans["run"].span_id == ROOT and spans["run"].parent_id is None
    assert spans["model_called"].span_id == MODEL and spans["model_called"].parent_id == ROOT
    assert spans["model_called"].start_time_ns == 1793577603_000_000_000
    assert spans["model_called"].end_time_ns == 1793577605_500_000_000
    assert spans["model_called"].span_type == "CHAT_MODEL"
    assert spans["model_called"].inputs == [{"role": "user", "content": "the prompt"}]
    assert spans["model_called"].outputs == "42"
    assert spans["input_staged"].attributes["provrun.refs"] == [DOC, PINNED]
    # The events with no span are events of the root span.
    assert [e.name for e in spans["run"].events] == ["access_granted", "prompt_rendered"]
    assert trace.info.tags[RUN_TAG] == RUN
    # MLflow adds nothing of its own, such as the user or the git commit.
    assert not [k for k in trace.info.trace_metadata if k.startswith(("mlflow.user", "mlflow.source"))]


def test_a_copy_at_names_holds_no_usage_and_no_bodies(store):
    copy = copy_run(store, RUN, own("names"), environ={})
    run, trace, spans = read_copy(client_of(store), copy)
    assert run.data.metrics == {}
    assert run.data.params == {"agent": "q", "backend": "litellm", "mode": "strict", "model": "openai/gpt-4o-mini"}
    model = spans["model_called"].attributes
    assert model["gen_ai.request.model"] == "openai/gpt-4o-mini"
    assert not [k for k in model if k.startswith("gen_ai.usage.") or k.startswith("provrun.data")]
    assert spans["model_called"].inputs is None and spans["model_called"].outputs is None
    assert spans["run"].events == []
    assert "provrun.refs" not in spans["input_staged"].attributes


def test_a_copy_at_usage_adds_the_counts_only(store):
    copy = copy_run(store, RUN, own("usage"), environ={})
    run, trace, spans = read_copy(client_of(store), copy)
    assert run.data.metrics["input_tokens"] == 10
    assert spans["model_called"].attributes["gen_ai.usage.output_tokens"] == 20
    assert spans["model_called"].outputs is None
    assert "provrun.data" not in spans["model_called"].attributes


def test_copying_again_rebuilds_the_copy(store):
    first = copy_run(store, RUN, own(), environ={})
    second = copy_run(store, RUN, own(), environ={})
    assert second.replaced == [first.mlflow_run_id]
    client = client_of(store)
    runs = client.search_runs([second.experiment_id], filter_string=f"tags.`{RUN_TAG}` = '{RUN}'")
    assert [r.info.run_id for r in runs] == [second.mlflow_run_id]
    assert len(client.search_traces(locations=[second.experiment_id])) == 1


def test_the_store_still_verifies_and_stays_in_wal_mode(store, tmp_path, capsys):
    copy_run(store, RUN, own(), environ={})
    assert main(["--runs", str(tmp_path / "runs"), "--store", f"sqlite:///{store.path}", "verify", RUN]) == 0
    assert sqlite3.connect(store.path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert sqlite3.connect(store.path).execute("SELECT count(*) FROM experiments").fetchone()[0] >= 1


def test_another_sqlite_file_is_made_in_wal_mode(store, tmp_path):
    path = tmp_path / "elsewhere" / "mlflow.db"
    sqlite3.connect(path.parent.mkdir() or path).close()  # a new file, in SQLite's default mode
    policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
        "lite": {"kind": "mlflow", "receives": "names", "endpoint": f"sqlite:///{path}"}}})
    copy = copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": f"sqlite:///{path}"})
    assert copy.destination == "lite"
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert MlflowClient(f"sqlite:///{path}").get_run(copy.mlflow_run_id).data.tags[RUN_TAG] == RUN


@pytest.mark.parametrize("environ, words", [
    ({}, "the store's own file"),
    ({"MLFLOW_TRACKING_URI": "https://evil.example"}, "https://evil.example:443"),
    ({"MLFLOW_TRACKING_URI": "postgresql://u:secret@db.example/m"}, "postgresql://db.example:5432/m"),
])
def test_a_place_that_the_policy_does_not_approve_gets_nothing(store, environ, words):
    with pytest.raises(ProveviError, match="approves no mlflow destination") as e:
        copy_run(store, RUN, TelemetryPolicy.deny_all(), environ=environ)
    assert words in str(e.value) and "secret" not in str(e.value)
    assert sqlite3.connect(store.path).execute(
        "SELECT count(*) FROM sqlite_master WHERE name = 'experiments'").fetchone()[0] == 0


def test_the_file_store_needs_a_tracking_uri(tmp_path):
    fs = FileStore(tmp_path / "runs")
    with pytest.raises(ProveviError, match="file store has no file"):
        tracking_target(own(), fs, {})


def test_a_run_that_does_not_verify_is_not_copied(store):
    conn = sqlite3.connect(store.path)
    conn.execute("UPDATE provevi_events SET line = replace(line, 'openai/gpt-4o-mini', 'other') WHERE seq = 4")
    conn.commit()
    conn.close()
    with pytest.raises(ProveviError, match="does not verify"):
        copy_run(store, RUN, own(), environ={})


def test_a_run_with_no_spans_is_copied_without_a_trace(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", spans=False)
    copy = copy_run(s, RUN, own(), environ={})
    assert copy.trace_id is None and copy.spans == 0
    assert client_of(s).get_run(copy.mlflow_run_id).info.status == "FINISHED"


def test_a_failed_run_is_failed_in_mlflow(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", failed=True)
    copy = copy_run(s, RUN, own(), environ={})
    run, trace, spans = read_copy(client_of(s), copy)
    assert run.info.status == "FAILED"
    assert spans["run"].status.status_code == "ERROR"
    assert spans["step_failed"].status.status_code == "ERROR"
    assert spans["model_called"].status.status_code == "OK"


def test_assessments_of_the_run_and_its_spans_are_copied(store):
    conn = sqlite3.connect(store.path)
    rows = [("a1", "run", RUN, "correct", "true", "matches the report", "human", "you@example.com"),
            ("a2", "span", MODEL, "grounded", '"yes"', "cites the report", "llm_judge", "judge"),
            ("a3", "span", "ffffffffffffffff", "other", "1", None, "code", None)]
    conn.executemany("INSERT INTO provevi_assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-11-02T00:01:00Z')", rows)
    conn.commit()
    conn.close()
    copy = copy_run(store, RUN, own("bodies"), environ={})
    assert copy.assessments == 2
    got = {a.name: a for a in client_of(store).get_trace(copy.trace_id).info.assessments}
    assert set(got) == {"correct", "grounded"}
    assert got["correct"].value is True and got["correct"].rationale == "matches the report"
    assert got["grounded"].span_id == MODEL and got["grounded"].source.source_type == "LLM_JUDGE"
    names = copy_run(store, RUN, own("names"), environ={})
    got = {a.name: a for a in client_of(store).get_trace(names.trace_id).info.assessments}
    assert got["correct"].rationale is None and got["grounded"].value == "yes"


def test_the_copy_does_not_import_litellm(store):
    # Importing LiteLLM fetches a price table and reads a .env file.
    code = ("import sys; from provevi import SqliteStore; from provevi.mlflow_copy import copy_run; "
            "from provevi.telemetry import TelemetryPolicy as P; "
            "p = P.from_dict({'version': 1, 'destinations': {'o': {'kind': 'mlflow', 'receives': 'usage'}}}); "
            f"copy_run(SqliteStore({str(store.path)!r}), {RUN!r}, p, environ={{}}); "
            "print(sys.modules.get('litellm', 'absent'))")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MLFLOW_", "OTEL_"))}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "absent"


def test_prove_mlflow_uses_the_policy_file(store, tmp_path, monkeypatch, capsys):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\ndestinations:\n  own: {kind: mlflow, receives: usage}\n")
    args = ["--runs", str(tmp_path / "runs"), "--store", f"sqlite:///{store.path}", "mlflow", RUN]
    assert main(args) == 1
    assert "approves no mlflow destination" in capsys.readouterr().err
    monkeypatch.setenv("PROVRUN_TELEMETRY_POLICY", str(policy))
    assert main(args) == 0
    out = capsys.readouterr().out
    assert f"{RUN}: copied to MLflow at sqlite:///" in out and "destination own, level usage" in out
    assert f"trace tr-{TRACE} with 3 spans" in out
    assert "View run" not in out


def test_the_viewer_listens_on_this_machine_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "https://elsewhere.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.example")
    command, env = ui_command(tmp_path / "e.db", 5099)
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--backend-store-uri") + 1] == f"sqlite:///{tmp_path / 'e.db'}"
    assert "MLFLOW_TRACKING_URI" not in env and "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["MLFLOW_DISABLE_TELEMETRY"] == "true"
    assert sqlite3.connect(tmp_path / "e.db").execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_rebuild_leaves_no_row_of_the_copy_before(store):
    first = copy_run(store, RUN, own(), environ={})
    copy_run(store, RUN, own(), environ={})
    conn = sqlite3.connect(store.path)
    for table in ("runs", "params", "tags", "metrics", "inputs"):
        column = "destination_id" if table == "inputs" else "run_uuid"
        n = conn.execute(f"SELECT count(*) FROM {table} WHERE {column} = ?", (first.mlflow_run_id,)).fetchone()[0]
        assert n == 0, table
    assert conn.execute("SELECT count(*) FROM datasets").fetchone()[0] == 1


def test_a_lower_level_copy_keeps_no_dataset_of_a_higher_one(store):
    copy_run(store, RUN, own("bodies"), environ={})
    names = copy_run(store, RUN, own("names"), environ={})
    [d] = client_of(store).get_run(names.mlflow_run_id).inputs.dataset_inputs
    assert json.loads(d.dataset.source) == {"sha256": SHA}
    sources = [r[0] for r in sqlite3.connect(store.path).execute("SELECT dataset_source FROM datasets")]
    assert sources == [d.dataset.source]


@pytest.mark.parametrize("where", ["_write_trace", "_write_assessments"])
def test_a_copy_that_fails_leaves_no_part_of_itself(store, monkeypatch, where):
    import provevi.mlflow_copy as m

    real = getattr(m, where)

    def broken(client, *a, **kw):
        real(client, *a, **kw)
        raise RuntimeError("the database went away")

    monkeypatch.setattr(m, where, broken)
    with pytest.raises(ProveviError, match="cannot take the copy.*went away"):
        copy_run(store, RUN, own(), environ={})
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM trace_info").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM inputs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0


@pytest.mark.parametrize("bad", ["x' OR '1' = '1", "a b", "", "-x", "x" * 300, 7])
def test_a_run_id_that_is_not_one_is_refused(store, bad):
    with pytest.raises(ProveviError, match="not a run ID"):
        copy_run(store, bad, own(), environ={})


@pytest.mark.parametrize("variable, url", [
    ("HTTPS_PROXY", "http://127.0.0.1:5999"),
    ("all_proxy", "https://127.0.0.1:5999"),
    ("PGHOST", "postgresql://127.0.0.1:5999/m"),
    ("PGSSLMODE", "postgresql+psycopg://127.0.0.1:5999/m"),
])
def test_a_variable_that_redirects_the_copy_is_refused(store, monkeypatch, variable, url):
    monkeypatch.setenv(variable, "elsewhere.example")
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
        "d": {"kind": "mlflow", "receives": "usage", "endpoint": url.replace("+psycopg", "")}}})
    with pytest.raises(ProveviError, match=f"{variable} would send the copy somewhere"):
        copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})


def test_a_proxy_from_the_system_settings_is_skipped(store, monkeypatch):
    import requests.utils

    import provevi.mlflow_copy as m

    monkeypatch.setattr(requests.utils, "getproxies", lambda: {"https": "http://proxy.example:8080"})
    assert requests.utils.get_environ_proxies("https://mlflow.example") != {}
    with m._mlflow_env(None):
        assert requests.utils.get_environ_proxies("https://mlflow.example") == {}
    assert "NO_PROXY" not in os.environ


@pytest.mark.parametrize("variable, url", [("MLFLOW_TRACKING_INSECURE_TLS", "https://127.0.0.1:5999"),
                                           ("MYSQL_TCP_PORT", "mysql://127.0.0.1/m")])
def test_a_variable_that_weakens_or_moves_the_connection_is_refused(store, monkeypatch, variable, url):
    monkeypatch.setenv(variable, "true")
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
        "d": {"kind": "mlflow", "receives": "usage", "endpoint": url}}})
    with pytest.raises(ProveviError, match=f"{variable} would send the copy somewhere"):
        copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})


def test_a_sqlite_url_with_a_driver_is_in_wal_mode_too(store, tmp_path):
    path = tmp_path / "driver.db"
    sqlite3.connect(path).close()
    policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
        "lite": {"kind": "mlflow", "receives": "names", "endpoint": f"sqlite:///{path}"}}})
    copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": f"sqlite+pysqlite:///{path}"})
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_names_keep_out_what_is_not_a_name(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    folder = write_run(s, tmp_path / "runs")
    conn = sqlite3.connect(s.path)
    rows = [("a1", "run", RUN, "score", "3", None, "code", "you@example.com"),
            ("a2", "run", RUN, "the secret plan", '"x"', None, "code", None),
            ("a3", "run", RUN, "ok", "true", None, "human", "a person with a secret")]
    conn.executemany("INSERT INTO provevi_assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-11-02T00:01:00Z')", rows)
    conn.commit()
    conn.close()
    names = copy_run(s, RUN, own("names"), environ={})
    got = {a.name: a for a in client_of(s).get_trace(names.trace_id).info.assessments}
    assert set(got) == {"score", "ok"}
    assert got["score"].value is None and got["ok"].value is True
    assert got["ok"].source.source_id == "provevi"
    usage = copy_run(s, RUN, own("usage"), environ={})
    got = {a.name: a for a in client_of(s).get_trace(usage.trace_id).info.assessments}
    assert got["score"].value == 3
    assert folder.exists()


@pytest.mark.parametrize("uri", ["https://bucket.example/report.txt?X-Amz-Signature=abc", DOC + "?at=v1;token=x",
                                 DOC + "?version=Bearer-token", "https://h.example/p/AKIAEXAMPLE/x"])
def test_below_bodies_a_dataset_holds_no_uri(uri):
    import provevi.mlflow_copy as m
    from mlflow.entities import Dataset

    event = EvidenceEvent(RUN, 2, "2026-11-02T00:00:02.500000Z", "input_staged", "Stager", "you", (DOC, uri),
                          {"sha256": SHA, "name": "the report, with notes"}, None, "observed", None)
    for level in ("names", "usage"):
        d = m._dataset(event, Dataset, level)
        assert json.loads(d.source) == {"sha256": SHA} and d.name == "input"
    assert json.loads(m._dataset(event, Dataset, "bodies").source)["pinned"] == uri


def test_below_bodies_an_agent_name_that_is_not_a_name_stays_out_of_the_experiment(tmp_path, monkeypatch):
    import provevi.mlflow_copy as m

    s = SqliteStore(tmp_path / "e.db")
    real = events_of

    def named(run_id, **kw):
        out = real(run_id, **kw)
        out[0][3]["agent"] = "a list for a person"
        return out

    monkeypatch.setattr(sys.modules[__name__], "events_of", named)
    write_run(s, tmp_path / "runs")
    client = client_of(s)
    names = copy_run(s, RUN, own("names"), environ={})
    assert client.get_experiment(names.experiment_id).name == "acme/demo/dev/agent"
    bodies = copy_run(s, RUN, own("bodies"), environ={})
    assert client.get_experiment(bodies.experiment_id).name == "acme/demo/dev/a list for a person"
    assert m.RUN_TAG



def test_a_database_that_cannot_be_reached_is_an_error_not_a_traceback(store, monkeypatch):
    import mlflow.store.db.utils as dbu

    monkeypatch.setattr(dbu, "MAX_RETRY_COUNT", 1)
    port = _free_port()
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
        "pg": {"kind": "mlflow", "receives": "usage", "endpoint": f"postgresql://127.0.0.1:{port}/m"}}})
    with pytest.raises(ProveviError, match="cannot take the copy") as e:
        copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": f"postgresql+psycopg://u:hunter2@127.0.0.1:{port}/m"})
    assert "hunter2" not in str(e.value)


def test_copies_at_the_same_time_leave_one_copy(store):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(3) as pool:
        copies = list(pool.map(lambda _: copy_run(store, RUN, own(), environ={}), range(3)))
    runs = client_of(store).search_runs([copies[0].experiment_id])
    assert [r.info.run_id for r in runs] in [[c.mlflow_run_id] for c in copies]


def test_a_step_with_no_start_is_a_span_of_no_length(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    root = tmp_path / "runs"
    folder = root / RUN
    (folder / "output").mkdir(parents=True)
    writer = s.open_run(RUN, folder / "evidence")
    prev = None
    events = events_of(RUN)
    events[4][3].pop("started_at")
    for seq, (t, component, refs, data, payload, sec) in enumerate(events):
        ref = writer.put_payload(payload) if payload is not None else None
        line = EvidenceEvent(RUN, seq, f"2026-11-02T00:00:{sec:02}.500000Z", t, component, "you@example.com",
                             tuple(refs), data, ref, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": RUN, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    copy = copy_run(s, RUN, own(), environ={})
    _, _, spans = read_copy(client_of(s), copy)
    assert spans["model_called"].start_time_ns == spans["model_called"].end_time_ns
    assert spans["model_called"].parent_id == ROOT


def test_the_copy_puts_the_variables_back_and_writes_nothing_here(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = dict(os.environ)
    copy_run(store, RUN, own(), environ={})
    assert dict(os.environ) == before
    assert not (tmp_path / "mlruns").exists()
    places = [r[0] for r in sqlite3.connect(store.path).execute("SELECT artifact_location FROM experiments")]
    assert places and all(p.startswith(store.path.parent.as_uri()) for p in places), places


LIB = "c0ffee00c0ffee00"


def run_with_a_library_span(tmp_path, library="pydantic-ai"):
    s = SqliteStore(tmp_path / "e.db")
    folder = tmp_path / "runs" / RUN
    (folder / "output").mkdir(parents=True)
    writer = s.open_run(RUN, folder / "evidence")
    events = events_of(RUN)
    span = {"name": "agent run", "library": library, "trace_id": TRACE, "span_id": LIB, "parent_span_id": MODEL,
            "start_time": 1793577603_100_000_000, "end_time": 1793577604_000_000_000, "status": "ERROR",
            "attributes": {"gen_ai.request.model": "gpt-4o-mini", "gen_ai.usage.input_tokens": 7}}
    body = {"attributes": {"gen_ai.prompt": "the secret prompt"},
            "events": [{"name": "retry", "timestamp": 1793577603_500_000_000, "attributes": {"why": "slow"}}],
            "links": []}
    events.insert(5, ("span_recorded", "telemetry", [], span, json.dumps(body).encode(), 5))
    prev = None
    for seq, (t, component, refs, data, payload, sec) in enumerate(events):
        ref = writer.put_payload(payload) if payload is not None else None
        line = EvidenceEvent(RUN, seq, f"2026-11-02T00:00:{sec:02}.500000Z", t, component, "you@example.com",
                             tuple(refs), data, ref, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": RUN, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    return s


def feeding(level, feeds=("own",)):
    return TelemetryPolicy.from_dict({"version": 1,
                                      "libraries": {"pydantic-ai": {"versions": ">=1", "telemetry": list(feeds)}},
                                      "destinations": {"own": {"kind": "mlflow", "receives": level}}})


def test_a_library_span_is_copied_when_the_policy_lets_it_feed_the_place(tmp_path):
    s = run_with_a_library_span(tmp_path)
    copy = copy_run(s, RUN, feeding("bodies"), environ={})
    assert copy.spans == 4
    _, _, spans = read_copy(client_of(s), copy)
    lib = spans["agent run"]
    assert lib.span_id == LIB and lib.parent_id == MODEL and lib.status.status_code == "ERROR"
    assert (lib.start_time_ns, lib.end_time_ns) == (1793577603_100_000_000, 1793577604_000_000_000)
    assert lib.attributes["gen_ai.prompt"] == "the secret prompt"
    assert lib.attributes["provrun.library"] == "pydantic-ai"
    assert [e.name for e in lib.events] == ["retry"]


def test_a_library_span_below_bodies_holds_no_payload(tmp_path):
    s = run_with_a_library_span(tmp_path)
    _, _, spans = read_copy(client_of(s), copy_run(s, RUN, feeding("names"), environ={}))
    lib = spans["agent run"].attributes
    assert lib["gen_ai.request.model"] == "gpt-4o-mini"
    assert "gen_ai.prompt" not in lib and "gen_ai.usage.input_tokens" not in lib
    assert spans["agent run"].events == []


def test_a_library_span_of_a_namespace_package_is_found_by_its_distribution(tmp_path):
    s = run_with_a_library_span(tmp_path, library="opentelemetry.sdk.trace")
    policy = TelemetryPolicy.from_dict({"version": 1,
                                        "libraries": {"opentelemetry-sdk": {"versions": ">=1", "telemetry": ["own"]}},
                                        "destinations": {"own": {"kind": "mlflow", "receives": "names"}}})
    assert copy_run(s, RUN, policy, environ={}).spans == 4


def test_a_library_span_whose_library_cannot_be_shown_stays_out(tmp_path):
    s = run_with_a_library_span(tmp_path, library="opentelemetry.instrumentation.nothere")
    policy = TelemetryPolicy.from_dict({"version": 1,
                                        "libraries": {"opentelemetry-sdk": {"versions": ">=1", "telemetry": ["own"]}},
                                        "destinations": {"own": {"kind": "mlflow", "receives": "names"}}})
    assert copy_run(s, RUN, policy, environ={}).spans == 3


@pytest.mark.parametrize("policy", [feeding("bodies", feeds=()), own("bodies")])
def test_a_library_span_stays_out_when_the_policy_does_not_let_it_feed_the_place(tmp_path, policy):
    s = run_with_a_library_span(tmp_path)
    copy = copy_run(s, RUN, policy, environ={})
    assert copy.spans == 3
    assert "agent run" not in read_copy(client_of(s), copy)[2]


@pytest.mark.parametrize("value, kept", [("openai/gpt-4o-mini", True), ("claude-sonnet@20240620", True),
                                         ("https://u:secret@h.example/v1", False), ("u:secret@h.example", False),
                                         ("token=abc", False), ("/home/someone/agent.py", False),
                                         ("ns:model@tag", False), ("bedrock/arn:aws:bedrock:us-east-1:1:x", True),
                                         (["a", "https://h.example/x"], False)])
def test_below_bodies_a_name_holds_no_url_password_setting_or_path(value, kept):
    import provevi.mlflow_copy as m

    assert m._keep(value, "names") is kept and m._keep(value, "bodies") is True


def test_below_bodies_a_library_span_keeps_no_secret_in_its_name_or_attributes(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    folder = tmp_path / "runs" / RUN
    (folder / "output").mkdir(parents=True)
    writer = s.open_run(RUN, folder / "evidence")
    events = events_of(RUN)
    span = {"name": "host:token@x", "library": "pydantic-ai", "trace_id": TRACE, "span_id": LIB,
            "parent_span_id": MODEL, "start_time": 1793577603_100_000_000, "end_time": 1793577604_000_000_000,
            "status": "OK", "attributes": {"gen_ai.request.model": "https://u:secret@h.example/m",
                                           "gen_ai.system": "openai"}}
    events.insert(5, ("span_recorded", "telemetry", [], span, None, 5))
    prev = None
    for seq, (t, component, refs, data, payload, sec) in enumerate(events):
        line = EvidenceEvent(RUN, seq, f"2026-11-02T00:00:{sec:02}.500000Z", t, component, "you@example.com",
                             tuple(refs), data, None, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": RUN, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    _, _, spans = read_copy(client_of(s), copy_run(s, RUN, feeding("names"), environ={}))
    lib = spans["span"]
    assert lib.span_id == LIB and "gen_ai.request.model" not in lib.attributes
    assert lib.attributes["gen_ai.system"] == "openai"


@pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "MLFLOW_TRACKING_SERVER_CERT_PATH",
                                      "MLFLOW_TRACKING_AUTH"])
def test_a_variable_that_changes_whom_the_copy_trusts_is_refused(store, monkeypatch, variable):
    monkeypatch.setenv(variable, "/somewhere/else.pem")
    url = "https://127.0.0.1:5999"
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
        "d": {"kind": "mlflow", "receives": "usage", "endpoint": url}}})
    with pytest.raises(ProveviError, match=f"{variable} would send the copy somewhere.*trusts"):
        copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.integration
def test_a_copy_through_a_tracking_server(store, tmp_path):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MLFLOW_", "OTEL_"))}
    env.update(MLFLOW_DISABLE_TELEMETRY="true", MLFLOW_DISABLE_AGENT_HINT="1")
    server = subprocess.Popen([str(Path(sys.executable).with_name("mlflow")), "server", "--host", "127.0.0.1",
                               "--port", str(port), "--backend-store-uri", f"sqlite:///{tmp_path / 'srv.db'}",
                               "--default-artifact-root", str(tmp_path / "art")],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.5)
        policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
            "server": {"kind": "mlflow", "receives": "usage", "endpoint": url}}})
        first = copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})
        copy = copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})
        assert copy.replaced == [first.mlflow_run_id]
        client = MlflowClient(url)
        [trace] = client.search_traces(locations=[copy.experiment_id], run_id=copy.mlflow_run_id)
        assert trace.info.trace_id == f"tr-{TRACE}" and trace.info.tags[RUN_TAG] == RUN
        assert {s.name for s in trace.data.spans} == {"run", "input_staged", "model_called"}
        assert client.get_run(copy.mlflow_run_id).data.metrics["output_tokens"] == 20
    finally:
        server.terminate()
        server.wait(timeout=30)


@pytest.mark.integration
def test_a_copy_into_postgres(store):
    import shutil

    if shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("no docker")
    port = _free_port()
    name = f"provevi-pg-{port}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=test",
                    "-e", "POSTGRES_DB=mlflow", "-p", f"127.0.0.1:{port}:5432", "postgres:17-alpine"],
                   check=True, capture_output=True, timeout=120)
    try:
        for _ in range(120):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "mlflow"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(0.5)
        time.sleep(1)  # the image restarts Postgres once after it makes the database
        url = f"postgresql+psycopg://postgres:test@127.0.0.1:{port}/mlflow?sslmode=disable"
        policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["127.0.0.1"], "destinations": {
            "pg": {"kind": "mlflow", "receives": "bodies", "endpoint": f"postgresql://127.0.0.1:{port}/mlflow?sslmode=disable"}}})
        copy = copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})
        assert copy.destination == "pg" and "test@" not in copy.tracking_uri
        again = copy_run(store, RUN, policy, environ={"MLFLOW_TRACKING_URI": url})
        assert again.replaced == [copy.mlflow_run_id]
        client = MlflowClient(url)
        [trace] = client.search_traces(locations=[again.experiment_id], run_id=again.mlflow_run_id)
        assert trace.info.trace_id == f"tr-{TRACE}" and len(trace.data.spans) == 3
        assert client.get_run(again.mlflow_run_id).data.tags[RUN_TAG] == RUN
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def test_the_redirect_check_reads_the_variables_it_is_given(monkeypatch):
    from provevi.mlflow_copy import refuse_redirects

    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    with pytest.raises(ProveviError, match="HTTPS_PROXY"):
        refuse_redirects("https://mlflow.example", {"HTTPS_PROXY": "http://proxy.example:3128"})
    refuse_redirects("https://mlflow.example", {})
    refuse_redirects("sqlite:///tmp/m.db", {"HTTPS_PROXY": "http://proxy.example:3128", "PGHOST": "db"})
    with pytest.raises(ProveviError, match="PGHOST"):
        refuse_redirects("postgresql://db.example/m", {"PGHOST": "elsewhere"})
