"""Experiments in the catalog, agent classes from the runs, and the store's schema 2."""
import json
import sqlite3
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

import provevi.sqlite
from provcat import Catalog
from provevi import (EvidenceEvent, Experiment, ProveviError, SqliteStore, read_experiment, sha256_bytes,
                     verify_run, write_experiment)
from provevi.cli import main
from provevi.sqlite import MIGRATIONS, connect, spec_sha256

EXP = "experiment://acme/risk/dev/credit/q"
SCHEMES = {"experiment": {"parts": ["org", "team", "env", "project", "name"], "rest": "path",
                          "target": "s3://exps/{ref}/{org}/{team}/{env}/{project}/{name}/{path}", "ref": "main"}}
SPEC = {"name": "q", "backend": "litellm", "model": "m", "prompt": "What was revenue?", "instructions": "Be brief.",
        "inputs": [{"uri": "file:///tmp/a.txt", "name": None}], "tools": [], "settings": {"temperature": 0},
        "limits": {}, "stream": False}
EVAL = Experiment("agent-eval", "Does a retrieval step raise accuracy?", ("data+table://acme/risk/dev/evals/q",),
                  ("accuracy",))


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="exps")
        s3.put_bucket_versioning(Bucket="exps", VersioningConfiguration={"Status": "Enabled"})
        yield s3


@pytest.fixture
def catalog(aws, tmp_path):
    return Catalog({"schemes": SCHEMES}, cache_root=tmp_path / "cache")


def config_file(tmp_path) -> str:
    path = tmp_path / "provcat.yaml"
    path.write_text(yaml.safe_dump({"schemes": SCHEMES, "cache": {"root": str(tmp_path / "cache")}}))
    return str(path)


def write_run(store, root: Path, run_id: str, spec=SPEC, named=None, parent=None) -> Path:
    """A short run as provrun writes it, which names an experiment if named is a PinnedExperiment."""
    folder = root / run_id
    (folder / "output").mkdir(parents=True)
    writer = store.open_run(run_id, folder / "evidence")
    created = {"org": "acme", "domain": "demo", "env": "dev", "mode": "strict", "agent": spec["name"],
               "backend": "litellm", "spec": spec}
    if named is not None:
        created.update(experiment=named.uri, experiment_pin=named.pin, experiment_sha256=named.sha256)
    if parent is not None:
        created["parent_run"] = parent
    prev = None
    events = [("run_created", created), ("run_completed", {})]
    for seq, (t, data) in enumerate(events):
        line = EvidenceEvent(run_id, seq, f"2026-11-05T00:00:0{seq}.000000Z", t, "RunContext", "you@example.com",
                             (f"run://acme/demo/dev/{run_id}",), data, None, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": run_id, "events": len(events), "last": prev, "payloads": [], "complete": True,
                 "problems": []})
    (folder / "bundle.json").write_text(json.dumps({"uri": None, "pinned": None, "note": "none"}))
    writer.record_bundle(None, None)
    return folder


def db(store):
    return sqlite3.connect(store.path, isolation_level=None)


def run_id(n: int) -> str:
    return f"run-20261105T000000Z-0000000{n}"


# The file.

def test_an_experiment_reads_back_as_it_was_written():
    assert Experiment.from_bytes(EVAL.to_bytes()) == EVAL
    assert yaml.safe_load(EVAL.to_bytes()) == {"kind": "agent-eval", "description": EVAL.description,
                                               "inputs": list(EVAL.inputs), "metrics": ["accuracy"]}


@pytest.mark.parametrize("raw, problem", [
    (b"kind: test\ndescription: x\n", "kind is agent-eval or ml"),
    (b"kind: ml\n", "needs a description"),
    (b"kind: ml\ndescription: x\nagent: {name: q}\n", "keys an experiment does not have: agent"),
    (b"kind: ml\ndescription: x\ninputs: data://a\n", "inputs must be a list of texts"),
    (b"kind: ml\ndescription: x\nmetrics: [1]\n", "metrics must be a list of texts"),
    (b"- a\n", "not a mapping"),
    (b"kind: [\n", "not YAML"),
    (b"\xff", "not YAML"),
])
def test_a_file_that_is_not_an_experiment_is_refused(raw, problem):
    with pytest.raises(ProveviError, match=problem):
        Experiment.from_bytes(raw)


