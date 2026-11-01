"""provrun as a library: everything provr does, without the command line."""
import asyncio
import json

import pytest

import provrun
from provrun import ProvrunError, RunFile, RunFileError, RunNotFound, RunSettingError, find_run, run_file_sync
from provrun.access import AccessDenied
from provrun.backends.litellm import LiteLLMBackend
from provrun.cli import main as provr
from provrun.evidence import EvidenceError
from provrun.stagers import InputCheckError

GOOD = """
run: {org: acme, domain: benefits, env: dev, actor: tester}
agent:
  name: q
  backend: litellm
  model: openai/gpt-x
  prompt: What was revenue?
  inputs: [INPUT]
  settings: {mock_response: Revenue was 42 million.}
"""


def write(tmp_path, text, docs=None):
    f = tmp_path / "run.yaml"
    f.write_text(text.replace("INPUT", f"file://{docs}/report.txt" if docs else "file:///x"))
    return f


async def answer(**kw):
    from types import SimpleNamespace

    choice = SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def test_a_run_file_loads_and_checks(tmp_path):
    rf = RunFile.load(write(tmp_path, GOOD))
    assert rf.settings.org == "acme" and rf.settings.allow == () and rf.agent.name == "q"


@pytest.mark.parametrize("text, message", [
    ("run: {org: a, domain: b, env: c}\nagent: {name: q, backend: litellm, model: m, prompt: p}", r"needs \['actor'\]"),
    ("run: {org: a, domain: b, env: c, actor: d, alow: [x]}\nagent: {name: q, backend: litellm, model: m, prompt: p}",
     r"unknown keys \['alow'\]"),
    ("run: {org: a, domain: b, env: c, actor: d, allow: x}\nagent: {name: q, backend: litellm, model: m, prompt: p}",
     "allow must be a list"),
    ("run: {org: 1, domain: b, env: c, actor: d}\nagent: {name: q, backend: litellm, model: m, prompt: p}",
     "org must be a string"),
    ("run: [1]\nagent: {name: q}", "must be a mapping"),
    ("- 1", "a mapping with run and agent"),
    ("run: {org: a}", "no agent section"),
    ("run: {org: a\n", "cannot read"),
])
def test_a_bad_run_file_raises_a_provrun_error(tmp_path, text, message):
    with pytest.raises(RunFileError, match=message) as e:
        RunFile.load(write(tmp_path, text))
    assert isinstance(e.value, ProvrunError) and isinstance(e.value, ValueError)


def test_a_missing_run_file_raises_a_provrun_error(tmp_path):
    with pytest.raises(RunFileError, match="cannot read"):
        RunFile.load(tmp_path / "nope.yaml")


def test_a_program_runs_a_run_file_as_provr_does(tmp_path, catalog, runs, docs, capsys):
    rf = RunFile.load(write(tmp_path, GOOD, docs))
    outcome = run_file_sync(rf, catalog=catalog, runs_root=runs, backend=LiteLLMBackend(answer))
    assert outcome.status == "completed", outcome.error
    d = outcome.to_dict()
    assert d["run_id"] == outcome.run_id and d["status"] == "completed" and d["error"] is None
    assert set(d) == {"run_id", "status", "folder", "bundle", "pinned", "note", "error"}
    json.dumps(d)  # plain values
    out = capsys.readouterr()
    assert out.out == ""  # the library prints nothing
    assert find_run(outcome.run_id, runs) == outcome.folder
    assert find_run(outcome.folder) == outcome.folder


