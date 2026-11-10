"""A run that names its experiment, or its parent run: the pin, the hash and the parent in run_created."""
import asyncio

import boto3
import pytest
from moto import mock_aws

from provcat import Catalog
from provevi import Experiment, verify_run, write_experiment

from provrun import RunFile, read_events
from provrun.errors import RunFileError, RunSettingError
from provrun.runner import run_agent, run_file_sync
from provrun.spec import AgentSpec, InputSpec

EXP = "experiment://acme/risk/dev/credit/q"
SCHEMES = {"experiment": {"parts": ["org", "team", "env", "project", "name"], "rest": "path",
                          "target": "s3://exps/{ref}/{org}/{team}/{env}/{project}/{name}/{path}", "ref": "main"}}
EVAL = Experiment("agent-eval", "Does a retrieval step raise accuracy?", ("data+table://acme/risk/dev/evals/q",),
                  ("accuracy",))
RUN = """
run: {org: acme, domain: risk, env: dev, actor: tester}
experiment: EXPERIMENT
agent:
  name: q
  backend: litellm
  model: openai/gpt-4o-mini
  prompt: What was revenue?
  inputs: [INPUT]
  settings: {mock_response: Revenue was 42 million.}
"""


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="exps")
        s3.put_bucket_versioning(Bucket="exps", VersioningConfiguration={"Status": "Enabled"})
        yield Catalog({"schemes": SCHEMES}, cache_root=tmp_path / "cache")


def run_file(tmp_path, docs, experiment=EXP, name="run.yaml"):
    f = tmp_path / name
    f.write_text(RUN.replace("EXPERIMENT", experiment).replace("INPUT", f"file://{docs}/report.txt"))
    return RunFile.load(f)


def created(out):
    return read_events(out.folder / "evidence")[0].data


def test_two_runs_of_one_version_name_one_pin_and_a_new_version_a_new_pin(tmp_path, docs, catalog, runs):
    first = write_experiment(catalog, EXP, EVAL, "first")
    rf = run_file(tmp_path, docs)
    a = run_file_sync(rf, catalog=catalog, runs_root=runs)
    b = run_file_sync(rf, catalog=catalog, runs_root=runs)
    assert a.status == b.status == "completed", (a.error, b.error)
    assert created(a)["experiment"] == EXP
    assert created(a)["experiment_pin"] == created(b)["experiment_pin"] == first.pin
    assert created(a)["experiment_sha256"] == first.sha256
    second = write_experiment(catalog, EXP, Experiment("agent-eval", "And recall?", EVAL.inputs, ("recall",)), "two")
    c = run_file_sync(rf, catalog=catalog, runs_root=runs)
    assert created(c)["experiment_pin"] == second.pin != first.pin
    assert created(c)["experiment_sha256"] == second.sha256
    # prove verify reads the experiment at each run's own pin.
    for out in (a, c):
        check = verify_run(out.folder, catalog=catalog)
        assert check.ok, check.problems
        assert check.experiment == created(out)["experiment_pin"]


def test_a_run_can_name_an_old_version_by_its_pin(tmp_path, docs, catalog, runs):
    first = write_experiment(catalog, EXP, EVAL, "first")
    write_experiment(catalog, EXP, Experiment("agent-eval", "Later.", EVAL.inputs, ("recall",)), "two")
    out = run_file_sync(run_file(tmp_path, docs, first.pin), catalog=catalog, runs_root=runs)
    assert created(out)["experiment"] == first.pin
    assert created(out)["experiment_pin"] == first.pin and created(out)["experiment_sha256"] == first.sha256
    assert verify_run(out.folder, catalog=catalog).ok


def test_a_run_that_names_no_experiment_records_none(docs, catalog, runs):
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev", actor="t"))
    assert not {"experiment", "experiment_pin", "experiment_sha256", "parent_run"} & set(created(out))


def test_an_experiment_that_is_not_there_stops_the_run_before_it_starts(tmp_path, docs, catalog, runs):
    rf = run_file(tmp_path, docs, "experiment://acme/risk/dev/credit/missing")
    with pytest.raises(RunSettingError, match="cannot pin the experiment"):
        run_file_sync(rf, catalog=catalog, runs_root=runs)
    assert not runs.exists() or not list(runs.iterdir())


