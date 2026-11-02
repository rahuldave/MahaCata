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
    assert blocked[0].data["library"] == "opentelemetry-sdk" and "SimpleSpanProcessor" in blocked[0].data["detail"]
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
    events = read_events(out.folder / "evidence")
    first, last = events[0], events[-1]
    model = next(e for e in events if e.type == "model_called")
    # One trace for the run: its root span's IDs open and end the run, and
    # each top-level step is the root's child.
    assert first.type == "run_created" and last.type == "run_completed"
    assert len(first.data["span_id"]) == 16 and len(first.data["trace_id"]) == 32
    assert {k: last.data[k] for k in ("trace_id", "span_id")} == {k: first.data[k] for k in ("trace_id", "span_id")}
    assert model.data["trace_id"] == first.data["trace_id"]
    assert model.data["parent_span_id"] == first.data["span_id"] != model.data["span_id"]


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


# ---- the root span ------------------------------------------------------------

def test_a_failed_run_is_one_trace_whose_root_ends_with_an_error(catalog, runs, spans):
    async def acompletion(**kw):
        raise RuntimeError("model down")

    out = run(catalog, runs, backend=LiteLLMBackend(acompletion))
    events = read_events(out.folder / "evidence")
    assert events[-1].type == "run_failed"
    finished = {s.name: s for s in spans.get_finished_spans()}
    root = finished["run"]
    assert root.parent is None and root.status.status_code.name == "ERROR"
    assert root.attributes["gen_ai.agent.name"] == "q" and root.attributes["provrun.run_id"] == out.run_id
    assert events[0].data["span_id"] == events[-1].data["span_id"] == format(root.context.span_id, "016x")
    failed = next(e for e in events if e.type == "step_failed")
    assert failed.data["parent_span_id"] == events[0].data["span_id"]
    assert root.start_time <= finished["model_called"].start_time
    assert root.end_time >= finished["model_called"].end_time


def test_a_run_inside_a_caller_s_span_starts_its_own_trace(catalog, runs, spans):
    from provrun import otel

    with otel._tracer.start_as_current_span("caller") as caller:
        out = run(catalog, runs)
    first = read_events(out.folder / "evidence")[0]
    assert first.data["trace_id"] != format(caller.get_span_context().trace_id, "032x")


# ---- the libraries' own telemetry -------------------------------------------

def blocked(out, path=None):
    return [e.data for e in read_events(out.folder / "evidence")
            if e.type == "telemetry_blocked" and (path is None or e.data["path"] == path)]


def test_litellm_callbacks_go_for_the_run_and_come_back(catalog, runs):
    litellm = pytest.importorskip("litellm")

    def before(*a, **k):
        pass

    def given(*a, **k):
        pass

    seen = []

    async def acompletion(**kw):
        seen.append({n: list(getattr(litellm, n)) for n in telemetry.LITELLM_LISTS})
        # A callbacks= argument on one call stays in LiteLLM's global lists.
        return await litellm.acompletion(**kw, mock_response="ok", callbacks=[given])

    litellm.success_callback.append(before)
    try:
        out = run(catalog, runs, backend=LiteLLMBackend(acompletion))
        assert out.status == "completed"
        assert all(lists == [] for s in seen for lists in s.values()), seen
        found = blocked(out, "litellm.callbacks")
        assert any("before" in b["detail"] and "litellm.success_callback" in b["detail"] for b in found)
        assert any("given" in b["detail"] for b in found)  # removed after the call
        assert all(b["library"] == telemetry._library_of(given) for b in found)
        assert verify(out.folder / "evidence") == []
        assert litellm.success_callback == [before]  # put back; the one the call gave is not
        assert all(given not in getattr(litellm, n) for n in telemetry.LITELLM_LISTS)
    finally:
        for n in telemetry.LITELLM_LISTS:
            getattr(litellm, n).clear()


def test_a_logger_at_debug_is_held_at_warning_and_put_back(catalog, runs):
    import logging

    root, anthropic = logging.getLogger(), logging.getLogger("anthropic")
    levels = root.level, anthropic.level
    root.setLevel(logging.DEBUG)
    anthropic.setLevel(logging.DEBUG)
    seen = []

    async def acompletion(**kw):
        seen.append({n: logging.getLogger(n).getEffectiveLevel() for n in telemetry.LOGGERS})
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    try:
        out = run(catalog, runs, backend=LiteLLMBackend(acompletion))
        assert all(level == logging.WARNING for level in seen[0].values())
        found = {b["detail"].split("'")[1]: b for b in blocked(out, "logging")}
        # A child, such as anthropic._base_client, is quiet once its parent is.
        assert set(found) == {n for n in telemetry.LOGGERS if "." not in n}
        assert found["anthropic"]["library"] == "anthropic" and found["LiteLLM Router"]["library"] == "litellm"
        assert anthropic.level == logging.DEBUG  # put back when the run ends
        assert logging.getLogger("LiteLLM").level == logging.NOTSET
    finally:
        root.setLevel(levels[0])
        anthropic.setLevel(levels[1])


