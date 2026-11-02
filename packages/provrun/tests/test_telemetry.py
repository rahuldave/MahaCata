"""Telemetry control: the policy, the environment and provrun's providers."""
import asyncio
import json
import os
import warnings
from types import SimpleNamespace

import pytest
from provevi.telemetry import TelemetryPolicy

from provrun import telemetry
from provrun.backends.litellm import LiteLLMBackend
from provrun.cli import main as provr
from provrun.errors import RunSettingError
from provrun.evidence import read_events, verify
from provrun.runner import run_agent, run_file_sync
from provrun.spec import AgentSpec, RunFile

AGENT = AgentSpec(name="q", backend="litellm", model="openai/gpt-x", prompt="p")

POLICY = {
    "version": 1,
    "libraries": {"litellm": {"versions": ">=1", "telemetry": ["collector"]}},
    "destinations": {"collector": {"kind": "otlp", "receives": "usage",
                                   "endpoint": "https://collector.example/v1/traces"}},
    "hosts": ["api.openai.com", "collector.example"],
}


def seeing(seen):
    """A LiteLLM backend whose model call keeps a copy of the environment."""
    async def acompletion(**kw):
        seen.append(dict(os.environ))
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    return LiteLLMBackend(acompletion)


def run(catalog, runs, env="dev", **kw):
    kw.setdefault("backend", seeing([]))
    return asyncio.run(run_agent(AGENT, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                 env=env, actor="tester", **kw))


def created(out):
    return read_events(out.folder / "evidence")[0].data["telemetry"]


def policy_file(tmp_path, doc=POLICY):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(doc))
    return p


NOISY = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318", "OTEL_SDK_DISABLED": "true",
         "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer s3cr3t-otlp",
         "OTEL_TRACES_EXPORTER": "console", "LOGFIRE_TOKEN": "s3cr3t-token", "LITELLM_LOG": "DEBUG",
         "LITELLM_MODE": "DEV", "ANTHROPIC_LOG": "debug"}


def test_a_run_sets_the_environment_and_puts_it_back(catalog, runs, monkeypatch):
    for k, v in NOISY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("MLFLOW_TRACE_SAMPLING_RATIO", raising=False)
    seen = []
    out = run(catalog, runs, backend=seeing(seen))
    assert out.status == "completed", out.error
    [env] = seen
    for k, v in telemetry.SETTINGS.items():
        assert env[k] == v
    assert not {"OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SDK_DISABLED",
                "LOGFIRE_TOKEN", "ANTHROPIC_LOG"} & env.keys()
    for k, v in NOISY.items():
        assert os.environ[k] == v  # put back after the run
    assert "MLFLOW_TRACE_SAMPLING_RATIO" not in os.environ
    record = created(out)
    assert record["policy"] == TelemetryPolicy.deny_all().digest()
    assert record["removed"] == ["ANTHROPIC_LOG", "LOGFIRE_TOKEN", "OTEL_EXPORTER_OTLP_ENDPOINT",
                                 "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SDK_DISABLED"]
    assert "s3cr3t-token" not in (out.folder / "evidence" / "events.jsonl").read_text()  # names, never values


def test_credentials_go_even_for_an_approved_destination(catalog, runs, monkeypatch, tmp_path):
    """No library may use a destination's credential: without its endpoint,
    an exporter would send it to a default address."""
    for k, v in NOISY.items():
        monkeypatch.setenv(k, v)
    seen = []
    out = run(catalog, runs, backend=seeing(seen), policy=policy_file(tmp_path))
    [env] = seen
    assert not {"OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_ENDPOINT", "LOGFIRE_TOKEN"} & env.keys()
    assert created(out)["policy"] == TelemetryPolicy.from_dict(POLICY).digest()


def test_the_policy_comes_from_the_variable(catalog, runs, monkeypatch, tmp_path):
    monkeypatch.setenv(telemetry.POLICY_ENV, str(policy_file(tmp_path)))
    out = run(catalog, runs)
    assert created(out)["policy"] == TelemetryPolicy.from_dict(POLICY).digest()
    assert created(out)["policy_source"] == str(tmp_path / "policy.json")


