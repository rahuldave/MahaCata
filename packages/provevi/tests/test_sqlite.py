"""The sqlite store: its tables, its record of runs, its checks and its queries."""
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import provevi
from provevi import (SCHEMA_VERSION, EvidenceError, EvidenceEvent, FileStore, ProveviError, RunNotFound, SqliteStore,
                     check_store_uri, open_store, sha256_bytes, verify_run)
from provevi.cli import main

DOC = "lakefs://acme/main/docs/report.txt"


def events_of(run_id, inputs=(DOC,), denied=(), outputs=("answer.txt",)):
    """The events of a small run: (type, refs, data, payload)."""
    run_uri = f"run://acme/demo/dev/{run_id}"
    spec = {"name": "q", "backend": "litellm", "model": "m", "tools": [], "limits": {}, "settings": {"k": "v"}}
    out = [("run_created", [run_uri], {"org": "acme", "domain": "demo", "env": "dev", "mode": "strict",
                                       "agent": "q", "spec": spec}, None)]
    for uri in inputs:
        out.append(("access_granted", [uri], {"grant_id": "g1", "mode": "mounted_snapshot", "reason": "ok"}, None))
        out.append(("input_staged", [uri, f"{uri}?at=commit:abc"], {"sha256": "0" * 64, "agent_path": "/input/r"},
                    None))
    for uri in denied:
        out.append(("access_denied", [uri], {"mode": "mounted_snapshot", "reason": "not allowed"}, None))
    out.append(("model_called", [], {"trace_id": "a" * 32, "span_id": "b" * 16}, b"the prompt"))
    for rel in outputs:
        out.append(("output_artifact_created", [f"{run_uri}/output/{rel}"], {"path": f"/output/{rel}",
                                                                            "sha256": sha256_bytes(b"42")}, None))
    out.append(("run_completed", [run_uri], {}, None))
    return out


def write_run(store, root: Path, run_id: str, **kw) -> Path:
    """Write a run through store, as provrun's recorder does, with fixed times."""
    folder = root / run_id
    (folder / "output").mkdir(parents=True)
    writer = store.open_run(run_id, folder / "evidence")
    prev = None
    events = events_of(run_id, **kw)
    for seq, (t, refs, data, payload) in enumerate(events):
        ref = writer.put_payload(payload) if payload is not None else None
        if t == "output_artifact_created":
            (folder / "output" / refs[0].rsplit("/output/", 1)[1]).write_bytes(b"42")
        line = EvidenceEvent(run_id, seq, f"2026-11-01T00:00:{seq:02}Z", t, "test", "you", tuple(refs), data, ref,
                             "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": run_id, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    bundle = {"uri": None, "pinned": None, "note": "no store for runs is set"}
    (folder / "bundle.json").write_text(json.dumps(bundle))  # as provrun writes it
    writer.record_bundle(bundle["uri"], bundle["pinned"])
    return folder


RUN = "run-20261101T000000Z-00000001"


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "evidence.db")


def db(store):
    conn = sqlite3.connect(store.path, isolation_level=None)
    return conn


def test_a_new_file_gets_the_tables_and_the_schema_version(store):
    names = {r[0] for r in db(store).execute("SELECT name FROM sqlite_master")}
    assert {"provevi_experiments", "provevi_runs", "provevi_run_params", "provevi_events", "provevi_event_refs",
            "provevi_payloads", "provevi_assessments", "provevi_grants", "provevi_refusals", "provevi_inputs",
            "provevi_outputs"} <= names
    assert provevi.sqlite.schema_version(db(store)) == SCHEMA_VERSION
    SqliteStore(store.path)  # opening it again changes nothing


def test_a_file_of_a_newer_provevi_is_refused(store):
    db(store).execute("UPDATE provevi_meta SET value = '99' WHERE key = 'schema_version'")
    for create in (True, False):
        with pytest.raises(ProveviError, match="schema version 99; this provevi reads up to"):
            SqliteStore(store.path, create=create)


def test_the_tables_go_into_a_file_that_holds_other_tables(tmp_path):
    path = tmp_path / "shared.db"
    sqlite3.connect(path).execute("CREATE TABLE experiments (experiment_id INTEGER PRIMARY KEY)")
    write_run(SqliteStore(path), tmp_path / "runs", RUN)
    assert SqliteStore(path).list_runs() == [RUN]


