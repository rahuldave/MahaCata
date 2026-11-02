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


def watched():
    """The variables the tests set or a run sets, as the environment holds
    them now. Only these are copied, so that a failed test prints no other."""
    names = {*NOISY, *telemetry.SETTINGS, *URLS}
    return {k: os.environ[k] for k in sorted(names) if k in os.environ}


def seeing(seen):
    """A LiteLLM backend whose model call keeps a copy of the watched variables."""
    async def acompletion(**kw):
        seen.append(watched())
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


URLS = {"MLFLOW_TRACKING_URI": "postgresql://mlflow:pa55word@db.example:5432/mlflow?sslkey=k3y",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://user:pw@collector.example/v1/traces?token=t0ken#frag",
        "LITELLM_MASTER_KEY": "sk-m4ster"}


def test_a_run_keeps_the_variables_and_records_them(catalog, runs, monkeypatch):
    for k, v in NOISY.items():
        monkeypatch.setenv(k, v)
    seen = []
    out = run(catalog, runs, backend=seeing(seen))
    assert out.status == "completed", out.error
    [env] = seen
    for k, v in telemetry.SETTINGS.items():
        assert env[k] == v
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SDK_DISABLED",
              "OTEL_TRACES_EXPORTER", "LOGFIRE_TOKEN", "ANTHROPIC_LOG"):
        assert env[k] == NOISY[k]  # kept as the user set it
    assert {k: os.environ.get(k) for k in NOISY} == NOISY  # the settings put back after the run
    record = created(out)
    assert record["policy"] == TelemetryPolicy.deny_all().digest()
    assert record["variables"] == {
        "LOGFIRE_TOKEN": None, "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318",
        "OTEL_EXPORTER_OTLP_HEADERS": None, "OTEL_SDK_DISABLED": None, "OTEL_TRACES_EXPORTER": None}
    assert record["overridden"] == {"LITELLM_LOG": "DEBUG", "LITELLM_MODE": "DEV"}
    text = (out.folder / "evidence" / "events.jsonl").read_text()
    assert "s3cr3t" not in text  # a token or a header by its name only