def test_a_bad_policy_fails_before_the_run_folder(catalog, runs, tmp_path):
    bad = policy_file(tmp_path, {**POLICY, "version": 2})
    with pytest.raises(RunSettingError, match="version"):
        run(catalog, runs, policy=bad)
    assert not runs.exists() or not any(runs.iterdir())


def test_a_section_that_widens_the_policy_fails_before_the_run_folder(catalog, runs, tmp_path):
    with pytest.raises(RunSettingError, match="not allowed"):
        run(catalog, runs, policy=policy_file(tmp_path), telemetry={"hosts": ["evil.example"]})
    assert not runs.exists() or not any(runs.iterdir())


def test_a_section_narrows_the_policy(catalog, runs, tmp_path, monkeypatch):
    out = run(catalog, runs, policy=policy_file(tmp_path), telemetry={"destinations": {}, "hosts": []})
    narrowed = TelemetryPolicy.from_dict(POLICY).narrow({"destinations": {}, "hosts": []})
    assert created(out)["policy"] == narrowed.digest()


def runfile(tmp_path, body):
    p = tmp_path / "run.yaml"
    p.write_text(body)
    return p


RUNFILE = """run: {org: acme, domain: benefits, env: dev, actor: tester}
agent: {name: q, backend: litellm, model: openai/gpt-x, prompt: p}
"""


def test_a_run_file_carries_its_telemetry_section(catalog, runs, tmp_path):
    rf = RunFile.load(runfile(tmp_path, RUNFILE + "telemetry: {hosts: []}\n"))
    assert rf.telemetry == {"hosts": []}
    out = run_file_sync(rf, catalog=catalog, runs_root=runs, backend=seeing([]), policy=policy_file(tmp_path))
    assert created(out)["policy"] == TelemetryPolicy.from_dict(POLICY).narrow({"hosts": []}).digest()


@pytest.mark.parametrize("body, message", [
    (RUNFILE + "telemetry: [a]\n", "telemetry section must be a mapping"),
    (RUNFILE + "telemetry: {hosts: []}\ntelemetry: {}\n", "named twice"),
    ("run: {org: acme, org: other, domain: benefits, env: dev, actor: tester}\n"
     "agent: {name: q, backend: litellm, model: openai/gpt-x, prompt: p}\n", "named twice"),
])
def test_a_bad_run_file_is_refused(tmp_path, body, message):
    from provrun.errors import RunFileError

    with pytest.raises(RunFileError, match=message):
        RunFile.load(runfile(tmp_path, body))


def test_provr_takes_the_policy(catalog, runs, tmp_path, capsys, monkeypatch):
    rf = runfile(tmp_path, RUNFILE.replace("litellm", "nonesuch"))
    bad = policy_file(tmp_path, {**POLICY, "version": 2})
    assert provr(["--runs", str(runs), "run", str(rf), "--policy", str(bad)]) == 1
    assert "version" in capsys.readouterr().err


def test_untested_versions_fail_a_strict_run_and_warn_in_a_lenient_one(catalog, runs, tmp_path):
    old = {**POLICY, "libraries": {"pyyaml": {"versions": "<1", "telemetry": []}}}
    with pytest.raises(RunSettingError, match="pyyaml"):
        run(catalog, runs, env="prod", policy=policy_file(tmp_path, old))
    with pytest.warns(UserWarning, match="pyyaml"):
        out = run(catalog, runs, env="dev", policy=policy_file(tmp_path, old))
    assert out.status == "completed" and "pyyaml" in created(out)["problems"][0]


def test_a_run_records_the_libraries_imported_before_it(catalog, runs):
    import litellm  # noqa: F401  the backend's library, imported by this test process

    assert "litellm" in created(run(catalog, runs))["imported_before"]