# The catalog.

def test_each_commit_is_a_version_and_an_old_pin_still_reads(catalog):
    first = write_experiment(catalog, EXP, EVAL, "first")
    assert first.uri == EXP and first.pin.startswith(EXP + "?at=commit:") and first.experiment == EVAL
    assert first.sha256 == sha256_bytes(EVAL.to_bytes())
    changed = Experiment("agent-eval", "Does a retrieval step raise recall?", EVAL.inputs, ("recall",))
    second = write_experiment(catalog, EXP, changed, "second")
    assert second.pin != first.pin and read_experiment(catalog, EXP).pin == second.pin
    assert read_experiment(catalog, first.pin).experiment == EVAL  # the old version is still there


def test_an_experiment_in_a_store_without_versions_is_refused(tmp_path):
    scheme = {"experiment": {**SCHEMES["experiment"], "target": f"file://{tmp_path}/e/{{org}}/{{name}}/{{path}}"}}
    del scheme["experiment"]["ref"]
    (tmp_path / "e" / "acme" / "q").mkdir(parents=True)
    (tmp_path / "e" / "acme" / "q" / "experiment.yaml").write_bytes(EVAL.to_bytes())
    with pytest.raises(ProveviError, match="no commits"):
        read_experiment(Catalog({"schemes": scheme}, cache_root=tmp_path / "cache"), EXP)


@pytest.mark.parametrize("uri, problem", [
    ("data://acme/x", "not an experiment's URI"),
    (EXP + "/experiment.yaml", "folder, not its file"),
    (EXP + "?at=commit:abc", "no \\?at="),
])
def test_write_refuses_a_uri_that_is_not_an_experiment_s_folder(catalog, uri, problem):
    with pytest.raises(ProveviError, match=problem):
        write_experiment(catalog, uri, EVAL, "m")


# The store.

def test_two_runs_of_one_version_name_one_pin_and_a_change_names_a_new_one(catalog, tmp_path):
    store = SqliteStore(tmp_path / "evidence.db")
    first = write_experiment(catalog, EXP, EVAL, "first")
    for n in (1, 2):
        write_run(store, tmp_path / "runs", run_id(n), named=read_experiment(catalog, EXP))
    second = write_experiment(catalog, EXP, Experiment("agent-eval", "Now with recall.", (), ("recall",)), "second")
    write_run(store, tmp_path / "runs", run_id(3), named=read_experiment(catalog, EXP))
    e = store.experiment(EXP)
    assert [(v["pin"], v["runs"]) for v in e["versions"]] == [(first.pin, [run_id(1), run_id(2)]),
                                                              (second.pin, [run_id(3)])]
    assert store.experiments() == [{"id": "acme/risk/dev/credit/q", "uri": EXP, "first_run": run_id(1),
                                    "created_at": "2026-11-05T00:00:00.000000Z", "versions": 2, "runs": 3}]
    assert store.runs_of_experiment(EXP) == [run_id(1), run_id(2), run_id(3)]
    assert store.runs_of_experiment(first.pin) == [run_id(1), run_id(2)]
    for n in (1, 2, 3):
        check = verify_run(tmp_path / "runs" / run_id(n), catalog=catalog, store=store)
        assert check.ok, check.problems
    assert check.experiment == second.pin


def test_runs_with_other_prompts_are_one_version_and_new_instructions_another(tmp_path):
    store = SqliteStore(tmp_path / "evidence.db")
    write_run(store, tmp_path / "runs", run_id(1))
    write_run(store, tmp_path / "runs", run_id(2), spec={**SPEC, "prompt": "Another question?", "inputs": []})
    write_run(store, tmp_path / "runs", run_id(3), spec={**SPEC, "instructions": "Be thorough."})
    a = store.agent("acme/demo/dev/q")
    assert [v["runs"] for v in a["versions"]] == [[run_id(1), run_id(2)], [run_id(3)]]
    assert a["versions"][0]["spec_sha256"] == spec_sha256(SPEC) and a["first_run"] == run_id(1)
    assert "settings" not in a["versions"][0]["settings"]  # the settings may hold secrets
    assert store.agents() == [{"id": "acme/demo/dev/q", "first_run": run_id(1),
                               "created_at": "2026-11-05T00:00:00.000000Z", "versions": 2, "runs": 3}]
    assert store.runs_of_agent("acme/demo/dev/q", a["versions"][1]["spec_sha256"][:8]) == [run_id(3)]