def test_a_migration_runs_each_step_once(tmp_path, monkeypatch):
    """A step that alters a table, which makes SQLite rewrite the table's
    stored statement, still opens: the expected schema is built by the steps."""
    path = tmp_path / "e.db"
    SqliteStore(path)
    monkeypatch.setattr(provevi.sqlite, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(provevi.sqlite.MIGRATIONS, 2, ["ALTER TABLE provevi_runs ADD COLUMN note TEXT"])
    SqliteStore(path)
    SqliteStore(path)  # a second open must not add the column again
    SqliteStore(path, create=False)
    SqliteStore(tmp_path / "new.db")  # a new file gets both steps
    assert provevi.sqlite.schema_version(sqlite3.connect(path)) == 2


def test_a_table_changed_outside_a_step_is_refused(store):
    db(store).execute("ALTER TABLE provevi_runs ADD COLUMN note TEXT")
    with pytest.raises(ProveviError, match="table provevi_runs is missing or was changed"):
        SqliteStore(store.path)


def test_an_extra_index_is_allowed(store, tmp_path):
    folder = write_run(store, tmp_path / "runs", RUN)
    db(store).execute("CREATE INDEX provevi_events_time ON provevi_events (time)")
    assert verify_run(folder, store=SqliteStore(store.path, create=False)).ok


def test_a_run_in_the_sqlite_store_verifies_and_is_recorded(store, tmp_path):
    folder = write_run(store, tmp_path / "runs", RUN)
    check = verify_run(folder, store=store)
    assert check.ok, check.problems
    run = store.run(RUN)
    assert run["status"] == "completed" and run["experiment_id"] == "acme/demo/dev/q" and run["actor"] == "you"
    assert run["pinned_bundle"] is None and run["bundle_uri"] is None
    assert run["params"] == {"agent": "q", "backend": "litellm", "model": "m", "mode": "strict", "tools": [],
                             "limits": {}}  # never the settings, which may hold secrets
    assert store.run_folder(RUN) == folder.resolve()
    [e] = db(store).execute("SELECT trace_id, span_id FROM provevi_events WHERE type = 'model_called'")
    assert e == ("a" * 32, "b" * 16)


def test_the_bundle_equals_the_file_stores(store, tmp_path):
    a = write_run(FileStore(), tmp_path / "file", RUN)
    b = write_run(store, tmp_path / "sqlite", RUN)
    files = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    assert all((a / f).read_bytes() == (b / f).read_bytes() for f in files)


@pytest.mark.parametrize("sql, problem", [
    ("UPDATE provevi_events SET line = replace(line, '\"test\"', '\"other\"') WHERE seq = 2", "chain is broken"),
    ("DELETE FROM provevi_events WHERE seq = 2", "seq is 3"),
    ("DELETE FROM provevi_events WHERE seq = (SELECT max(seq) FROM provevi_events)", "last event"),
    ("UPDATE provevi_events SET type = 'access_denied' WHERE seq = 1", "columns differ from its line"),
    ("UPDATE provevi_event_refs SET uri = 'lakefs://acme/main/other' WHERE seq = 2 AND n = 0", "refs differ"),
    ("DELETE FROM provevi_event_refs WHERE seq = 2", "refs differ"),
    ("UPDATE provevi_runs SET manifest = replace(manifest, '\"complete\": true', '\"complete\": false')",
     "manifest hash differs"),
    ("DELETE FROM provevi_payloads", "is missing"),
    ("DELETE FROM provevi_runs", "no run"),
])
def test_a_changed_or_removed_row_fails(store, tmp_path, sql, problem):
    folder = write_run(store, tmp_path / "runs", RUN)
    db(store).execute(sql)
    check = verify_run(folder, store=store)
    assert not check.ok and any(problem in p for p in check.problems), check.problems


def test_a_folder_that_differs_from_the_store_fails(store, tmp_path):
    folder = write_run(store, tmp_path / "runs", RUN)
    events = folder / "evidence" / "events.jsonl"
    other = write_run(FileStore(), tmp_path / "other", RUN, inputs=())
    events.write_bytes((other / "evidence" / "events.jsonl").read_bytes())
    (folder / "evidence" / "manifest.json").write_bytes((other / "evidence" / "manifest.json").read_bytes())
    check = verify_run(folder, store=store)
    assert "events.jsonl in the run folder differs from the store" in check.problems


def test_a_failed_line_leaves_no_row(store, tmp_path, monkeypatch):
    writer = store.open_run(RUN, tmp_path / "evidence")
    line = EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line()

    def fail(line):
        raise OSError("disk full")

    monkeypatch.setattr(writer.files, "append", fail)
    with pytest.raises(OSError):
        writer.append(line)
    assert db(store).execute("SELECT count(*) FROM provevi_events").fetchone() == (0,)
    monkeypatch.undo()
    writer.append(line)  # the same seq is free again
    assert db(store).execute("SELECT count(*) FROM provevi_events").fetchone() == (1,)


def test_a_run_id_can_be_opened_once(store, tmp_path):
    store.open_run(RUN, tmp_path / "a")
    with pytest.raises(EvidenceError, match="already in"):
        store.open_run(RUN, tmp_path / "b")
    with pytest.raises(RunNotFound):
        store.read_run("run-nothing")


def _write_many(store, root, run_id, n):
    writer = store.open_run(run_id, root / run_id / "evidence")
    prev = None
    for seq in range(n):
        t = "run_created" if seq == 0 else ("run_completed" if seq == n - 1 else "note_added")
        line = EvidenceEvent(run_id, seq, "t", t, "c", "a", (), {"n": seq}, None, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    writer.seal({"run_id": run_id, "events": n, "last": prev, "payloads": [], "complete": True, "problems": []})


def test_two_runs_at_once_lose_no_event(store, tmp_path):
    ids = [f"run-20261101T000000Z-0000000{i}" for i in range(4)]
    threads = [threading.Thread(target=_write_many, args=(store, tmp_path, i, 200)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in ids:
        assert len(store.read_run(i).lines()) == 200
        (tmp_path / i / "output").mkdir()
        assert verify_run(tmp_path / i, store=store).ok


def test_two_processes_at_once_lose_no_event(store, tmp_path):
    script = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[3]);"
        "from test_sqlite import _write_many; from provevi import SqliteStore;"
        "_write_many(SqliteStore(sys.argv[1]), Path(sys.argv[2]), sys.argv[4], 200)")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(store.path), str(tmp_path),
                               str(Path(__file__).parent), f"run-20261101T000000Z-p000000{i}"]) for i in range(3)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    for i in range(3):
        assert len(store.read_run(f"run-20261101T000000Z-p000000{i}").lines()) == 200