def test_provrun_owns_the_global_providers():
    from opentelemetry import _logs, metrics, trace

    p = telemetry.providers()
    assert trace.get_tracer_provider() is p.tracer
    assert metrics.get_meter_provider() is p.meter
    assert _logs.get_logger_provider() is p.logger
    assert p.foreign == []


def test_a_foreign_processor_is_refused_and_recorded(catalog, runs):
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    p = telemetry.providers()
    exporter = InMemorySpanExporter()

    async def acompletion(**kw):
        p.tracer.add_span_processor(SimpleSpanProcessor(exporter))
        p.logger.add_log_record_processor(SimpleLogRecordProcessor(None))
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    out = run(catalog, runs, backend=LiteLLMBackend(acompletion))
    blocked = [e for e in read_events(out.folder / "evidence") if e.type == "telemetry_blocked"]
    assert [e.data["path"] for e in blocked] == ["opentelemetry.span_processor",
                                                 "opentelemetry.log_record_processor"]
    assert blocked[0].data["library"] == "opentelemetry" and "SimpleSpanProcessor" in blocked[0].data["detail"]
    assert verify(out.folder / "evidence") == []
    with p.tracer.get_tracer("t").start_as_current_span("s"):
        pass
    assert exporter.get_finished_spans() == ()  # the processor was never added
    with pytest.warns(UserWarning, match="span_processor"):  # outside a run, a warning
        p.tracer.add_span_processor(SimpleSpanProcessor(exporter))


def test_a_run_has_span_ids_from_provrun_s_provider(catalog, runs, monkeypatch):
    from provrun import otel

    monkeypatch.setattr(otel, "_tracer", telemetry.providers().tracer.get_tracer("provrun"))
    out = run(catalog, runs)
    model = next(e for e in read_events(out.folder / "evidence") if e.type == "model_called")
    assert len(model.data["span_id"]) == 16 and model.data["parent_span_id"] is None


def test_a_foreign_global_provider_fails_a_strict_run(catalog, runs, monkeypatch):
    monkeypatch.setattr(telemetry, "_providers", SimpleNamespace(foreign=["tracer"]))
    with pytest.raises(RunSettingError, match="tracer provider was set by someone else"):
        run(catalog, runs, env="prod")
    with pytest.warns(UserWarning, match="tracer provider"):
        out = run(catalog, runs, env="dev")
    assert created(out)["problems"]