def test_a_place_is_recorded_without_its_credentials(catalog, runs, monkeypatch):
    for k, v in URLS.items():
        monkeypatch.setenv(k, v)
    out = run(catalog, runs)
    assert created(out)["variables"] == {
        "LITELLM_MASTER_KEY": None,
        "MLFLOW_TRACKING_URI": "postgresql://db.example:5432",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://collector.example"}
    text = (out.folder / "evidence" / "events.jsonl").read_text()
    assert not [secret for secret in ("pa55word", "k3y", "pw@", "t0ken", "frag", "m4ster") if secret in text]


@pytest.mark.parametrize("value, recorded", [
    ("sqlite:////var/data/mlflow.db", "sqlite:<path not recorded>"),
    ("file:///home/someone/mlruns", "file:<path not recorded>"),
    ("databricks", "databricks"),
    ("databricks-uc", "databricks-uc"),
    ("http://[::1]:4318/v1/traces", "http://[::1]:4318"),
    ("http://h:notaport/x", "<not recorded: not a valid URL>"),
    ("https://h.example?key=x", "https://h.example"),
    # The review's forms: each would keep a secret.
    ("user:secretpw@collector:4317", "<not recorded: not a URL>"),
    ("tok123@collector:4317", "<not recorded: not a URL>"),
    ("//u:pw@host:4317/x", "<not recorded: not a URL>"),
    ("dns:///u:pw@h", "<not recorded: not a URL>"),
    ("https://hooks.example/services/T00/B00/XXXXSECRET", "https://hooks.example"),
    ("https://h.example/p;token=abc", "https://h.example"),
    ("http://user:p/ss@host:4318", "<not recorded: not a valid URL>"),
    ("sk-ant-abc123", "<not recorded: not a URL>"),
    # The second review's forms.
    ("http://alice:1234?@mlflow.internal", "<not recorded: not a URL>"),
    ("http://alice:99#x@host/", "<not recorded: not a URL>"),
    ("alice:hunter2", "<not recorded: not a URL>"),
    ("localhost:5000", "<not recorded: not a URL>"),
])
def test_a_value_keeps_its_place_and_loses_its_secrets(value, recorded):
    assert telemetry.variables({"MLFLOW_TRACKING_URI": value}) == {"MLFLOW_TRACKING_URI": recorded}


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
        seen.append(watched())
        await gate.wait()
        return reply()

    async def quick(**kw):
        seen.append(watched())
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


def test_a_variable_set_during_a_run_is_the_program_s_and_stays(catalog, runs, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://before:4318")
    inner = []

    async def nested(**kw):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://elsewhere:4318"
        inner.append(await run_agent(AGENT, catalog=catalog, runs_root=runs, org="acme", domain="benefits",
                                     env="dev", actor="tester", backend=seeing([])))
        return reply()

    outer = run(catalog, runs, backend=LiteLLMBackend(nested))
    assert outer.status == "completed", outer.error
    assert created(outer)["variables"] == {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://before:4318"}
    assert created(inner[0])["variables"] == {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://elsewhere:4318"}
    assert os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://elsewhere:4318"


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
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "False")
    monkeypatch.setenv("LITELLM_MODE", "DEV")

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
    assert real.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"  # the first change went through
    telemetry._leave_environment(t)
    assert real.get("LITELLM_LOCAL_MODEL_COST_MAP") == "False" and real.get("LITELLM_MODE") == "DEV"
    assert telemetry._open == [] and telemetry._saved == {}


def test_a_setting_changed_during_a_run_is_put_back(catalog, runs, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG", "DEBUG")

    async def changes(**kw):
        os.environ["LITELLM_LOG"] = "INFO"
        return reply()

    assert run(catalog, runs, backend=LiteLLMBackend(changes)).status == "completed"
    assert os.environ.get("LITELLM_LOG") == "DEBUG"


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


# ---- what cannot be turned off: Logfire, an auto-configuration, patches -------

def problems_in(out):
    return [e.data["problem"] for e in read_events(out.folder / "evidence") if e.type == "telemetry_problem"]


@pytest.fixture
def logfire_config(monkeypatch):
    """Logfire's global configuration, as logfire.configure() leaves it, set
    by the test and put back after it."""
    config = pytest.importorskip("logfire._internal.config").GLOBAL_CONFIG

    def configure():
        monkeypatch.setattr(config, "_initialized", True)

    return configure


def test_a_configured_logfire_fails_a_strict_run_and_is_recorded_in_a_lenient_one(catalog, runs, logfire_config):
    logfire_config()
    with pytest.raises(RunSettingError, match="logfire.configure"):
        run(catalog, runs, env="prod")
    with pytest.warns(UserWarning, match="logfire.configure"):
        out = run(catalog, runs, env="dev")
    assert any("logfire.configure" in p for p in created(out)["problems"])


def test_an_auto_configuration_is_a_problem(catalog, runs, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "opentelemetry.distro", types.ModuleType("opentelemetry.distro"))
    with pytest.raises(RunSettingError, match="opentelemetry.distro is loaded"):
        run(catalog, runs, env="prod")


def test_logfire_configured_during_a_run(catalog, runs, logfire_config):
    async def configures(**kw):
        logfire_config()
        return reply()

    with pytest.warns(UserWarning, match="logfire.configure"):
        out = run(catalog, runs, env="dev", backend=LiteLLMBackend(configures))
    assert out.status == "completed"
    assert len(problems_in(out)) == 1 and "logfire.configure" in problems_in(out)[0]  # once, not at each check
    assert verify(out.folder / "evidence") == []


def test_a_strict_run_fails_at_the_check_after_the_call(catalog, runs, logfire_config):
    async def configures(**kw):
        logfire_config()
        return reply()

    out = run(catalog, runs, env="prod", backend=LiteLLMBackend(configures))
    assert out.status == "failed" and "logfire.configure" in str(out.error)
    types = [e.type for e in read_events(out.folder / "evidence")]
    assert "step_failed" in types and types[-1] == "run_failed"
    assert [p for p in problems_in(out) if "logfire.configure" in p]


def test_a_global_provider_replaced_during_a_run(catalog, runs, monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    telemetry.providers()

    async def replaces(**kw):
        monkeypatch.setattr(trace, "_TRACER_PROVIDER", TracerProvider())
        return reply()

    with pytest.warns(UserWarning, match="tracer provider was replaced"):
        out = run(catalog, runs, env="dev", backend=LiteLLMBackend(replaces))
    assert problems_in(out) == ["the global tracer provider was replaced during the run"]


def test_a_function_wrapped_during_a_run_is_found_at_its_end(catalog, runs, monkeypatch):
    import functools

    import httpx

    original = httpx.Client.send

    async def patches(**kw):
        @functools.wraps(original)
        def send(*a, **k):
            return original(*a, **k)

        monkeypatch.setattr(httpx.Client, "send", send)
        return reply()

    with pytest.warns(UserWarning, match="replaced by a wrapper"):
        out = run(catalog, runs, env="dev", backend=LiteLLMBackend(patches))
    assert problems_in(out) == ["httpx.Client.send was replaced by a wrapper during the run"]
    monkeypatch.setattr(httpx.Client, "send", original)
    strict = run(catalog, runs, env="prod", backend=LiteLLMBackend(patches))
    assert strict.status == "failed" and "replaced by a wrapper" in str(strict.error)


def test_a_module_loaded_during_a_run_is_not_a_patch(catalog, runs, monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "httpx._transports.mock", raising=False)

    async def loads(**kw):
        import httpx._transports.mock  # noqa: F401
        return reply()

    out = run(catalog, runs, env="prod", backend=LiteLLMBackend(loads))
    assert out.status == "completed", out.error


def test_an_mlflow_patch_left_on_is_found_as_the_run_starts(catalog, runs, monkeypatch):
    import httpx

    monkeypatch.setattr(httpx.Client, "_gorilla_active_patch_send", object(), raising=False)
    with pytest.raises(RunSettingError, match="MLflow.s patch on httpx.Client.send is still on"):
        run(catalog, runs, env="prod")


# ---- the adversarial review's findings ---------------------------------------

def test_a_strict_problem_is_recorded_when_the_call_fails(catalog, runs, logfire_config):
    async def configures_then_fails(**kw):
        logfire_config()
        raise RuntimeError("model down")

    out = run(catalog, runs, env="prod", backend=LiteLLMBackend(configures_then_fails))
    assert out.status == "failed" and "model down" in str(out.error)
    assert [p for p in problems_in(out) if "logfire.configure" in p]
    assert verify(out.folder / "evidence") == []


def test_a_wrapper_is_found_when_the_call_fails(catalog, runs, monkeypatch):
    import functools

    import httpx

    original = httpx.Client.send

    async def patches_then_fails(**kw):
        @functools.wraps(original)
        def send(*a, **k):
            return original(*a, **k)

        monkeypatch.setattr(httpx.Client, "send", send)
        raise RuntimeError("model down")

    with pytest.warns(UserWarning, match="replaced by a wrapper"):
        out = run(catalog, runs, env="dev", backend=LiteLLMBackend(patches_then_fails))
    assert out.status == "failed" and "model down" in str(out.error)
    assert problems_in(out) == ["httpx.Client.send was replaced by a wrapper during the run"]


def test_a_value_left_by_an_interrupted_end_is_still_the_user_s(catalog, runs, monkeypatch):
    monkeypatch.setenv("LITELLM_MODE", "DEV")
    t = telemetry.Telemetry(TelemetryPolicy.deny_all(), "lenient")
    telemetry._enter_environment(t)
    with telemetry._lock:  # an end interrupted after it put back all but LITELLM_MODE
        telemetry._open.clear()
        for k in [k for k in telemetry._saved if k != "LITELLM_MODE"]:
            v = telemetry._saved.pop(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"
    run(catalog, runs)
    assert os.environ["LITELLM_MODE"] == "DEV"


def test_the_scan_reads_no_member_s_code(monkeypatch):
    import sys
    import types

    calls = []

    class Lazy:
        @property
        def __class__(self):
            calls.append("__class__")
            raise RuntimeError("a lazy load")

        def __getattr__(self, name):
            calls.append(name)
            raise RuntimeError("a lazy load")

    for name in ("httpx._lazy_test", "httpx_plugin"):
        module = types.ModuleType(name)
        module.x = Lazy()
        monkeypatch.setitem(sys.modules, name, module)
    telemetry._scan()
    assert calls == []


@pytest.mark.parametrize("name, watched", [
    ("httpx", True), ("httpx._client", True), ("openai.types", False), ("openai.types.chat", False),
    ("openai.lib.typesafe", True), ("httpx_plugin", False), ("requests_toolbelt", False),
])
def test_the_scan_watches_whole_package_names(name, watched):
    assert telemetry._watched(name) is watched


def test_the_baseline_holds_the_functions_themselves():
    import httpx

    _, found = telemetry._scan()
    assert found["httpx.Client.send"][0] is vars(httpx.Client)["send"]


def test_a_problem_found_after_the_record_is_written_when_the_run_opens():
    class Recorder:
        closed = False

        def __init__(self):
            self.events = []

        def emit(self, type_, step, data):
            self.events.append((type_, data))

    for mode in ("lenient", "strict"):
        t = telemetry.Telemetry(TelemetryPolicy.deny_all(), mode)
        t.found("before")
        t.checked = True  # the start's check, which applied does
        assert t.record()["problems"] == ["before"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            t.found("between")
            recorder = Recorder()
            t.attach(recorder)
        assert recorder.events == [("telemetry_problem", {"problem": "between"})]
        assert t.failed == (["between"] if mode == "strict" else [])


def test_a_problem_never_raises_when_warnings_are_errors():
    class Recorder:
        closed = False
        events = []

        def emit(self, type_, step, data):
            self.events.append(data)

    t = telemetry.Telemetry(TelemetryPolicy.deny_all(), "lenient")
    t.attach(Recorder())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        t.found("a problem")
    assert Recorder.events == [{"problem": "a problem"}]


@pytest.mark.parametrize("key, value, recorded", [
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf", "http/protobuf"),
    ("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc", "grpc"),
    ("OTEL_SERVICE_NAME", "billing_api.v2", "billing_api.v2"),
    ("OTEL_SERVICE_NAME", "a b=c", "<not recorded>"),
])
def test_a_protocol_and_a_service_name_keep_their_value(key, value, recorded):
    assert telemetry.variables({key: value}) == {key: recorded}


def test_a_problem_found_after_the_start_check_still_fails_a_strict_run(catalog, runs, logfire_config, monkeypatch):
    from provrun import runner

    new_run_id = runner.new_run_id

    def racing():  # what another thread's run can do between the start's check and run_created
        logfire_config()
        telemetry.recheck()
        return new_run_id()

    monkeypatch.setattr(runner, "new_run_id", racing)
    out = run(catalog, runs, env="prod")
    assert out.status == "failed" and "logfire.configure" in str(out.error)
    assert [p for p in created(out)["problems"] if "logfire.configure" in p]
    assert verify(out.folder / "evidence") == []


def test_the_scan_goes_past_a_module_s_and_a_metaclass_s_own_lookup(monkeypatch):
    import sys
    import types

    calls = []

    class Module(types.ModuleType):
        def __getattribute__(self, name):
            calls.append(("module", name))
            return super().__getattribute__(name)

    class Meta(type):
        def __getattribute__(cls, name):
            calls.append(("class", name))
            return super().__getattribute__(name)

    module = Module("httpx._probe_test")
    module.Probe = Meta("Probe", (), {"__module__": "httpx._probe_test", "f": lambda self: None})
    monkeypatch.setitem(sys.modules, "httpx._probe_test", module)
    monkeypatch.setitem(sys.modules, 7, module)  # a key that is not a name
    calls.clear()
    _, found = telemetry._scan()
    assert "httpx._probe_test.Probe.f" in found
    assert calls == []


def test_a_failed_end_check_on_a_failed_run_warns(catalog, runs, monkeypatch):
    async def fails(**kw):
        raise RuntimeError("model down")

    def broken(self, *, fail=True):
        raise ValueError("scan broke")

    monkeypatch.setattr(telemetry.Telemetry, "end", broken)
    with pytest.warns(UserWarning, match="the end check failed: scan broke"):
        out = run(catalog, runs, env="dev", backend=LiteLLMBackend(fails))
    assert "model down" in str(out.error)


def test_a_problem_after_the_evidence_closed_warns_in_strict_mode():
    class Closed:
        closed = True

    t = telemetry.Telemetry(TelemetryPolicy.deny_all(), "strict")
    t.attach(Closed())
    with pytest.warns(UserWarning, match="evidence is closed"):
        t.found("late")