def test_queries_across_runs(store, tmp_path):
    a = "run-20261101T000000Z-0000000a"
    b = "run-20261101T000000Z-0000000b"
    write_run(store, tmp_path / "runs", a)
    write_run(store, tmp_path / "runs", b, inputs=("lakefs://acme/main/docs/other.txt",), denied=(DOC,))
    assert store.runs_reading(DOC) == [a]
    assert store.runs_reading(f"{DOC}?at=commit:abc") == [a]
    assert store.runs_reading("lakefs://acme/main/docs/report") == []  # a prefix is not the URI
    assert store.runs_of_experiment("acme/demo/dev/q") == [a, b]
    assert store.runs_of_experiment("experiment://acme/demo/dev/q") == [a, b]
    assert [e["id"] for e in store.experiments()] == ["acme/demo/dev/q"]
    assert [(x["decision"], x["uri"]) for x in store.access(b)] == [
        ("granted", "lakefs://acme/main/docs/other.txt"), ("denied", DOC)]
    assert store.inputs(a)[0]["pinned"] == f"{DOC}?at=commit:abc"
    assert store.list_runs() == [a, b]


def test_prove_with_a_sqlite_store(store, tmp_path, capsys):
    folder = write_run(store, tmp_path / "runs", RUN)
    s = ["--store", f"sqlite:///{store.path}"]  # an absolute path makes sqlite:////...
    assert main(s + ["runs"]) == 0 and capsys.readouterr().out.split() == [RUN]
    assert main(s + ["runs", "--reading", DOC]) == 0 and capsys.readouterr().out.split() == [RUN]
    assert main(s + ["runs", "--experiment", "acme/demo/dev/q"]) == 0 and RUN in capsys.readouterr().out
    assert main(s + ["experiments"]) == 0 and "acme/demo/dev/q" in capsys.readouterr().out
    assert main(s + ["access", RUN]) == 0 and "granted" in capsys.readouterr().out
    assert main(s + ["show", RUN]) == 0 and "input_staged" in capsys.readouterr().out
    assert main(s + ["verify", RUN]) == 0 and "verify" in capsys.readouterr().out
    db(store).execute("DELETE FROM provevi_events WHERE seq = 2")
    assert main(s + ["verify", RUN]) == 1
    assert "in the store:" in capsys.readouterr().err
    assert main(s + ["access", "run-nothing"]) == 1
    assert main(["--runs", str(folder.parent), "runs", "--reading", DOC]) == 1
    assert "file store cannot answer" in capsys.readouterr().err


