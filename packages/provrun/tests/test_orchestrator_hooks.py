"""An orchestrator's hooks see every catalog step of a provrun run."""
import asyncio
from types import SimpleNamespace

from provcat import Catalog

from provrun.backends.litellm import LiteLLMBackend
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec


def test_every_catalog_step_of_a_run_goes_through_the_callers_hooks(tmp_path, docs):
    seen = []

    def record(ctx, req, next):
        seen.append((ctx.app, req.spot, str(req.logical or req.physical)))
        return next(req)

    async def answer(**kw):
        choice = SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    catalog = Catalog(cache_root=tmp_path / "cache", hooks={"*": [record]}, app="orchestrator")
    agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                      inputs=[InputSpec(f"file://{docs}/report.txt")])
    out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=tmp_path / "runs", org="acme",
                                domain="benefits", env="dev", actor="tester",
                                backend=LiteLLMBackend(answer)))
    assert out.status == "completed", out.error
    spots = [s for _, s, _ in seen]
    assert {"pin", "fetch", "snapshot"} <= set(spots)
    assert all(app == "orchestrator" for app, _, _ in seen)
    assert any(s == "snapshot" and u.endswith("report.txt") for _, s, u in seen)


def test_a_runs_bundle_is_written_and_committed_through_the_callers_hooks(tmp_path, docs, monkeypatch):
    import boto3
    from moto import mock_aws

    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    seen = []

    def record(ctx, req, next):
        seen.append((ctx.app, req.spot, str(req.logical or req.physical)))
        return next(req)

    async def answer(**kw):
        choice = SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    scheme = {"run": {"parts": ["org", "domain", "env"], "rest": "path",
                      "target": "s3://runs/{ref}/{org}/{domain}/{env}/{path}", "ref": "main"}}
    with mock_aws():
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="runs")
        s3.put_bucket_versioning(Bucket="runs", VersioningConfiguration={"Status": "Enabled"})
        catalog = Catalog({"schemes": scheme}, cache_root=tmp_path / "cache", hooks={"*": [record]},
                          app="orchestrator")
        agent = AgentSpec(name="q", backend="litellm", model="m", prompt="p",
                          inputs=[InputSpec(f"file://{docs}/report.txt")])
        out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=tmp_path / "runs", org="acme",
                                    domain="benefits", env="dev", actor="tester",
                                    backend=LiteLLMBackend(answer), bundle_scheme="run"))
    assert out.status == "completed" and out.bundle.pinned, (out.error, out.bundle.note)
    puts = [u for _, s, u in seen if s == "put"]
    assert any(u.endswith("/evidence/events.jsonl") for u in puts) and any(u.endswith("/output/response.txt")
                                                                          for u in puts)
    assert all(u.startswith("run://acme/benefits/dev/") for u in puts)  # logical, so a gate sees the name
    assert any(s == "commit" for _, s, _ in seen)
    assert all(app == "orchestrator" for app, _, _ in seen)