def test_an_experiment_in_a_store_with_no_commits_stops_the_run_before_it_starts(tmp_path, docs, runs):
    # A folder on this machine keeps no versions, so an experiment there cannot be pinned.
    loose = {"experiment": {"parts": ["org", "team", "env", "project", "name"], "rest": "path",
                            "target": f"file://{tmp_path}/exps/{{org}}/{{name}}/{{path}}"}}
    (tmp_path / "exps" / "acme" / "q").mkdir(parents=True)
    (tmp_path / "exps" / "acme" / "q" / "experiment.yaml").write_bytes(EVAL.to_bytes())
    catalog = Catalog({"schemes": loose}, cache_root=tmp_path / "cache")
    with pytest.raises(RunSettingError, match="no commits"):
        run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs)
    assert not runs.exists() or not list(runs.iterdir())


@pytest.mark.parametrize("value", ["data://acme/risk/dev/q", "[a, b]", "''"])
def test_a_run_file_names_an_experiment_by_its_uri_only(tmp_path, docs, value):
    with pytest.raises(RunFileError, match="experiment://"):
        run_file(tmp_path, docs, value)


def test_a_child_run_records_its_parent(docs, catalog, runs):
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    kw = dict(catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev", actor="t")
    parent = asyncio.run(run_agent(agent, **kw))
    child = asyncio.run(run_agent(agent, parent_run=parent.run_id, **kw))
    assert created(child)["parent_run"] == parent.run_id
    with pytest.raises(RunSettingError, match="parent_run"):
        asyncio.run(run_agent(agent, parent_run="../etc", **kw))


def test_the_catalog_s_hooks_see_the_pin_and_a_gate_can_refuse_it(tmp_path, docs, catalog, runs):
    from provcat.hooks import AccessDenied, Hooks

    write_experiment(catalog, EXP, EVAL, "first")
    seen = []

    def gate(ctx, req, next):
        seen.append((req.spot, str(req.logical)))
        if req.logical is not None and str(req.logical).startswith("experiment://"):
            raise AccessDenied("this app may not read experiments")
        return next(req)

    gated = Catalog({"schemes": SCHEMES}, cache_root=tmp_path / "cache2", hooks=Hooks({"*": [gate]}))
    with pytest.raises(RunSettingError, match="may not read experiments"):
        run_file_sync(run_file(tmp_path, docs), catalog=gated, runs_root=runs)
    assert ("resolve", EXP) in seen  # the gate refused it as the catalog resolved it
    assert not runs.exists() or not list(runs.iterdir())


def test_an_os_error_while_pinning_is_a_setting_error(tmp_path, docs, catalog, runs, monkeypatch):
    # Review: a store or hook outside provcat that raised OSError broke provr run with a traceback.
    write_experiment(catalog, EXP, EVAL, "first")

    def down(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(catalog, "read", down)
    with pytest.raises(RunSettingError, match="network down"):
        run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs)
    assert not runs.exists() or not list(runs.iterdir())


def test_an_experiment_argument_takes_the_place_of_the_run_file_s(tmp_path, docs, catalog, runs):
    # Review: run_file(rf, experiment=...) raised TypeError for a repeated keyword.
    first = write_experiment(catalog, EXP, EVAL, "first")
    write_experiment(catalog, EXP, Experiment("agent-eval", "Later.", EVAL.inputs, ("recall",)), "two")
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, experiment=first.pin)
    assert created(out)["experiment"] == first.pin == created(out)["experiment_pin"]


def test_a_parent_run_that_is_not_in_the_store_stops_the_run_before_it_starts(docs, catalog, runs):
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    kw = dict(catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev", actor="t")
    with pytest.raises(RunSettingError, match="not in the evidence store"):
        asyncio.run(run_agent(agent, parent_run="run-20260101T000000Z-0a1b2c3d", **kw))
    assert not runs.exists() or not list(runs.iterdir())


def test_a_parent_run_is_looked_for_in_the_run_s_own_store(tmp_path, docs, catalog, runs):
    from provevi import SqliteStore

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    kw = dict(catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev", actor="t")
    in_files = asyncio.run(run_agent(agent, **kw))
    store = SqliteStore(tmp_path / "evidence.db")
    in_db = asyncio.run(run_agent(agent, store=store, **kw))
    child = asyncio.run(run_agent(agent, store=store, parent_run=in_db.run_id, **kw))
    assert created(child)["parent_run"] == in_db.run_id
    # A run that only the file store holds is not in the sqlite store.
    with pytest.raises(RunSettingError, match="not in the evidence store"):
        asyncio.run(run_agent(agent, store=store, parent_run=in_files.run_id, **kw))


def test_a_store_that_cannot_read_runs_back_takes_the_parent_unchecked(docs, catalog, runs):
    from provevi import FileRunWriter
    from provevi.store import EvidenceStore

    class WriteOnly(EvidenceStore):
        def open_run(self, run_id, folder):
            return FileRunWriter(folder)

    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev",
                                actor="t", store=WriteOnly(), parent_run="run-20260101T000000Z-0a1b2c3d"))
    assert created(out)["parent_run"] == "run-20260101T000000Z-0a1b2c3d"