@pytest.mark.parametrize("uri", ["sqlite:///x.db", "sqlite:////tmp/x.db", "file"])
def test_good_store_uris(uri):
    check_store_uri(uri)


@pytest.mark.parametrize("uri", ["sqlite://x.db", "sqlite:///", "sqlite:////", "postgres://h/db", "x"])
def test_bad_store_uris(uri):
    with pytest.raises(ProveviError):
        check_store_uri(uri)


def test_open_store_resolves_a_relative_sqlite_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = open_store("sqlite:///sub/e.db")
    assert isinstance(s, SqliteStore) and (tmp_path / "sub" / "e.db").exists()
    assert isinstance(open_store(None, tmp_path), FileStore)
    assert json.loads(json.dumps(s.list_runs())) == []


# Each test below is one finding of the review of Phase 2.

@pytest.mark.parametrize("sql, problem", [
    ("UPDATE provevi_runs SET status = 'failed'", "the run's status is 'failed'"),
    ("UPDATE provevi_runs SET experiment_id = 'evil/x/y/z'", "the run's experiment_id"),
    ("UPDATE provevi_runs SET actor = 'someone'", "the run's actor"),
    ("UPDATE provevi_runs SET org = 'other'", "the run's org"),
    ("UPDATE provevi_runs SET started_at = 'x'", "the run's started_at"),
    ("UPDATE provevi_runs SET ended_at = NULL", "the run's ended_at"),
    ("UPDATE provevi_runs SET bundle_uri = 'run://x/y/z/w'", "bundle_uri differs from bundle.json"),
    ("UPDATE provevi_run_params SET value = '\"gpt-evil\"' WHERE key = 'model'", "settings differ"),
    ("DELETE FROM provevi_run_params WHERE key = 'tools'", "settings differ"),
    ("UPDATE provevi_experiments SET name = 'other'", "experiment acme/demo/dev/q is missing or differs"),
    ("DELETE FROM provevi_experiments", "experiment acme/demo/dev/q is missing or differs"),
    ("UPDATE provevi_payloads SET size = 1", "its size is not 1"),
    ("UPDATE provevi_events SET trace_id = 'f' WHERE type = 'model_called'", "columns differ from its line"),
])
def test_a_changed_derived_column_fails(store, tmp_path, sql, problem):
    folder = write_run(store, tmp_path / "runs", RUN)
    db(store).execute(sql)
    check = verify_run(folder, store=store)
    assert any(problem in p for p in check.problems), check.problems


@pytest.mark.parametrize("sql, problem", [
    ("UPDATE provevi_events SET line = CAST(line AS BLOB) WHERE seq = 1", "the line is not text"),
    ("UPDATE provevi_runs SET manifest = CAST(manifest AS BLOB)", "the manifest"),
    ("DROP VIEW provevi_inputs; DROP TABLE provevi_payloads", "cannot read"),
])
def test_a_broken_file_gives_problems_not_an_exception(store, tmp_path, capsys, sql, problem):
    folder = write_run(store, tmp_path / "runs", RUN)
    db(store).executescript(sql)
    check = verify_run(folder, store=store)
    assert any(problem in p for p in check.problems), check.problems
    assert main(["--runs", str(folder.parent), "--store", f"sqlite:///{store.path}", "verify", RUN]) == 1


