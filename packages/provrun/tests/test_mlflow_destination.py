"""The MLflow destination: a sealed run is copied into the place the policy approves, and only there."""
import json
import os
import socket
import time
import warnings
from pathlib import Path

import pytest
from provevi import SqliteStore
from provevi.telemetry import TelemetryPolicy

from provrun import destinations
from provrun.evidence import read_events

from .test_destinations import real_spans  # noqa: F401
from .test_telemetry import blocked, created, run

mlflow = pytest.importorskip("mlflow")
from mlflow import MlflowClient  # noqa: E402


@pytest.fixture(autouse=True)
def no_mlflow_variables(monkeypatch):
    for key in [k for k in os.environ if k.startswith(("MLFLOW_", "OTEL_", "PG"))]:
        monkeypatch.delenv(key)


def own(level="names", endpoint=None):
    dest = {"kind": "mlflow", "receives": level, **({"endpoint": endpoint} if endpoint else {})}
    return TelemetryPolicy.from_dict({"version": 1, "destinations": {"own": dest}})


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "evidence.db")


def spans_of(uri, copy):
    trace = MlflowClient(tracking_uri=uri).get_trace(copy["trace_id"])
    return {s.name: s for s in trace.data.spans}


def test_a_sealed_run_is_copied_into_the_store_s_own_file_at_the_level(catalog, runs, store):
    out = run(catalog, runs, store=store, policy=own("names"))
    assert out.status == "completed", out.error
    copy = out.mlflow
    assert copy["copied"] is True and copy["destination"] == "own" and copy["receives"] == "names"
    assert copy["tracking_uri"] == f"sqlite:///{store.path}"
    assert out.to_dict()["mlflow"] == copy
    spans = spans_of(copy["tracking_uri"], copy)
    assert {"run", "model_called"} <= set(spans)
    assert not [k for k in spans["model_called"].attributes if k.startswith("gen_ai.usage.")]
    [route] = created(out)["destinations"]
    assert route == {"kind": "mlflow", "variable": "MLFLOW_TRACKING_URI", "url": "<the store's own file>",
                     "destination": "own", "receives": "names", "sending": True,
                     "note": "copied into MLflow after the run is sealed"}


def test_an_approved_tracking_uri_gets_the_copy_with_usage(catalog, runs, store, tmp_path, monkeypatch):
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    out = run(catalog, runs, store=store, policy=own("usage", endpoint=uri))
    assert out.mlflow["copied"] is True and out.mlflow["tracking_uri"] == uri
    model = spans_of(uri, out.mlflow)["model_called"].attributes
    assert "gen_ai.usage.input_tokens" in model


def test_a_tracking_uri_the_policy_does_not_approve_gets_nothing(catalog, runs, store, tmp_path, monkeypatch):
    other = tmp_path / "other.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{other}")
    out = run(catalog, runs, store=store, policy=own("bodies", endpoint=f"sqlite:///{tmp_path / 'mlflow.db'}"))
    assert out.status == "completed", out.error
    assert out.mlflow is None and "mlflow" not in out.to_dict()
    assert not other.exists()
    [b] = blocked(out, "destination")
    assert b["detail"] == "MLFLOW_TRACKING_URI: matches no mlflow destination of the policy; nothing is copied"
    # No local path of the URL reaches the evidence.
    assert str(tmp_path) not in (out.folder / "evidence" / "events.jsonl").read_text()
    assert created(out)["destinations"][0]["url"] == "sqlite:<path not recorded>"
    assert created(out)["destinations"][0]["sending"] is False


def test_no_variable_and_no_own_place_is_no_destination(catalog, runs, store):
    policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
        "elsewhere": {"kind": "mlflow", "receives": "names", "endpoint": "https://mlflow.example"}},
        "hosts": ["mlflow.example"]})
    out = run(catalog, runs, store=store, policy=policy)
    assert out.mlflow is None and created(out)["destinations"] == []


def test_the_file_store_with_no_variable_warns_and_copies_nothing(catalog, runs):
    with pytest.warns(UserWarning, match="file store has no file for MLflow"):
        out = run(catalog, runs, policy=own())
    assert out.status == "completed", out.error
    assert out.mlflow is None
    [route] = created(out)["destinations"]
    assert route["destination"] == "own" and route["sending"] is False
    assert "nothing is copied" in route["note"]
    assert blocked(out, "destination") == []


def test_a_copy_that_fails_warns_and_keeps_the_run(catalog, runs, store, monkeypatch):
    monkeypatch.setattr(destinations, "_copy_in_process", lambda *a: {"error": "MLflow cannot be reached"})
    with pytest.warns(UserWarning, match="was not copied into MLflow: MLflow cannot be reached"):
        out = run(catalog, runs, store=store, policy=own())
    assert out.status == "completed"
    assert out.mlflow == {"destination": "own", "receives": "names", "copied": False,
                          "error": "MLflow cannot be reached"}