def grants(out):
    return [(e.type, e.refs[0], e.data.get("mode"), e.data.get("purpose")) for e in read_events(out.folder / "evidence")
            if e.type.startswith("access_")]


def test_the_broker_grants_the_experiment_and_the_run_records_it(tmp_path, docs, catalog, runs):
    write_experiment(catalog, EXP, EVAL, "first")
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs)
    assert out.status == "completed", out.error
    events = read_events(out.folder / "evidence")
    assert [e.type for e in events[:3]] == ["run_created", "access_requested", "access_granted"]
    assert grants(out)[:2] == [("access_requested", EXP, "pinned_read", "experiment"),
                               ("access_granted", EXP, "pinned_read", None)]
    assert events[2].data["decision"] == "grant" and events[2].data["policy"] == "stub"
    assert verify_run(out.folder, catalog=catalog).ok


def refused(out, catalog, experiment, reason):
    """Check that out is a run that the broker's refusal of experiment failed."""
    assert out.status == "failed" and reason in str(out.error), out.error
    events = read_events(out.folder / "evidence")
    assert [e.type for e in events][:3] == ["run_created", "access_requested", "access_denied"]
    assert events[-1].type == "run_failed"
    assert events[1].refs == events[2].refs == (experiment,) and events[1].data["purpose"] == "experiment"
    assert events[2].data["decision"] == "deny" and reason in events[2].data["reason"]
    assert not {"experiment", "experiment_pin", "experiment_sha256"} & events[0].data.keys()
    assert not any(e.type == "access_requested" and e.data["purpose"] != "experiment" for e in events)
    check = verify_run(out.folder, catalog=catalog)
    assert check.ok, check.problems


def test_the_broker_refuses_an_experiment_of_another_environment_before_it_is_read(tmp_path, docs, catalog,
                                                                                    runs, monkeypatch):
    prod = "experiment://acme/risk/prod/credit/q"
    write_experiment(catalog, prod, EVAL, "first")
    import provevi

    read = []
    monkeypatch.setattr(provevi, "read_experiment", lambda *a: read.append(a))
    out = run_file_sync(run_file(tmp_path, docs, prod), catalog=catalog, runs_root=runs)
    assert read == []
    refused(out, catalog, prod, "environment prod")