def test_a_relative_path_is_fixed_when_the_store_opens(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.chdir(tmp_path / "a")
    s = open_store("sqlite:///e.db")
    monkeypatch.chdir(tmp_path / "b")
    write_run(s, tmp_path / "runs", RUN)
    assert not (tmp_path / "b" / "e.db").exists() and SqliteStore(tmp_path / "a" / "e.db").list_runs() == [RUN]


def test_a_home_path_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert open_store("sqlite:///~/e.db").path == (tmp_path / "e.db").resolve()
    assert not (tmp_path / "~").exists()


@pytest.mark.parametrize("uri", ["sqlite:///e.db?mode=ro", "sqlite:///e.db#x"])
def test_a_store_uri_with_a_query_is_refused(uri):
    with pytest.raises(ProveviError, match="query or a fragment"):
        check_store_uri(uri)


def test_prove_reads_and_creates_nothing(tmp_path, capsys):
    assert main(["--store", f"sqlite:///{tmp_path}/typo/e.db", "runs"]) == 1
    assert "no evidence file" in capsys.readouterr().err and not (tmp_path / "typo").exists()
    (tmp_path / "x.py").write_text("print(1)\n" * 100)
    assert main(["--store", f"sqlite:///{tmp_path}/x.py", "runs"]) == 1
    assert "cannot open the evidence file" in capsys.readouterr().err
    other = sqlite3.connect(tmp_path / "other.db")
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("CREATE TABLE t (x)")
    other.close()
    assert main(["--store", f"sqlite:///{tmp_path}/other.db", "runs"]) == 1
    assert "holds no provevi evidence" in capsys.readouterr().err


def test_prove_refuses_a_file_in_a_folder_it_cannot_write(tmp_path, capsys):
    """WAL mode only: SQLite writes a side file next to a WAL file even to
    read it, so a folder that cannot be written is refused, plainly."""
    folder = tmp_path / "ro"
    write_run(SqliteStore(folder / "e.db"), tmp_path / "runs", RUN)
    folder.chmod(0o555)
    try:
        assert main(["--store", f"sqlite:///{folder}/e.db", "runs"]) == 1
        assert "its folder cannot be written" in capsys.readouterr().err
    finally:
        folder.chmod(0o755)


def test_the_file_is_in_wal_mode(store, tmp_path):
    write_run(store, tmp_path / "runs", RUN)
    assert db(store).execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_a_file_in_another_mode_is_refused_to_read(store, tmp_path, capsys):
    write_run(store, tmp_path / "runs", RUN)
    db(store).execute("PRAGMA journal_mode=DELETE")
    with pytest.raises(ProveviError, match="delete mode, not WAL"):
        SqliteStore(store.path, create=False)
    assert main(["--store", f"sqlite:///{store.path}", "runs"]) == 1
    assert "provevi uses WAL only" in capsys.readouterr().err


def test_a_file_that_cannot_be_put_in_wal_mode_is_refused(tmp_path, monkeypatch):
    real = sqlite3.connect

    class NoWal:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            if sql == "PRAGMA journal_mode=WAL":
                return self.conn.execute("PRAGMA journal_mode")  # as a file system without WAL answers
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(provevi.sqlite.sqlite3, "connect", lambda *a, **k: NoWal(real(*a, **k)))
    with pytest.raises(ProveviError, match="not WAL"):
        SqliteStore(tmp_path / "e.db")


def test_prove_refuses_an_older_schema_without_changing_it(store, monkeypatch, capsys):
    monkeypatch.setattr(provevi.sqlite, "SCHEMA_VERSION", 2)
    assert main(["--store", f"sqlite:///{store.path}", "runs"]) == 1
    assert "open it to write" in capsys.readouterr().err
    assert provevi.sqlite.schema_version(db(store)) == 1


class FailingCommit:
    """A connection whose next COMMIT fails, as a full disk would make it."""

    def __init__(self, conn):
        self.conn, self.armed = conn, True

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.armed:
            self.armed = False
            self.conn.execute("ROLLBACK")  # SQLite rolls back on some failed commits
            raise sqlite3.OperationalError("disk I/O error")
        return self.conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_a_failed_commit_takes_the_line_and_the_files_back(store, tmp_path):
    ev = tmp_path / "evidence"
    writer = store.open_run(RUN, ev)
    writer.append(EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line())
    before = (ev / "events.jsonl").read_bytes()
    writer._conn = FailingCommit(writer._conn)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):  # not "no transaction is active"
        writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())
    assert (ev / "events.jsonl").read_bytes() == before
    writer._conn.armed = True
    with pytest.raises(sqlite3.OperationalError):
        writer.put_payload(b"x")
    assert not (ev / "payloads" / sha256_bytes(b"x")).exists()
    writer._conn.armed = True
    with pytest.raises(sqlite3.OperationalError):
        writer.seal({"run_id": RUN})
    assert not (ev / "manifest.json").exists()