def test_a_parent_run_is_recorded_and_checked(tmp_path):
    store = SqliteStore(tmp_path / "evidence.db")
    write_run(store, tmp_path / "runs", run_id(1))
    folder = write_run(store, tmp_path / "runs", run_id(2), parent=run_id(1))
    [parent] = db(store).execute("SELECT parent_run FROM provevi_runs WHERE run_id = ?",
                                                   (run_id(2),)).fetchone()
    assert parent == run_id(1)
    db(store).execute("UPDATE provevi_runs SET parent_run = 'other'")
    assert any("parent_run" in p for p in verify_run(folder, store=store).problems)


@pytest.mark.parametrize("sql, problem", [
    ("UPDATE provevi_runs SET experiment_pin = 'experiment://acme/risk/dev/credit/q?at=commit:x'",
     "the run's experiment_pin"),
    ("UPDATE provevi_runs SET experiment_sha256 = 'f'", "the run's experiment_sha256"),
    ("UPDATE provevi_experiment_versions SET sha256 = 'f'", "version"),
    ("DELETE FROM provevi_experiment_versions", "version"),
    ("UPDATE provevi_experiments SET uri = 'experiment://evil/x/y/z/w'", "the experiment acme/risk/dev/credit/q"),
    ("""UPDATE provevi_agent_versions SET spec = '{"model":"gpt-evil","api_key":"forged"}'""",
     "the settings of the version"),
])
def test_a_changed_experiment_row_fails_the_check(catalog, tmp_path, sql, problem):
    store = SqliteStore(tmp_path / "evidence.db")
    folder = write_run(store, tmp_path / "runs", run_id(1), named=write_experiment(catalog, EXP, EVAL, "m"))
    db(store).execute(sql)
    assert any(problem in p for p in verify_run(folder, catalog=catalog, store=store).problems)


def test_a_run_whose_experiment_file_changed_at_its_pin_fails(catalog, tmp_path):
    store = SqliteStore(tmp_path / "evidence.db")
    named = write_experiment(catalog, EXP, EVAL, "m")
    forged = type(named)(named.uri, named.pin, "0" * 64, named.experiment)
    folder = write_run(store, tmp_path / "runs", run_id(1), named=forged)
    problems = verify_run(folder, catalog=catalog, store=store).problems
    assert any("has changed" in p for p in problems), problems


def test_a_run_that_names_an_experiment_needs_a_catalog_to_verify(catalog, tmp_path):
    store = SqliteStore(tmp_path / "evidence.db")
    folder = write_run(store, tmp_path / "runs", run_id(1), named=write_experiment(catalog, EXP, EVAL, "m"))
    assert any("no catalog was given" in p for p in verify_run(folder, store=store).problems)


@pytest.mark.parametrize("created, problem", [
    ({"experiment": EXP}, "without its URI, its pin and its hash"),
    ({"experiment": EXP, "experiment_pin": "experiment://acme/risk/dev/other/q?at=commit:a",
      "experiment_sha256": "a" * 64}, "is not of"),
    ({"experiment": EXP, "experiment_pin": EXP, "experiment_sha256": "a" * 64}, "names no commit"),
    ({"experiment": EXP, "experiment_pin": EXP + "?at=branch:main", "experiment_sha256": "a" * 64},
     "names no commit"),
    ({"experiment": EXP, "experiment_pin": EXP + "?at=commit:a&at=branch:main", "experiment_sha256": "a" * 64},
     "names no commit"),
    ({"experiment": EXP + "?at=commit:a", "experiment_pin": EXP + "?at=commit:b", "experiment_sha256": "a" * 64},
     "but its pin is"),
])
def test_a_run_s_experiment_fields_must_agree(catalog, created, problem):
    from provevi.experiments import experiment_problems

    assert any(problem in p for p in experiment_problems(catalog, created))


# The migration from schema 1, where each agent class was called an experiment.