def test_any_error_of_the_copy_never_raises(catalog, runs, store, monkeypatch):
    monkeypatch.setattr(destinations, "_copy_in_process", lambda *a: 1 / 0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = run(catalog, runs, store=store, policy=own())  # a warning as an error must not break the end
    assert out.mlflow["error"] == "ZeroDivisionError: division by zero"


def test_the_copy_leaves_this_process_alone(catalog, runs, store, monkeypatch):
    # copy_run changes os.environ and sys.modules while it works, which
    # another run of the program would see. So it runs in a process of its own.
    import provevi.mlflow_copy

    def here(*a, **k):
        raise AssertionError("the copy ran in this process")

    monkeypatch.setattr(provevi.mlflow_copy, "copy_run", here)
    before = dict(os.environ)
    out = run(catalog, runs, store=store, policy=own())
    assert out.mlflow["copied"] is True
    assert dict(os.environ) == before


def test_a_server_that_does_not_answer_is_given_up_in_time(catalog, runs, store, monkeypatch):
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()  # takes the connection, and never answers
        uri = f"http://127.0.0.1:{silent.getsockname()[1]}"
        monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
        monkeypatch.setattr(destinations, "COPY_SECONDS", 5)
        policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
            "server": {"kind": "mlflow", "receives": "names", "endpoint": uri}}, "hosts": ["127.0.0.1"]})
        start = time.monotonic()
        with pytest.warns(UserWarning, match="took more than 5 seconds"):
            out = run(catalog, runs, store=store, policy=policy)
    assert time.monotonic() - start < 30
    assert out.status == "completed" and out.mlflow["copied"] is False


def test_a_variable_that_would_redirect_the_copy_is_refused_as_the_run_starts(catalog, runs, store, monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "https://mlflow.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:s3cret@proxy.example:3128")
    policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
        "server": {"kind": "mlflow", "receives": "names", "endpoint": "https://mlflow.example"}},
        "hosts": ["mlflow.example"]})
    out = run(catalog, runs, store=store, policy=policy)
    assert out.mlflow is None
    [route] = created(out)["destinations"]
    assert route["sending"] is False and route["destination"] == "server"
    [b] = blocked(out, "destination")
    assert "HTTPS_PROXY" in b["detail"] and "nothing is copied" in b["detail"]
    assert "s3cret" not in (out.folder / "evidence" / "events.jsonl").read_text()


def test_the_file_store_s_copy_keeps_its_lock_beside_the_runs(catalog, runs, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    out = run(catalog, runs, policy=own(endpoint=uri))
    assert out.mlflow["copied"] is True, out.mlflow
    assert not (tmp_path / "xdg").exists()
    assert (runs / ".mlflow-lock").exists()


def test_a_failed_run_is_copied_too(catalog, runs, store):
    class Boom:
        async def run(self, ctx, agent, run):
            raise RuntimeError("boom")

    out = run(catalog, runs, store=store, policy=own(), backend=Boom())
    assert out.status == "failed"
    assert out.mlflow["copied"] is True
    assert read_events(out.folder / "evidence")[-1].type == "run_failed"


def test_a_file_in_the_working_folder_cannot_stand_in_for_a_library(catalog, runs, store, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "mlflow.py").write_text(f"open({str(tmp_path / 'ran')!r}, 'w').close()\nraise ImportError('not MLflow')\n")
    monkeypatch.chdir(work)
    out = run(catalog, runs, store=store, policy=own())
    assert out.mlflow["copied"] is True, out.mlflow
    assert not (tmp_path / "ran").exists()


def test_the_copy_gets_the_policy_as_the_run_narrowed_it(monkeypatch, tmp_path):
    # narrow can cut hosts and keep a destination at one of them, which
    # from_dict would refuse; the child gets the policy object as it is.
    import base64
    import pickle
    import subprocess
    from types import SimpleNamespace

    policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
        "srv": {"kind": "mlflow", "receives": "names", "endpoint": "https://mlflow.example"}},
        "hosts": ["api.openai.com", "mlflow.example"]}).narrow({"hosts": ["api.openai.com"]})
    sent = {}

    def fake(cmd, input, **kw):
        sent.update(json.loads(input), cmd=cmd, cwd=kw["cwd"])
        return SimpleNamespace(stdout='{"error": "stop"}\n', stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake)
    destinations._copy_in_process("run-1", None, tmp_path / "r" / "evidence", policy, {})
    assert pickle.loads(base64.b64decode(sent["policy"])) == policy
    assert "-P" in sent["cmd"] and sent["cwd"] == tmp_path / "r" / "evidence"


def test_relative_paths_still_copy(catalog, tmp_path, monkeypatch):
    # The copy works in the evidence folder, so it gets every path absolute.
    monkeypatch.chdir(tmp_path)
    out = run(catalog, Path("runs"), store=SqliteStore(Path("e.db")), policy=own())
    assert out.mlflow["copied"] is True, out.mlflow