def test_a_moved_runs_root_still_verifies(store, tmp_path, capsys):
    write_run(store, tmp_path / "runs", RUN)
    (tmp_path / "runs").rename(tmp_path / "runs2")
    assert verify_run(tmp_path / "runs2" / RUN, store=store).ok
    assert main(["--runs", str(tmp_path / "runs2"), "--store", f"sqlite:///{store.path}", "verify", RUN]) == 0


def test_show_finds_a_run_as_verify_does(store, tmp_path, capsys):
    folder = write_run(store, tmp_path / "runs", RUN)
    other = write_run(FileStore(), tmp_path / "runs", "run-20261101T000000Z-00000002")
    s = ["--runs", str(tmp_path / "runs"), "--store", f"sqlite:///{store.path}"]
    for run in (str(folder), other.name, str(other)):
        assert main(s + ["show", run]) == 0, run
        assert "run_created" in capsys.readouterr().out


def test_an_experiment_keeps_no_agent_settings(store, tmp_path):
    write_run(store, tmp_path / "runs", RUN)
    [spec] = db(store).execute("SELECT spec FROM provevi_experiments").fetchone()
    assert "settings" not in json.loads(spec) and json.loads(spec)["model"] == "m"


def test_a_refused_run_leaves_no_files(store, tmp_path):
    store.open_run(RUN, tmp_path / "a")
    with pytest.raises(EvidenceError):
        store.open_run(RUN, tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_a_run_sealed_without_its_ending_is_incomplete(store, tmp_path):
    writer = store.open_run(RUN, tmp_path / "evidence")
    line = EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line()
    writer.append(line)
    writer.close()
    writer.seal({"run_id": RUN, "events": 1, "last": sha256_bytes(line.encode()), "payloads": [],
                 "complete": False, "problems": ["lost run_completed"]})
    assert store.run(RUN)["status"] == "incomplete"
    assert not any("status" in p for p in store.read_run(RUN).problems())


def test_a_store_opened_to_read_cannot_write(store, tmp_path):
    with pytest.raises(ProveviError, match="opened to read"):
        SqliteStore(store.path, create=False).open_run(RUN, tmp_path / "e")


# Each test below is one finding of the second review of Phase 2.

def test_a_store_that_cannot_be_read_fails_the_check(store, tmp_path):
    folder = write_run(store, tmp_path / "runs", RUN)
    reader = SqliteStore(store.path, create=False)
    db(store).execute("DROP TABLE provevi_runs")
    check = verify_run(folder, store=reader)
    assert not check.ok and check.problems[0].startswith("in the store:"), check.problems


def test_a_store_that_only_writes_leaves_the_check_to_the_folder(tmp_path):
    from provevi import EvidenceStore

    class OnlyWrites(EvidenceStore):
        def open_run(self, run_id, folder):
            return FileStore().open_run(run_id, folder)

    folder = write_run(OnlyWrites(), tmp_path / "runs", RUN)
    assert verify_run(folder, store=OnlyWrites()).ok


@pytest.mark.parametrize("sql", [
    "DROP VIEW provevi_inputs; CREATE VIEW provevi_inputs AS SELECT run_id, 0 AS seq, 'lakefs://evil' AS uri, "
    "NULL AS pinned, NULL AS sha256, NULL AS agent_path FROM provevi_runs",
    "DROP VIEW provevi_grants; CREATE VIEW provevi_grants AS SELECT run_id, 0 AS seq, '' AS time, "
    "'secret://all' AS uri, '' AS grant_id, '' AS mode, '' AS reason FROM provevi_runs",
    "CREATE TRIGGER provevi_evil AFTER INSERT ON provevi_events BEGIN UPDATE provevi_runs SET actor = 'x'; END",
    "CREATE TRIGGER other AFTER INSERT ON provevi_runs BEGIN SELECT 1; END",
    "DROP INDEX provevi_event_refs_uri",
])
def test_a_changed_schema_is_refused_and_fails_the_check(store, tmp_path, sql):
    folder = write_run(store, tmp_path / "runs", RUN)
    reader = SqliteStore(store.path, create=False)
    db(store).executescript(sql)
    for create in (True, False):
        with pytest.raises(ProveviError, match="schema .* was changed"):
            SqliteStore(store.path, create=create)
    check = verify_run(folder, store=reader)
    assert any(p.startswith("in the store: the schema:") for p in check.problems), check.problems


def test_a_line_that_fails_to_write_is_never_written_later(store, tmp_path, monkeypatch):
    import errno
    import os

    ev = tmp_path / "evidence"
    writer = store.open_run(RUN, ev)
    writer.append(EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line())
    real = os.write
    calls = []

    def full(fd, data):
        if not calls:
            calls.append(1)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(os, "write", full)
    with pytest.raises(OSError):
        writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a", data={"lost": True}).line())
    writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())
    lines = (ev / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2 and "lost" not in lines[1]
    assert len(store.read_run(RUN).lines()) == 2


class FailingRollback(FailingCommit):
    """A connection whose COMMIT and then ROLLBACK both fail."""

    def execute(self, sql, *args):
        if sql == "COMMIT" and self.armed:
            raise sqlite3.OperationalError("disk I/O error")
        if sql == "ROLLBACK" and self.armed:
            self.armed = False
            raise sqlite3.OperationalError("cannot roll back")
        return self.conn.execute(sql, *args)


def test_a_failed_rollback_still_takes_the_line_back_and_the_run_goes_on(store, tmp_path):
    ev = tmp_path / "evidence"
    writer = store.open_run(RUN, ev)
    writer.append(EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line())
    before = (ev / "events.jsonl").read_bytes()
    writer._conn = FailingRollback(writer._conn)
    with pytest.raises(sqlite3.OperationalError):
        writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())
    assert (ev / "events.jsonl").read_bytes() == before
    writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())  # a new connection
    assert len(store.read_run(RUN).lines()) == 2