def test_a_broker_passed_in_decides_the_experiment_too(tmp_path, docs, catalog, runs):
    from provrun.access import StubBroker

    write_experiment(catalog, EXP, EVAL, "first")
    only_inputs = StubBroker([f"file://{docs}/report.txt"], physical_schemes=catalog.stores.keys())
    refused(run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=only_inputs),
            catalog, EXP, "allow list")
    both = StubBroker([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    assert run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=both).status == "completed"


def test_an_input_cannot_ask_for_pinned_read(docs, catalog, runs):
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt", mode="pinned_read")])
    with pytest.raises(RunSettingError, match="pinned_read"):
        asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev", actor="t"))
    assert not runs.exists() or not list(runs.iterdir())


def test_a_broker_that_reads_more_than_a_run_scope_has_gives_a_setting_error(tmp_path, docs, catalog, runs):
    from provrun.access import StubBroker

    class Nosy(StubBroker):
        def decide(self, ctx, uri, mode, purpose):
            ctx.folder  # a RunContext has it; a RunScope does not
            return super().decide(ctx, uri, mode, purpose)

    write_experiment(catalog, EXP, EVAL, "first")
    nosy = Nosy([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    with pytest.raises(RunSettingError, match="could not decide the experiment: AttributeError"):
        run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=nosy)
    assert not runs.exists() or not list(runs.iterdir())


def test_a_grant_that_cannot_be_recorded_fails_the_run_and_seals_it(tmp_path, docs, catalog, runs):
    from provrun.access import StubBroker

    class Down(StubBroker):
        def record(self, ctx, grant, purpose=""):
            raise RuntimeError("audit down")

    write_experiment(catalog, EXP, EVAL, "first")
    down = Down([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=down)
    assert out.status == "failed" and "audit down" in str(out.error)
    assert read_events(out.folder / "evidence")[-1].type == "run_failed"
    check = verify_run(out.folder, catalog=catalog)
    assert check.ok, check.problems


def test_a_grant_that_expired_before_the_run_opened_fails_the_run(tmp_path, docs, catalog, runs):
    from datetime import timedelta

    from provrun.access import StubBroker

    write_experiment(catalog, EXP, EVAL, "first")
    stale = StubBroker([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys(),
                       ttl=timedelta(seconds=-1))
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=stale)
    assert out.status == "failed" and "expired" in str(out.error)
    types = [e.type for e in read_events(out.folder / "evidence")]
    assert types[:3] == ["run_created", "access_requested", "access_denied"] and types[-1] == "run_failed"
    check = verify_run(out.folder, catalog=catalog)
    assert check.ok, check.problems


def test_an_experiment_on_a_branch_must_be_in_the_allow_list_itself(tmp_path, docs, catalog, runs):
    write_experiment(catalog, EXP, EVAL, "first")
    branch = EXP + "?at=branch:main"
    refused(run_file_sync(run_file(tmp_path, docs, branch), catalog=catalog, runs_root=runs), catalog, branch,
            "selector")
    agent = run_file(tmp_path, docs).agent
    out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="risk", env="dev",
                                actor="t", experiment=branch, allow=[branch]))
    assert out.status == "completed", out.error
    # A commit names one version only, so the run's own may be pinned to it.
    pinned = created(out)["experiment_pin"]
    assert run_file_sync(run_file(tmp_path, docs, pinned), catalog=catalog, runs_root=runs).status == "completed"


def test_a_bad_environment_is_refused_before_the_broker_or_the_catalog_hears_of_it(docs, catalog, runs,
                                                                                    monkeypatch):
    import provevi

    asked = []
    monkeypatch.setattr(provevi, "read_experiment", lambda *a: asked.append(a))
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p", settings={"mock_response": "x"},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    with pytest.raises(RunSettingError, match="env"):
        asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="r", env="Bad Env",
                              actor="t", experiment=EXP))
    assert asked == []


@pytest.mark.parametrize("value", ["prod", "main", "ABCDEF1", "abc"])
def test_a_commit_selector_that_names_no_commit_id_must_be_in_the_allow_list(tmp_path, docs, catalog, runs, value):
    # Some stores take a branch name where a commit goes, so commit:prod could read the prod branch.
    uri = f"{EXP}?at=commit:{value}"
    refused(run_file_sync(run_file(tmp_path, docs, uri), catalog=catalog, runs_root=runs), catalog, uri, "selector")


@pytest.mark.parametrize("expires, ok", [("2999-01-01T00:00:00", True), ("tomorrow", False), (12345, False)])
def test_a_grant_s_expiry_with_no_zone_is_utc_and_one_that_does_not_parse_stops_the_run(tmp_path, docs, catalog,
                                                                                         runs, expires, ok):
    from dataclasses import replace

    from provrun.access import StubBroker

    class Odd(StubBroker):
        def decide(self, ctx, uri, mode, purpose):
            return replace(super().decide(ctx, uri, mode, purpose), expires_at=expires)

    write_experiment(catalog, EXP, EVAL, "first")
    odd = Odd([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    if ok:
        out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=odd)
        assert out.status == "completed", out.error
    else:
        with pytest.raises(RunSettingError, match="could not decide the experiment.*not an ISO 8601 time"):
            run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=odd)
        assert not runs.exists() or not list(runs.iterdir())


def test_an_experiment_that_ends_with_a_slash_allows_no_folder(tmp_path, docs, catalog, runs):
    # In the allow list, an entry that ends with / allows every URI under it.
    from provrun.runner import _own_experiment

    assert _own_experiment(EXP + "/") == []
    refused(run_file_sync(run_file(tmp_path, docs, EXP + "/"), catalog=catalog, runs_root=runs), catalog, EXP + "/",
            "allow list")