def test_overlapping_runs_under_two_policies(catalog, runs, tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG", "DEBUG")
    seen = []
    gate = asyncio.Event()

    async def slow(**kw):
        seen.append(dict(os.environ))
        await gate.wait()
        return reply()

    async def quick(**kw):
        seen.append(dict(os.environ))
        gate.set()
        return reply()

    def one(backend, **kw):
        return run_agent(AGENT, catalog=catalog, runs_root=runs, org="acme", domain="benefits", env="dev",
                         actor="tester", backend=backend, **kw)

    async def go():
        first = asyncio.create_task(one(LiteLLMBackend(slow)))
        await asyncio.sleep(0.05)
        second = await one(LiteLLMBackend(quick), policy=policy_file(tmp_path))
        assert os.environ["LITELLM_LOG"] == "ERROR"  # the first run is still going on
        return await first, second

    a, b = asyncio.run(go())
    assert a.status == b.status == "completed"
    assert all(env["LITELLM_LOG"] == "ERROR" for env in seen)
    assert os.environ["LITELLM_LOG"] == "DEBUG"


def reply():
    choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def test_a_variable_set_during_a_run_is_removed_for_the_next_and_undone(catalog, runs, monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    inner = []

    async def nested(**kw):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://elsewhere:4318"
        inner.append(await run_agent(AGENT, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                     env="dev", actor="tester", backend=seeing(seen)))
        return reply()

    seen = []
    outer = run(catalog, runs, backend=LiteLLMBackend(nested))
    assert outer.status == "completed", outer.error
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in seen[0]
    assert created(inner[0])["removed"] == ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ  # it was not there before the first run


def test_the_environment_comes_back_after_a_failed_run(catalog, runs, monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "s3cr3t-token")

    async def boom(**kw):
        raise RuntimeError("the model is down")

    out = run(catalog, runs, backend=LiteLLMBackend(boom))
    assert out.status == "failed"
    assert os.environ["LOGFIRE_TOKEN"] == "s3cr3t-token"
    assert telemetry._open == [] and telemetry._saved == {}


def test_two_runs_set_up_alike_are_told_apart(monkeypatch):
    """Two runs with equal values are still two runs: the one that leaves
    first must not take the other's place."""
    monkeypatch.setenv("LITELLM_LOG", "DEBUG")
    policy = TelemetryPolicy.deny_all()
    a = telemetry.Telemetry(policy, "lenient")
    b = telemetry.Telemetry(policy, "lenient")
    telemetry._enter_environment(a)
    telemetry._enter_environment(b)
    telemetry._leave_environment(b)
    a.recorder = object()
    telemetry._leave_environment(a)
    assert telemetry._open == [] and os.environ["LITELLM_LOG"] == "DEBUG"


def test_providers_are_made_live_even_when_the_sdk_is_disabled(monkeypatch):
    from provrun import otel

    monkeypatch.setattr(telemetry, "_providers", None)
    monkeypatch.setattr(otel, "_tracer", None)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        p = telemetry.providers()
    with p.tracer.get_tracer("t").start_as_current_span("s") as span:
        assert span.is_recording()
    assert os.environ["OTEL_SDK_DISABLED"] == "true"  # held back only while the providers were made
    assert otel._tracer is not None  # provrun's spans come from provrun's provider


def test_a_refusal_that_cannot_be_recorded_warns(monkeypatch):
    class Broken:
        closed = False

        def emit(self, *a, **kw):
            raise RuntimeError("the sink is gone")

    t = telemetry.Telemetry(TelemetryPolicy.deny_all(), "strict", recorder=Broken())
    with pytest.warns(UserWarning, match="not recorded: the sink is gone"):
        t.blocked("opentelemetry.span_processor", detail="refused X")


def test_an_interrupt_while_the_environment_changes_puts_it_back(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "s3cr3t-token")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.example:4318")

    class Interrupting(dict):
        """os.environ for the moment the run changes it: the second change is interrupted."""
        def __init__(self, real):
            super().__init__()
            self.real, self.changes = real, 0

        def _change(self):
            self.changes += 1
            if self.changes == 2:
                raise KeyboardInterrupt

        def pop(self, key, *default):
            self._change()
            return self.real.pop(key, *default)

        def __setitem__(self, key, value):
            self._change()
            self.real[key] = value

        def __getitem__(self, key):
            return self.real[key]

        def get(self, key, default=None):
            return self.real.get(key, default)

        def __iter__(self):
            return iter(list(self.real))

        def __contains__(self, key):
            return key in self.real

    real = os.environ
    t = telemetry.Telemetry(TelemetryPolicy.deny_all(), "lenient")
    monkeypatch.setattr(telemetry.os, "environ", Interrupting(real))
    with pytest.raises(KeyboardInterrupt):
        telemetry._enter_environment(t)
    monkeypatch.setattr(telemetry.os, "environ", real)
    assert "LOGFIRE_TOKEN" not in real  # the first change went through
    telemetry._leave_environment(t)
    assert real["LOGFIRE_TOKEN"] == "s3cr3t-token"
    assert real["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector.example:4318"
    assert telemetry._open == [] and telemetry._saved == {}


def test_a_variable_one_run_sets_goes_when_it_ends(catalog, runs, monkeypatch):
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "LOGFIRE_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    async def sets(**kw):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://mid:4318"
        os.environ["LOGFIRE_TOKEN"] = "midtok"
        return reply()

    assert run(catalog, runs, backend=LiteLLMBackend(sets)).status == "completed"
    assert not {"OTEL_EXPORTER_OTLP_ENDPOINT", "LOGFIRE_TOKEN"} & os.environ.keys()