def v1_file(tmp_path, source: SqliteStore) -> Path:
    """A schema 1 file holding the runs of source, as provevi 1 wrote them."""
    path = tmp_path / "v1.db"
    conn = connect(path)
    for st in MIGRATIONS[1]:
        conn.execute(st)
    conn.execute("INSERT INTO provevi_meta VALUES ('schema_version', '1')")
    conn.execute("ATTACH DATABASE ? AS new", (str(source.path),))
    for table in ("provevi_events", "provevi_event_refs", "provevi_payloads", "provevi_run_params"):
        conn.execute(f"INSERT INTO {table} SELECT * FROM new.{table}")
    conn.execute("INSERT INTO provevi_runs SELECT run_id, agent_id, org, domain, env, actor, status, started_at, "
                 "ended_at, mode, parent_run, folder, last_hash, manifest, manifest_sha256, sealed_at, bundle_uri, "
                 "pinned_bundle FROM new.provevi_runs")
    conn.execute("INSERT INTO provevi_experiments (id, uri, org, domain, env, name, spec, spec_sha256, created_by, "
                 "created_at) SELECT id, 'experiment://' || id, org, domain, env, name, '{}', 'x', created_by, "
                 "created_at FROM new.provevi_agents")
    conn.commit()
    conn.execute("DETACH DATABASE new")
    conn.close()
    return path


def test_an_older_store_s_experiments_become_agent_classes_and_its_runs_still_verify(tmp_path):
    source = SqliteStore(tmp_path / "evidence.db")
    for n in (1, 2):
        write_run(source, tmp_path / "runs", run_id(n))
    write_run(source, tmp_path / "runs", run_id(3), spec={**SPEC, "instructions": "Be thorough."})
    path = v1_file(tmp_path, source)
    with pytest.raises(ProveviError, match="schema version 1"):
        SqliteStore(path, create=False)  # reading never migrates
    store = SqliteStore(path)
    assert provevi.sqlite.schema_version(sqlite3.connect(path)) == 2
    assert [(a["id"], a["versions"], a["runs"]) for a in store.agents()] == [("acme/demo/dev/q", 2, 3)]
    assert store.agent("acme/demo/dev/q")["first_run"] == run_id(1)
    assert store.experiments() == [] and store.runs_of_agent("acme/demo/dev/q") == [run_id(n) for n in (1, 2, 3)]
    for n in (1, 2, 3):
        check = verify_run(tmp_path / "runs" / run_id(n), store=SqliteStore(path, create=False))
        assert check.ok, check.problems


# The commands.

def test_prove_lists_and_shows_experiments_and_agents(catalog, tmp_path, capsys):
    store = SqliteStore(tmp_path / "evidence.db")
    named = write_experiment(catalog, EXP, EVAL, "first")
    write_run(store, tmp_path / "runs", run_id(1), named=named)
    s = ["--runs", str(tmp_path / "runs"), "--store", f"sqlite:///{store.path}"]
    assert main(s + ["experiments"]) == 0
    assert capsys.readouterr().out == f"{EXP}  1 version, 1 run\n"
    assert main(s + ["experiment", "show", EXP, "--config", config_file(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert named.pin in out and run_id(1) in out and "kind: agent-eval" in out
    assert main(s + ["agents"]) == 0 and capsys.readouterr().out == "acme/demo/dev/q  1 version, 1 run\n"
    assert main(s + ["agent", "show", "acme/demo/dev/q"]) == 0
    assert spec_sha256(SPEC)[:12] in capsys.readouterr().out
    assert main(s + ["runs", "--experiment", EXP]) == 0 and capsys.readouterr().out.split() == [run_id(1)]
    assert main(s + ["runs", "--agent", "acme/demo/dev/q"]) == 0 and capsys.readouterr().out.split() == [run_id(1)]
    assert main(s + ["runs", "--agent", "a", "--experiment", EXP]) == 1
    assert "give one of" in capsys.readouterr().err
    assert main(s + ["verify", run_id(1), "--config", config_file(tmp_path)]) == 0
    assert f"its experiment matches {named.pin}" in capsys.readouterr().out
    assert main(s + ["experiment", "show", "experiment://acme/risk/dev/credit/none"]) == 1
    assert "no run in the store names" in capsys.readouterr().err
    assert main(s + ["agent", "show", "acme/demo/dev/none"]) == 1
