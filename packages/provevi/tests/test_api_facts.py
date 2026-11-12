"""Facts that the Reference pages state about provevi's public API, pinned so a change to the code must change the docs."""
import sqlite3
import subprocess

import pytest

import provevi.sqlite
from provevi import Experiment, FileStore, SqliteStore, TelemetryPolicy, open_store, plain_name
from provevi.checks import _once
from provevi.cli import main

from test_provevi import RUN, write_run


def test_a_catalog_opener_that_fails_is_called_again_and_one_that_succeeds_only_once():
    calls = []

    def opener():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return object()

    get = _once(opener)
    with pytest.raises(RuntimeError):
        get()
    first = get()
    assert get() is first and len(calls) == 2


def test_bad_arguments_exit_2_through_argparse():
    with pytest.raises(SystemExit) as e:
        main(["no-such-command"])
    assert e.value.code == 2


def test_prove_ui_gives_mlflow_s_exit_code(tmp_path, monkeypatch):
    import shutil

    SqliteStore(tmp_path / "e.db")
    monkeypatch.setattr(shutil, "which", lambda cmd: cmd)
    monkeypatch.setattr(subprocess, "call", lambda command, env: 7)
    assert main(["--store", f"sqlite:///{tmp_path / 'e.db'}", "ui"]) == 7


@pytest.mark.parametrize("value, plain", [(3, True), (True, True), (1.5, False), ([], False)])
def test_a_whole_number_is_plain_and_a_fraction_is_not(value, plain):
    assert plain_name(value) is plain


def test_inputs_that_cannot_be_looped_over_raise_type_error():
    with pytest.raises(TypeError):
        Experiment("ml", "a question", inputs=5)


def test_a_write_to_the_sqlite_file_can_raise_a_sqlite_error(tmp_path, monkeypatch):
    store = SqliteStore(tmp_path / "e.db")
    writer = store.open_run(RUN, tmp_path / RUN / "evidence")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(provevi.sqlite, "connect", broken)
    with pytest.raises(sqlite3.OperationalError):
        writer.record_bundle("run://a/b/dev/x", None)


def test_open_store_to_write_brings_an_older_schema_up_to_date(tmp_path, monkeypatch):
    path = tmp_path / "e.db"
    SqliteStore(path)
    following = provevi.sqlite.SCHEMA_VERSION + 1
    monkeypatch.setattr(provevi.sqlite, "SCHEMA_VERSION", following)
    monkeypatch.setitem(provevi.sqlite.MIGRATIONS, following, ["ALTER TABLE provevi_runs ADD COLUMN note TEXT"])
    open_store(f"sqlite:///{path}")
    assert provevi.sqlite.schema_version(sqlite3.connect(path)) == following


def test_the_file_store_reads_a_given_folder_whatever_the_run_id(tmp_path):
    folder = write_run(tmp_path / "runs")
    record = FileStore(tmp_path / "elsewhere").read_run("no-such-run", folder / "evidence")
    assert len(record.lines()) == 2


def test_a_policy_keeps_its_hosts_in_the_order_given():
    hosts = ["b.example", "a.example"]
    assert TelemetryPolicy.from_dict({"version": 1, "hosts": hosts}).to_dict()["hosts"] == hosts
