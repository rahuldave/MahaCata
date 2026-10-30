"""Resolution to snapshots, staging and the input check."""
import os

import pytest

from provrun.access import StubBroker
from provrun.context import RunContext
from provrun.evidence import read_events
from provrun.resolve import ProvcatResolver
from provrun.stagers import InProcessStager, InputCheckError


def staged(tmp_path, catalog, docs):
    uris = [f"file://{docs}/report.txt", f"file://{docs}/notes"]
    c = RunContext.open(org="acme", domain="benefits", env="dev", actor="tester",
                        broker=StubBroker(uris, physical_schemes=catalog.stores.keys()),
                        resolver=ProvcatResolver(catalog), catalog=catalog, runs_root=tmp_path / "runs")
    snaps = [(u.rsplit("/", 1)[1], c.resolver.snapshot(c, c.broker.request(c, u, "mounted_snapshot"))) for u in uris]
    stager = InProcessStager()
    return c, stager, stager.stage(c, snaps), snaps


def test_inputs_are_staged_read_only_with_evidence(tmp_path, catalog, docs):
    c, _, run, snaps = staged(tmp_path, catalog, docs)
    assert [i.agent_path for i in run.inputs] == ["/input/report.txt", "/input/notes"]
    assert (run.input_dir / "report.txt").read_text().startswith("Revenue")
    assert not os.access(run.input_dir / "report.txt", os.W_OK)
    assert not os.access(run.input_dir / "notes", os.W_OK)
    events = read_events(c.folder / "evidence")
    types = [e.type for e in events]
    assert types.count("snapshot_materialized") == 2 and types.count("input_staged") == 2
    snap_event = next(e for e in events if e.type == "snapshot_materialized")
    assert snap_event.data["sha256"] == snaps[0][1].digest


def test_a_change_to_a_staged_input_is_found_and_does_not_reach_the_snapshot(tmp_path, catalog, docs):
    c, stager, run, snaps = staged(tmp_path, catalog, docs)
    f = run.input_dir / "report.txt"
    f.chmod(0o644)
    f.write_text("Revenue was 99 million.")
    with pytest.raises(InputCheckError, match="report.txt"):
        stager.verify_inputs(c, run)
    assert snaps[0][1].path.read_text().startswith("Revenue in 2025")
    checks = [e for e in read_events(c.folder / "evidence") if e.type == "input_verified"]
    assert [e.data["unchanged"] for e in checks] == [False, True]


def test_a_repeated_name_is_refused(tmp_path, catalog, docs):
    c, stager, run, snaps = staged(tmp_path, catalog, docs)
    with pytest.raises(ValueError, match="repeated"):
        InProcessStager().stage(c, [("x", snaps[0][1]), ("x", snaps[0][1])])