def test_a_refused_experiment_is_in_the_store_s_access_rows_but_has_no_runs(tmp_path, docs, catalog, runs):
    from provevi import SqliteStore

    prod = "experiment://acme/risk/prod/credit/q"
    store = SqliteStore(tmp_path / "evidence.db")
    out = run_file_sync(run_file(tmp_path, docs, prod), catalog=catalog, runs_root=runs, store=store)
    refused(out, catalog, prod, "environment prod")
    assert store.run(out.run_id)["status"] == "failed"
    assert [a["decision"] for a in store.access(out.run_id)] == ["denied"]
    assert store.experiments() == [] and store.runs_of_experiment("acme/risk/prod/credit/q") == []
    assert verify_run(out.folder, catalog=catalog, store=store).ok


def test_a_broker_that_raises_access_denied_with_a_grant_still_refuses(tmp_path, docs, catalog, runs, monkeypatch):
    from dataclasses import replace

    from provrun.access import AccessDenied, StubBroker

    class Muddled(StubBroker):
        def decide_early(self, scope, uri, mode, purpose=""):
            raise AccessDenied(replace(super().decide(scope, uri, mode, purpose), reason="no, after all"))

    import provevi

    read = []
    monkeypatch.setattr(provevi, "read_experiment", lambda *a: read.append(a))
    write_experiment(catalog, EXP, EVAL, "first")
    muddled = Muddled([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=muddled)
    assert read == []
    refused(out, catalog, EXP, "no, after all")


def test_a_refusal_with_no_grant_is_a_setting_error(tmp_path, docs, catalog, runs):
    from provrun.access import AccessDenied, Grant, StubBroker

    class NoGrant(StubBroker):
        def decide_early(self, scope, uri, mode, purpose=""):
            e = AccessDenied(Grant("g", uri, mode, "deny", "x", None, "p"))
            e.grant = None
            raise e

    broker = NoGrant([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    with pytest.raises(RunSettingError, match="refused the experiment with no grant"):
        run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=broker)
    assert not runs.exists() or not list(runs.iterdir())


def test_a_refusal_is_recorded_for_the_experiment_whatever_uri_the_grant_names(tmp_path, docs, catalog, runs):
    from dataclasses import replace

    from provrun.access import AccessDenied, StubBroker

    class Elsewhere(StubBroker):
        def decide_early(self, scope, uri, mode, purpose=""):
            g = super().decide(scope, uri, mode, purpose)
            raise AccessDenied(replace(g, uri="file:///elsewhere", mode="mounted_snapshot", reason="not this one"))

    broker = Elsewhere([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    out = run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=broker)
    refused(out, catalog, EXP, "not this one")
    events = read_events(out.folder / "evidence")
    assert events[1].data["mode"] == events[2].data["mode"] == "pinned_read"


def test_a_refused_experiment_with_a_parent_not_in_the_store_is_a_setting_error(tmp_path, docs, catalog, runs):
    prod = "experiment://acme/risk/prod/credit/q"
    agent = run_file(tmp_path, docs, prod).agent
    with pytest.raises(RunSettingError, match="not in the evidence store"):
        asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="risk", env="dev",
                              actor="t", experiment=prod, parent_run="run-20260101T000000Z-00000000"))
    assert not runs.exists() or not list(runs.iterdir())


@pytest.mark.parametrize("answer", ["none", "elsewhere", "other mode"])
def test_a_grant_for_another_request_than_the_experiment_is_a_setting_error(tmp_path, docs, catalog, runs, answer):
    from dataclasses import replace

    from provrun.access import StubBroker

    class Odd(StubBroker):
        def decide_early(self, scope, uri, mode, purpose=""):
            g = super().decide_early(scope, uri, mode, purpose)
            return {"none": None, "elsewhere": replace(g, uri="file:///elsewhere"),
                    "other mode": replace(g, mode="mounted_snapshot")}[answer]

    write_experiment(catalog, EXP, EVAL, "first")
    odd = Odd([f"file://{docs}/report.txt", EXP], physical_schemes=catalog.stores.keys())
    with pytest.raises(RunSettingError, match="answered for another request"):
        run_file_sync(run_file(tmp_path, docs), catalog=catalog, runs_root=runs, broker=odd)
    assert not runs.exists() or not list(runs.iterdir())