def test_a_copy_out_of_wal_mode_is_refused_and_can_be_put_back(store, tmp_path):
    write_run(store, tmp_path / "runs", RUN)
    copy = tmp_path / "copy.db"
    db(store).execute(f"VACUUM INTO '{copy}'")
    with pytest.raises(ProveviError, match="not WAL"):
        SqliteStore(copy, create=False)
    SqliteStore(copy)  # opening it to write puts it in WAL mode, as the docs say
    assert SqliteStore(copy, create=False).list_runs() == [RUN]


def test_a_bundle_the_store_did_not_record_is_not_a_problem(store, tmp_path):
    folder = write_run(store, tmp_path / "runs", RUN)
    (folder / "bundle.json").write_text(json.dumps({"uri": "run://a/b/c/x", "pinned": None}))
    assert verify_run(folder, store=store).ok
    db(store).execute("UPDATE provevi_runs SET bundle_uri = 'run://a/b/c/y'")
    assert "the store's bundle_uri differs from bundle.json" in verify_run(folder, store=store).problems


# Each test below is one finding of the third review of Phase 2.

def test_a_failed_reconnect_keeps_the_first_error_and_the_run_goes_on(store, tmp_path, monkeypatch):
    ev = tmp_path / "evidence"
    writer = store.open_run(RUN, ev)
    writer.append(EvidenceEvent(RUN, 0, "t", "run_created", "c", "a").line())
    writer._conn = FailingRollback(writer._conn)
    real = provevi.sqlite.connect
    monkeypatch.setattr(provevi.sqlite, "connect", lambda *a, **k: (_ for _ in ()).throw(ProveviError("no")))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):  # not the reconnect's error
        writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())
    monkeypatch.setattr(provevi.sqlite, "connect", real)
    writer.append(EvidenceEvent(RUN, 1, "t", "note_added", "c", "a").line())  # it reconnects now
    assert len(store.read_run(RUN).lines()) == 2


def test_a_short_write_never_joins_two_lines_in_the_file_store(tmp_path, monkeypatch):
    import errno
    import os

    from provevi import JsonlSink

    sink = JsonlSink(tmp_path / "events.jsonl")
    sink.append("one")
    real = os.write
    calls = []

    def short_then_full(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real(fd, data[:2])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(os, "write", short_then_full)
    with pytest.raises(OSError):
        sink.append("lost line")
    sink.append("two")
    sink.close()
    assert (tmp_path / "events.jsonl").read_text() == "one\ntwo\n"


def test_the_events_file_keeps_the_usual_mode(tmp_path):
    import os

    from provevi import JsonlSink

    old = os.umask(0o002)
    try:
        JsonlSink(tmp_path / "events.jsonl").close()
    finally:
        os.umask(old)
    assert (tmp_path / "events.jsonl").stat().st_mode & 0o777 == 0o664
