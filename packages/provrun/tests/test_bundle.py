"""The bundle of a run, written and committed through provcat."""
import asyncio
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from provcat import Catalog
from provrun.backends.litellm import LiteLLMBackend
from provrun.evidence import EvidenceError
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec

RUN_SCHEME = {"run": {"parts": ["org", "domain", "env"], "rest": "path",
                      "target": "s3://runs/{ref}/{org}/{domain}/{env}/{path}", "ref": "main"}}


async def answer(**kw):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="runs")
        s3.put_bucket_versioning(Bucket="runs", VersioningConfiguration={"Status": "Enabled"})
        yield s3


def go(catalog, runs, docs, **kw):
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    return asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                 env="dev", actor="t", backend=LiteLLMBackend(answer), **kw))


def test_the_bundle_is_committed_and_reads_back_at_its_commit(aws, tmp_path, runs, docs):
    catalog = Catalog({"schemes": RUN_SCHEME}, cache_root=tmp_path / "cache")
    out = go(catalog, runs, docs, bundle_scheme="run")
    assert out.status == "completed", out.error
    assert out.bundle.uri == f"run://acme/benefits/dev/{out.run_id}"
    assert out.bundle.pinned and "?at=commit:" in out.bundle.pinned
    pinned_events = out.bundle.pinned.replace("?at=", "/evidence/events.jsonl?at=")
    assert catalog.read(pinned_events) == (out.folder / "evidence" / "events.jsonl").read_bytes()
    pinned_output = out.bundle.pinned.replace("?at=", "/output/response.txt?at=")
    assert catalog.read(pinned_output) == b"42"


def test_a_store_without_commits_keeps_the_bundle_unpinned(tmp_path, runs, docs):
    scheme = {"run": {**RUN_SCHEME["run"], "target": f"file://{tmp_path}/bundles/{{org}}/{{domain}}/{{env}}/{{path}}"}}
    del scheme["run"]["ref"]
    catalog = Catalog({"schemes": scheme}, cache_root=tmp_path / "cache")
    out = go(catalog, runs, docs, bundle_scheme="run")
    assert out.status == "completed", out.error
    assert out.bundle.pinned is None and "no commits" in out.bundle.note
    assert (tmp_path / "bundles" / "acme" / "benefits" / "dev" / out.run_id / "output" / "response.txt").exists()


def test_a_bundle_that_cannot_be_written_stops_a_strict_run(tmp_path, runs, docs):
    catalog = Catalog({"schemes": RUN_SCHEME}, cache_root=tmp_path / "cache", offline=True)
    with pytest.raises(EvidenceError, match="cannot write the bundle"):
        go(catalog, runs, docs, bundle_scheme="run", mode="strict")