def test_provr_run_prints_the_same_summary(tmp_path, runs, docs, capsys):
    f = write(tmp_path, GOOD, docs)
    assert provr(["--runs", str(runs), "run", str(f)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "completed" and find_run(printed["run_id"], runs)


def test_a_missing_run_raises_a_provrun_error(runs, monkeypatch, tmp_path):
    with pytest.raises(RunNotFound, match="no run 'nope'"):
        find_run("nope", runs)
    monkeypatch.setenv("PROVRUN_RUNS", str(tmp_path / "elsewhere"))
    with pytest.raises(RunNotFound, match="elsewhere"):
        find_run("nope")


def test_provr_reports_library_errors_without_a_traceback(tmp_path, runs, capsys):
    assert provr(["--runs", str(runs), "show", "nope"]) == 1
    assert provr(["--runs", str(runs), "run", str(write(tmp_path, "run: {org: a}\nagent: {name: q}"))]) == 1
    err = capsys.readouterr().err
    assert "provr: no run 'nope'" in err and "provr: " in err and "Traceback" not in err


def test_every_provrun_error_shares_one_base():
    for cls in (EvidenceError, AccessDenied, InputCheckError, RunFileError, RunNotFound):
        assert issubclass(cls, ProvrunError)
    assert set(provrun.__all__) >= {"run_file", "run_file_sync", "RunFile", "find_run", "ProvrunError"}


@pytest.mark.parametrize("agent, message", [
    ("{name: q}", "missing 3 required"),
    ("{name: q, backend: litellm, model: m, prompt: p, tolls: []}", r"unknown keys \['tolls'\]"),
    ("[1]", "must be a mapping"),
])
def test_a_bad_agent_section_raises_a_run_file_error(tmp_path, agent, message):
    with pytest.raises(RunFileError, match=message):
        RunFile.load(write(tmp_path, f"run: {{org: a, domain: b, env: c, actor: d}}\nagent: {agent}"))


def test_litellm_prints_nothing_on_stdout(tmp_path, catalog, runs, docs, capfd):
    rf = RunFile.load(write(tmp_path, GOOD, docs))
    outcome = run_file_sync(rf, catalog=catalog, runs_root=runs)  # the real litellm, with mock_response
    assert outcome.status == "completed", outcome.error
    assert capfd.readouterr().out == ""


RUN = "run: {org: acme, domain: benefits, env: dev, actor: tester}\n"
AGENT = "agent: {name: q, backend: litellm, model: m, prompt: p, inputs: ['file:///x']}\n"


@pytest.mark.parametrize("run, message", [
    ("{org: acme, domain: demo/prod, env: dev, actor: t}", "domain must be one part of a URI"),
    ("{org: acme, domain: demo, env: '..', actor: t}", "env must be one part"),
    ("{org: 'a b', domain: demo, env: dev, actor: t}", "org must be one part"),
    ("{org: a, domain: b, env: dev, actor: t, allow: ['']}", "allow"),
    ("{org: a, domain: b, env: dev, actor: t, allow: ['::not a uri']}", "allow"),
    ("{org: a, domain: b, env: dev, actor: t, bundle_scheme: ''}", "bundle_scheme must be a scheme name"),
    ("{org: a, domain: b, env: dev, actor: t, bundle_scheme: 'x://../..'}", "bundle_scheme must be a scheme name"),
])
def test_a_bad_run_setting_is_refused_when_the_file_loads(tmp_path, run, message):
    with pytest.raises(RunFileError, match=message):
        RunFile.load(write(tmp_path, f"run: {run}\n{AGENT}"))


@pytest.mark.parametrize("agent, message", [
    ("{name: q, backend: litellm, model: m, prompt: p, inputs: 'file:///x'}", "inputs must be a list"),
    ("{name: q, backend: litellm, model: m, prompt: p, inputs: [1]}", "must be a URI string"),
    ("{name: q, backend: litellm, model: m, prompt: p, inputs: ['::bad']}", "input"),
    ("{name: q, backend: litellm, model: m, prompt: p, inputs: [{uri: 1}]}", "input uri must be a string"),
    ("{name: q, backend: 3, model: m, prompt: p}", "backend must be a string"),
    ("{name: q, backend: litellm, model: m, prompt: p, tools: x}", "tools must be a list"),
    ("{name: q, backend: litellm, model: m, prompt: p, settings: [1]}", "settings must be a mapping"),
    ("{name: q, backend: litellm, model: m, prompt: p, limits: 3}", "limits must be a mapping"),
])
def test_a_bad_agent_part_is_refused_when_the_file_loads(tmp_path, agent, message):
    with pytest.raises(RunFileError, match=message):
        RunFile.load(write(tmp_path, f"{RUN}agent: {agent}"))


def test_a_run_file_that_is_not_utf8_raises_a_run_file_error(tmp_path):
    f = tmp_path / "run.yaml"
    f.write_bytes(b"run: {org: \xff}\n")
    with pytest.raises(RunFileError, match="cannot read"):
        RunFile.load(f)


@pytest.mark.parametrize("kw, message", [
    ({"mode": "bogus"}, "mode must be one of"),
    ({"bundle_scheme": "file"}, "is a store of the catalog"),
    ({"bundle_scheme": ""}, "bundle_scheme must be a scheme name"),
])
def test_a_bad_argument_raises_before_the_run_folder_is_made(tmp_path, catalog, runs, kw, message):
    rf = RunFile.load(write(tmp_path, GOOD))
    with pytest.raises(RunSettingError, match=message):
        run_file_sync(rf, catalog=catalog, runs_root=runs, backend=LiteLLMBackend(answer), **kw)
    assert not runs.exists()


def test_an_unknown_backend_raises_before_the_run_folder_is_made(tmp_path, catalog, runs):
    rf = RunFile.load(write(tmp_path, GOOD.replace("backend: litellm", "backend: nosuch")))
    with pytest.raises(RunSettingError, match="no backend 'nosuch'"):
        run_file_sync(rf, catalog=catalog, runs_root=runs)
    assert not runs.exists()


def test_run_agent_refuses_an_env_that_is_not_one_segment(catalog, runs):
    rf_agent = provrun.AgentSpec.from_dict({"name": "q", "backend": "litellm", "model": "m", "prompt": "p"})
    with pytest.raises(RunSettingError, match="domain must be one part"):
        asyncio.run(provrun.run_agent(rf_agent, catalog=catalog, org="a", domain="demo/prod", env="dev",
                                      actor="t", runs_root=runs, backend=LiteLLMBackend(answer)))
    assert not runs.exists()


def test_runs_root_can_be_a_string_and_false_writes_no_bundle(tmp_path, catalog, runs, docs):
    rf = RunFile.load(write(tmp_path, GOOD.replace("actor: tester", "actor: tester, bundle_scheme: run"), docs))
    assert rf.settings.bundle_scheme == "run"
    outcome = run_file_sync(rf, catalog=catalog, runs_root=str(runs), backend=LiteLLMBackend(answer),
                            bundle_scheme=False)
    assert outcome.status == "completed", outcome.error
    assert outcome.bundle.uri is None and outcome.folder.parent == runs


def test_find_run_prefers_the_root_and_can_take_ids_only(tmp_path, catalog, runs, docs, monkeypatch):
    outcome = run_file_sync(RunFile.load(write(tmp_path, GOOD, docs)), catalog=catalog, runs_root=runs,
                            backend=LiteLLMBackend(answer))
    other = tmp_path / "cwd"
    (other / outcome.run_id / "evidence").mkdir(parents=True)
    monkeypatch.chdir(other)
    assert find_run(outcome.run_id, runs) == outcome.folder.resolve()  # the root wins over the cwd
    assert find_run(outcome.run_id, runs).is_absolute()
    monkeypatch.chdir(runs)
    for bad in ("", ".", "..", f"../runs/{outcome.run_id}", str(outcome.folder)):
        with pytest.raises(RunNotFound):
            find_run(bad, runs, ids_only=True)
    with pytest.raises(RunNotFound):
        find_run(outcome.run_id, tmp_path / "nonexistent", ids_only=True)
    assert find_run(outcome.run_id, runs, ids_only=True) == outcome.folder.resolve()


def test_read_events_is_part_of_the_library():
    assert "read_events" in provrun.__all__ and provrun.read_events


def test_litellm_prints_nothing_on_stdout_when_the_provider_is_unknown(tmp_path, catalog, runs, docs, capfd):
    rf = RunFile.load(write(tmp_path, GOOD.replace("openai/gpt-x", "nosuchprovider/xyz")
                            .replace("  settings: {mock_response: Revenue was 42 million.}\n", ""), docs))
    outcome = run_file_sync(rf, catalog=catalog, runs_root=runs)
    assert outcome.status == "failed"
    assert capfd.readouterr().out == ""


def test_run_file_writes_through_the_store_it_is_given(tmp_path, catalog, runs, docs):
    from provevi import FileStore

    opened = []

    class Store(FileStore):
        def open_run(self, run_id, folder):
            opened.append(run_id)
            return super().open_run(run_id, folder)

    outcome = run_file_sync(RunFile.load(write(tmp_path, GOOD, docs)), catalog=catalog, runs_root=runs,
                            backend=LiteLLMBackend(answer), store=Store())
    assert outcome.status == "completed", outcome.error
    assert opened == [outcome.run_id]


def test_provr_verify_and_prove_verify_agree(tmp_path, catalog, runs, docs, capsys):
    from provevi.cli import main as prove

    outcome = run_file_sync(RunFile.load(write(tmp_path, GOOD, docs)), catalog=catalog, runs_root=runs,
                            backend=LiteLLMBackend(answer))
    for tamper in (None, lambda: (outcome.folder / "output" / "response.txt").write_text("x")):
        if tamper:
            tamper()
        a = provr(["--runs", str(runs), "verify", outcome.run_id])
        out_a = capsys.readouterr()
        b = prove(["--runs", str(runs), "verify", outcome.run_id])
        out_b = capsys.readouterr()
        assert a == b and out_a.out == out_b.out
        assert out_a.err.replace("provr:", "prove:") == out_b.err


def test_provr_verify_reports_a_bad_config_without_a_traceback(tmp_path, catalog, runs, docs, capsys):
    outcome = run_file_sync(RunFile.load(write(tmp_path, GOOD, docs)), catalog=catalog, runs_root=runs,
                            backend=LiteLLMBackend(answer))
    args = ["--runs", str(runs), "verify", outcome.run_id, "--bundle", "run://a/b/dev/x?at=commit:1",
            "--config", str(tmp_path / "none.yaml")]
    assert provr(args) == 1
    assert "cannot open the catalog" in capsys.readouterr().err


def test_a_run_file_can_name_a_sqlite_store(tmp_path, catalog, runs, docs, capsys):
    from provevi import SqliteStore

    path = tmp_path / "evidence.db"
    rf = RunFile.load(write(tmp_path, GOOD.replace("actor: tester", f"actor: tester, store: 'sqlite:///{path}'"),
                            docs))
    assert rf.settings.store == f"sqlite:///{path}"
    outcome = run_file_sync(rf, catalog=catalog, runs_root=runs, backend=LiteLLMBackend(answer))
    assert outcome.status == "completed", outcome.error
    store = SqliteStore(path)
    assert store.runs_reading(f"file://{docs}/report.txt") == [outcome.run_id]
    assert store.run(outcome.run_id)["status"] == "completed"
    assert provr(["--runs", str(runs), "--store", f"sqlite:///{path}", "verify", outcome.run_id]) == 0
    capsys.readouterr()


def test_provr_run_takes_a_store(tmp_path, runs, docs, capsys):
    from provevi import SqliteStore

    path = tmp_path / "evidence.db"
    assert provr(["--runs", str(runs), "--store", f"sqlite:///{path}", "run", str(write(tmp_path, GOOD, docs))]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert SqliteStore(path).list_runs() == [run_id]


@pytest.mark.parametrize("store, message", [("postgres://x/db", "names no store"), ("sqlite://x.db", "not a sqlite")])
def test_a_bad_store_is_refused_when_the_file_loads(tmp_path, store, message):
    with pytest.raises(RunFileError, match=message):
        RunFile.load(write(tmp_path, GOOD.replace("actor: tester", f"actor: tester, store: '{store}'")))


def test_a_writer_without_record_bundle_gives_no_warning(tmp_path, catalog, runs, docs):
    import warnings

    from provevi import EvidenceStore
    from provevi.store import FileRunWriter

    class Writer:  # a writer that does not subclass RunWriter
        def __init__(self, folder):
            self.files = FileRunWriter(folder)

        def __getattr__(self, name):
            if name == "record_bundle":
                raise AttributeError(name)
            return getattr(self.files, name)

    class Store(EvidenceStore):
        def open_run(self, run_id, folder):
            return Writer(folder)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        outcome = run_file_sync(RunFile.load(write(tmp_path, GOOD, docs)), catalog=catalog, runs_root=runs,
                                backend=LiteLLMBackend(answer), store=Store())
    assert outcome.status == "completed", outcome.error


def test_the_recorder_payloads_work_with_the_sqlite_store(tmp_path):
    from provevi import SqliteStore

    from provrun.evidence import EvidenceRecorder

    r = EvidenceRecorder("run-x", "t", tmp_path / "evidence", store=SqliteStore(tmp_path / "e.db"))
    digest = r.writer.put_payload(b"x")
    assert r.payloads.get(digest) == b"x"


def test_provr_show_reads_from_the_store(tmp_path, runs, docs, capsys):
    path = tmp_path / "evidence.db"
    store = ["--runs", str(runs), "--store", f"sqlite:///{path}"]
    assert provr(store + ["run", str(write(tmp_path, GOOD, docs))]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    import sqlite3

    sqlite3.connect(path, isolation_level=None).execute("DELETE FROM provevi_events WHERE seq > 0")
    assert provr(store + ["show", run_id]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 1  # the store's one row, not the folder's lines
