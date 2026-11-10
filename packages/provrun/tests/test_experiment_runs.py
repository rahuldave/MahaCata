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