def test_a_logger_already_quiet_is_not_recorded(catalog, runs):
    assert blocked(run(catalog, runs), "logging") == []


def pa_run(catalog, runs, model, **kw):
    from provrun.backends.pydantic_ai import PydanticAIBackend

    agent = AgentSpec(name="pa", backend="pydantic-ai", model="test", prompt="p")
    return asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                 env="dev", actor="tester", backend=PydanticAIBackend(model=model), **kw))


def foreign_settings():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai.models.instrumented import InstrumentationSettings

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return InstrumentationSettings(tracer_provider=provider), exporter


def test_pydantic_ai_instrumentation_is_off_for_a_run(catalog, runs, monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.instrumented import InstrumentedModel

    settings, exporter = foreign_settings()
    monkeypatch.setattr(Agent, "_instrument_default", settings)  # as Agent.instrument_all(settings) sets it
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    out = pa_run(catalog, runs, InstrumentedModel(InstrumentedModel(model, settings), settings))
    assert out.status == "completed", out.error
    assert exporter.get_finished_spans() == ()
    found = blocked(out, "pydantic_ai.instrumented_model")
    assert len(found) == 2 and found[0]["library"] == "pydantic-ai-slim"


def test_an_approved_pydantic_ai_instrumentation_uses_provrun_s_providers():
    from pydantic_ai.models.instrumented import InstrumentationSettings

    approved = TelemetryPolicy.from_dict({
        "version": 1, "libraries": {"pydantic-ai": {"versions": ">=2", "telemetry": ["logfire"]}},
        "destinations": {"logfire": {"kind": "otlp", "receives": "names",
                                     "endpoint": "https://collector.example/v1/traces"}},
        "hosts": ["collector.example"]})
    assert telemetry.pydantic_ai_instrument(TelemetryPolicy.deny_all()) is False
    assert telemetry.pydantic_ai_instrument(TelemetryPolicy.from_dict(POLICY)) is False  # litellm only
    settings = telemetry.pydantic_ai_instrument(approved)
    assert isinstance(settings, InstrumentationSettings) and settings.include_binary_content is False
    with settings.tracer.start_as_current_span("x") as s:
        assert s.resource is telemetry.providers().tracer.resource  # made by provrun's provider


@pytest.mark.parametrize("module, distribution", [
    ("opentelemetry.sdk.trace.export", "opentelemetry-sdk"),
    ("opentelemetry.trace", "opentelemetry-api"),
    ("litellm.integrations.custom_logger", "litellm"),
    ("pydantic_ai.models", "pydantic-ai-slim"),
    ("no_such_module.x", "no_such_module"),
])
def test_a_blocked_library_is_named_by_its_distribution(module, distribution):
    assert telemetry._distribution(module) == distribution


def test_a_program_s_own_script_names_no_library():
    def fn():
        pass

    fn.__module__ = "__main__"
    assert telemetry._library_of(fn) is None and telemetry._library_of(len) is None


def test_mlflow_autolog_and_tracing_go_off_for_a_run(catalog, runs, tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    pytest.importorskip("litellm")
    from mlflow.telemetry.client import get_telemetry_client
    from mlflow.tracing.provider import is_tracing_enabled
    from mlflow.utils.autologging_utils import AUTOLOGGING_INTEGRATIONS

    mlflow.set_tracking_uri(f"sqlite:///{tmp_path}/m.db")
    mlflow.litellm.autolog()
    mlflow.openai.autolog()
    seen = []

    async def acompletion(**kw):
        seen.append(is_tracing_enabled())
        mlflow.litellm.autolog()  # turned on again during the run
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    out = run(catalog, runs, backend=LiteLLMBackend(acompletion))
    assert seen == [False]
    found = [b["detail"] for b in blocked(out, "mlflow.autolog")]
    assert "turned off MLflow's litellm autolog" in found and "turned off MLflow's openai autolog" in found
    assert found.count("turned off MLflow's litellm autolog") == 2  # and again after the call
    assert all(conf.get("disable") is not False for conf in AUTOLOGGING_INTEGRATIONS.values())
    assert not is_tracing_enabled() and get_telemetry_client() is None
    import litellm
    assert all(getattr(litellm, n) == [] for n in telemetry.LITELLM_LISTS)


def test_an_instrumented_model_inside_another_fails_the_run(catalog, runs):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.fallback import FallbackModel
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.instrumented import InstrumentedModel

    settings, exporter = foreign_settings()
    fn = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    out = pa_run(catalog, runs, FallbackModel(InstrumentedModel(fn, settings), fn))
    assert out.status == "failed" and "InstrumentedModel inside another model" in str(out.error)
    assert exporter.get_finished_spans() == ()


def test_an_instrumented_model_s_event_names_its_distribution(catalog, runs):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.models.instrumented import InstrumentedModel

    settings, _ = foreign_settings()
    fn = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    out = pa_run(catalog, runs, InstrumentedModel(fn, settings))
    assert [b["library"] for b in blocked(out, "pydantic_ai.instrumented_model")] == ["pydantic-ai-slim"]


def test_a_blocked_mlflow_does_not_stop_a_run(catalog, runs, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "mlflow", None)  # how a program blocks an import
    assert run(catalog, runs).status == "completed"


def test_an_mlflow_that_cannot_be_turned_off_is_a_problem(catalog, runs, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "mlflow", types.ModuleType("mlflow"))  # old: no mlflow.tracing
    for name in [m for m in sys.modules if m.startswith("mlflow.")]:  # a real MLflow loaded earlier
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mlflow.tracing", None)
    with pytest.raises(RunSettingError, match="cannot turn off MLflow"):
        run(catalog, runs, env="prod")
    with pytest.warns(UserWarning, match="cannot turn off MLflow"):
        out = run(catalog, runs, env="dev")
    assert out.status == "completed"
    assert any("cannot turn off MLflow" in p for p in created(out)["problems"])


def test_the_distribution_that_holds_the_deepest_path_wins(monkeypatch):
    from importlib import metadata

    files = {"pkg-core": ["pkg/__init__.py"], "pkg-extra": ["pkg/__init__.py", "pkg/sub/__init__.py"]}
    monkeypatch.setattr(metadata, "packages_distributions", lambda: {"pkg": ["pkg-core", "pkg-extra"]})
    monkeypatch.setattr(metadata, "files", lambda name: files[name])
    telemetry._distribution.cache_clear()
    try:
        assert telemetry._distribution("pkg.sub.mod") == "pkg-extra"
        assert telemetry._distribution("pkg.other") == "pkg-core"
    finally:
        telemetry._distribution.cache_clear()


@pytest.mark.parametrize("name, receives, content", [("pydantic-ai-slim", "usage", False),
                                                      ("pydantic-ai", "bodies", True)])
def test_approved_pydantic_ai_spans_hold_bodies_only_for_a_destination_that_takes_them(name, receives, content):
    policy = TelemetryPolicy.from_dict({
        "version": 1, "libraries": {name: {"versions": ">=2", "telemetry": ["c"]}},
        "destinations": {"c": {"kind": "otlp", "receives": receives,
                               "endpoint": "https://collector.example/v1/traces"}},
        "hosts": ["collector.example"]})
    assert telemetry.pydantic_ai_instrument(policy).include_content is content


def test_an_mlflow_without_usage_telemetry_is_no_problem(monkeypatch):
    import sys
    import types

    # MLflow 2 has autolog and tracing, but no mlflow.telemetry.
    for name in [m for m in sys.modules if m.startswith("mlflow.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "mlflow", types.ModuleType("mlflow"))
    monkeypatch.setitem(sys.modules, "mlflow.telemetry", None)
    assert telemetry._mlflow_usage_off() == []


@pytest.mark.parametrize("odd", ["mock", "magicmock", "number", "method", "raises"])
def test_a_model_with_odd_attributes_is_left_as_it_is(odd):
    from unittest import mock

    class Number:
        models = 5

    class Method:
        def models(self):
            return []

    class Raises:
        @property
        def wrapped(self):
            raise RuntimeError("no")

    model = {"mock": mock.Mock(), "magicmock": mock.MagicMock(), "number": Number(), "method": Method(),
             "raises": Raises()}[odd]
    assert telemetry.pydantic_ai_model(model) is model


def test_spans_hold_bodies_only_when_every_destination_takes_them():
    def endpoint(n):
        return {"kind": "otlp", "endpoint": f"https://{n}.example/v1/traces"}

    policy = TelemetryPolicy.from_dict({
        "version": 1, "libraries": {"pydantic-ai": {"versions": ">=2", "telemetry": ["a", "b"]}},
        "destinations": {"a": {**endpoint("a"), "receives": "bodies"}, "b": {**endpoint("b"), "receives": "usage"}},
        "hosts": ["a.example", "b.example"]})
    assert telemetry.pydantic_ai_instrument(policy).include_content is False


def test_an_unwrapped_model_is_recorded_only_in_its_own_run():
    from pydantic_ai.models.instrumented import InstrumentedModel
    from pydantic_ai.models.test import TestModel

    settings, _ = foreign_settings()
    mine = telemetry.Telemetry(TelemetryPolicy.deny_all(), "lenient")
    other = telemetry.Telemetry(TelemetryPolicy.deny_all(), "lenient")
    with mine.applied(), other.applied():
        telemetry.pydantic_ai_model(InstrumentedModel(TestModel(), settings), mine)
        assert [p[0] for p in mine.pending] == ["pydantic_ai.instrumented_model"]
        assert other.pending == []
        mine.pending.clear()


def test_a_broken_install_still_names_the_module(monkeypatch):
    from importlib import metadata

    def broken():
        raise ValueError("bad RECORD")

    monkeypatch.setattr(metadata, "packages_distributions", broken)
    telemetry._distribution.cache_clear()
    try:
        assert telemetry._distribution("somepkg.mod") == "somepkg"
    finally:
        telemetry._distribution.cache_clear()
