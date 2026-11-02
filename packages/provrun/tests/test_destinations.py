"""Where a run's spans go: the evidence, and the destinations that the policy approves."""
import asyncio
import gzip
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from provevi.telemetry import TelemetryPolicy

from provrun import destinations
from provrun.evidence import read_events, verify
from provrun.runner import run_agent

from .test_telemetry import AGENT, blocked, created, pa_run, reply, run, seeing
from provrun.backends.litellm import LiteLLMBackend


class Collector:
    """A fake OTLP receiver over HTTP. It keeps each request's path, headers
    and spans."""

    def __init__(self):
        self.requests = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                spans = []
                for rs in ExportTraceServiceRequest.FromString(body).resource_spans:
                    for ss in rs.scope_spans:
                        for sp in ss.spans:
                            spans.append({"name": sp.name, "scope": ss.scope.name, "trace_id": sp.trace_id.hex(),
                                          "attributes": {kv.key for kv in sp.attributes},
                                          "events": len(sp.events)})
                collector.requests.append({"path": self.path, "headers": dict(self.headers), "spans": spans})
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def spans(self):
        return [s for r in self.requests for s in r["spans"]]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def real_spans(monkeypatch):
    """provrun's own tracer, which conftest's no_spans takes away."""
    from provrun import otel, telemetry

    monkeypatch.setattr(otel, "_tracer", telemetry.providers().tracer.get_tracer("provrun"))


@pytest.fixture
def collector():
    c = Collector()
    yield c
    c.close()


def policy(collector, receives="names", libraries=None, kind="otlp", path="/v1/traces", extra=None):
    return TelemetryPolicy.from_dict({
        "version": 1,
        "libraries": libraries or {"litellm": {"versions": ">=1", "telemetry": ["collector"]}},
        "destinations": {"collector": {"kind": kind, "receives": receives, "endpoint": collector.url + path},
                         **(extra or {})},
        "hosts": ["127.0.0.1", "logfire.example"],
    })


def events(out, type_):
    return [e for e in read_events(out.folder / "evidence") if e.type == type_]


def test_an_approved_collector_gets_the_run_s_spans_at_its_level(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-test=h3ader")
    out = run(catalog, runs, policy=policy(collector, "names"))
    assert out.status == "completed", out.error
    names = {s["name"] for s in collector.spans}
    assert {"run", "model_called"} <= names
    assert {r["path"] for r in collector.requests} == {"/v1/traces"}
    assert all(r["headers"].get("x-test") == "h3ader" for r in collector.requests)
    assert {s["trace_id"] for s in collector.spans} == {read_events(out.folder / "evidence")[0].data["trace_id"]}
    model = next(s for s in collector.spans if s["name"] == "model_called")
    assert "gen_ai.usage.input_tokens" not in model["attributes"]  # usage is above names
    assert created(out)["destinations"] == [{
        "kind": "otlp", "variable": "OTEL_EXPORTER_OTLP_ENDPOINT", "url": collector.url, "destination": "collector",
        "receives": "names", "sending": True, "note": ""}]
    assert "h3ader" not in (out.folder / "evidence" / "events.jsonl").read_text()


def test_a_destination_at_usage_gets_the_token_counts(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", collector.url + "/v1/traces")
    run(catalog, runs, policy=policy(collector, "usage"))
    model = next(s for s in collector.spans if s["name"] == "model_called")
    assert "gen_ai.usage.input_tokens" in model["attributes"]


def test_a_place_the_policy_does_not_approve_gets_nothing(catalog, runs, collector, monkeypatch):
    other = Collector()
    try:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", other.url)
        out = run(catalog, runs, policy=policy(collector))
    finally:
        other.close()
    assert other.requests == [] and collector.requests == []
    [b] = blocked(out, "destination")
    assert b["detail"].startswith("OTEL_EXPORTER_OTLP_ENDPOINT: matches no otlp destination")
    assert created(out)["destinations"][0]["destination"] is None
    assert verify(out.folder / "evidence") == []


@pytest.mark.parametrize("name, value", [("OTEL_SDK_DISABLED", "true"), ("OTEL_TRACES_EXPORTER", "none")])
def test_the_user_s_off_switch_sends_nothing(catalog, runs, collector, monkeypatch, name, value):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv(name, value)
    out = run(catalog, runs, policy=policy(collector))
    assert collector.requests == []
    assert created(out)["destinations"][0]["sending"] is False


def test_the_proxy_variables_do_not_redirect_the_spans(catalog, runs, collector, monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://user:pr0xy@127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    out = run(catalog, runs, policy=policy(collector))
    assert collector.spans
    assert created(out)["redirects"] == ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "http_proxy"]
    assert "pr0xy" not in (out.folder / "evidence" / "events.jsonl").read_text()


def test_logfire_gets_the_spans_with_its_token_only(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_us_t0ken")
    monkeypatch.setenv("LOGFIRE_BASE_URL", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-test=otlp-only")
    lib = {"litellm": {"versions": ">=1", "telemetry": ["collector"]}}
    out = run(catalog, runs, policy=policy(collector, libraries=lib, kind="logfire", path=""))
    assert collector.spans and {r["path"] for r in collector.requests} == {"/v1/traces"}
    assert all(r["headers"].get("Authorization") == "pylf_v1_us_t0ken" for r in collector.requests)
    assert all("x-test" not in r["headers"] for r in collector.requests)
    assert created(out)["destinations"][0]["kind"] == "logfire"
    assert "t0ken" not in (out.folder / "evidence" / "events.jsonl").read_text()


def test_a_logfire_token_of_an_unknown_region_sends_nothing(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_mars_t0ken")
    out = run(catalog, runs, policy=policy(collector, kind="logfire", path=""))
    assert collector.requests == []
    assert "region that provrun does not know" in blocked(out, "destination")[0]["detail"]


def test_a_protocol_with_no_exporter_sends_nothing_and_warns(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/json")
    with pytest.warns(UserWarning, match="no http/json exporter"):
        out = run(catalog, runs, policy=policy(collector))
    assert collector.requests == []
    assert created(out)["destinations"][0]["sending"] is False


def test_a_collector_that_is_down_does_not_fail_the_run(catalog, runs, collector, monkeypatch):
    url = collector.url
    collector.close()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    monkeypatch.setattr(destinations, "FLUSH_MILLIS", 2000)
    with pytest.warns(UserWarning, match="spans were not sent"):
        out = run(catalog, runs, env="prod", policy=policy(collector))
    assert out.status == "completed", out.error


def test_a_span_of_a_library_the_policy_does_not_approve_is_refused(catalog, runs, collector, monkeypatch):
    from opentelemetry import trace

    async def rogue(**kw):
        tracer = trace.get_tracer("rogue-lib")
        for _ in range(2):
            with tracer.start_as_current_span("send everything"):
                pass
        return reply()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    out = run(catalog, runs, backend=LiteLLMBackend(rogue), policy=policy(collector))
    assert "send everything" not in {s["name"] for s in collector.spans}
    [b] = blocked(out, "opentelemetry.span")
    assert "rogue-lib" in b["detail"]
    assert events(out, "span_recorded") == []


def test_an_approved_library_s_spans_are_evidence_and_go_to_its_destinations(catalog, runs, collector,
                                                                              monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    lib = {"pydantic-ai": {"versions": ">=0", "telemetry": ["collector"]}}
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    out = pa_run(catalog, runs, model, policy=policy(collector, "bodies", libraries=lib))
    assert out.status == "completed", out.error
    recorded = events(out, "span_recorded")
    assert recorded and {e.data["library"] for e in recorded} == {"pydantic-ai"}
    assert any(e.payload for e in recorded)  # the prompt and reply are bodies, kept apart
    assert "pydantic-ai" in {s["scope"] for s in collector.spans}
    assert verify(out.folder / "evidence") == []


def test_a_library_s_spans_go_only_to_the_destinations_it_may_feed(catalog, runs, collector, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    lib = {"pydantic-ai": {"versions": ">=0", "telemetry": ["elsewhere"]}}
    extra = {"elsewhere": {"kind": "logfire", "receives": "names", "endpoint": "https://logfire.example"}}
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("done")]))
    out = pa_run(catalog, runs, model, policy=policy(collector, libraries=lib, extra=extra))
    assert events(out, "span_recorded")
    scopes = {s["scope"] for s in collector.spans}
    assert scopes == {"provrun"}


def test_overlapping_runs_send_only_their_own_spans(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)

    async def both():
        kw = dict(catalog=catalog, runs_root=runs, org="acme", domain="benefits", env="dev", actor="tester")
        return await asyncio.gather(
            run_agent(AGENT, backend=seeing([]), policy=policy(collector), **kw),
            run_agent(AGENT, backend=seeing([]), policy=TelemetryPolicy.deny_all(), **kw))

    approved, denied = asyncio.run(both())
    trace_of = {o.run_id: read_events(o.folder / "evidence")[0].data["trace_id"] for o in (approved, denied)}
    assert {s["trace_id"] for s in collector.spans} == {trace_of[approved.run_id]}
    assert blocked(denied, "destination")




def test_the_traces_headers_win_over_the_general_ones(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-test=general")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x-test=traces")
    run(catalog, runs, policy=policy(collector))
    assert {r["headers"].get("x-test") for r in collector.requests} == {"traces"}


def test_a_redirect_from_the_collector_is_not_followed(catalog, runs, collector, monkeypatch):
    other = Collector()

    def moved(handler):
        handler.send_response(307)
        handler.send_header("Location", other.url + "/v1/traces")
        handler.end_headers()

    monkeypatch.setattr(collector.server.RequestHandlerClass, "do_POST", moved)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    try:
        with pytest.warns(UserWarning, match="HTTP 307"):
            run(catalog, runs, policy=policy(collector))
        assert other.requests == []
    finally:
        other.close()


def test_grpc_without_its_exporter_sends_nothing(catalog, runs, collector, monkeypatch):
    try:
        import opentelemetry.exporter.otlp.proto.grpc  # noqa: F401
        pytest.skip("the gRPC exporter is installed")
    except ImportError:
        pass
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    with pytest.warns(UserWarning, match="install provrun.otlp-grpc"):
        out = run(catalog, runs, policy=policy(collector, path=""))
    assert created(out)["destinations"][0]["sending"] is False


def grpc_channels(monkeypatch):
    """The insecure channels the gRPC exporter opens, with their options."""
    module = pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc.exporter")
    calls = []
    real = module.insecure_channel

    def spy(target, options=None, *a, **k):
        calls.append(dict(options or ()))
        return real(target, options, *a, **k)

    monkeypatch.setattr(module, "insecure_channel", spy)  # the module imported it by name
    return calls


def test_grpc_turns_off_the_proxy(monkeypatch):
    calls = grpc_channels(monkeypatch)
    destinations._otlp_exporter("grpc", "http://127.0.0.1:4317", {}).shutdown()
    assert calls and calls[0].get("grpc.enable_http_proxy") == 0


def test_grpc_takes_an_upper_case_http_scheme_as_insecure(monkeypatch):
    calls = grpc_channels(monkeypatch)
    destinations._otlp_exporter("grpc", "HTTP://127.0.0.1:4317", {}).shutdown()
    assert calls


# ---- what the review found -------------------------------------------------------

def test_warnings_as_errors_do_not_break_the_run_s_end(catalog, runs, collector, monkeypatch):
    import os
    import warnings

    url = collector.url
    collector.close()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")
    monkeypatch.delenv("LOGFIRE_SEND_TO_LOGFIRE", raising=False)
    monkeypatch.setattr(destinations, "FLUSH_MILLIS", 2000)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        out = run(catalog, runs, policy=policy(collector))
    assert out.status == "completed", out.error
    assert "LOGFIRE_SEND_TO_LOGFIRE" not in os.environ  # the run's settings went away


def test_a_failing_destination_never_raises_into_the_span_s_end(catalog, runs, collector, monkeypatch):
    def broken(self, span, own):
        raise RuntimeError("boom")

    monkeypatch.setattr(destinations.Routes, "on_end", broken)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    with pytest.warns(UserWarning, match="was not kept or sent: RuntimeError"):
        out = run(catalog, runs, policy=policy(collector))
    assert out.status == "completed", out.error


def test_a_hanging_collector_holds_the_run_s_end_for_the_flush_time_only(catalog, runs, collector, monkeypatch):
    import time

    release = threading.Event()

    def hang(handler):
        release.wait(30)

    monkeypatch.setattr(collector.server.RequestHandlerClass, "do_POST", hang)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "30000")
    monkeypatch.setattr(destinations, "FLUSH_MILLIS", 1000)
    started = time.monotonic()
    try:
        with pytest.warns(UserWarning, match="spans were not sent"):
            out = run(catalog, runs, policy=policy(collector))
    finally:
        release.set()
    assert out.status == "completed", out.error
    assert time.monotonic() - started < 6


def test_the_batch_variables_do_not_change_what_is_sent(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    run(catalog, runs, policy=policy(collector))
    assert {"run", "model_called"} <= {s["name"] for s in collector.spans}


def test_a_tracer_that_calls_itself_provrun_is_still_a_library(catalog, runs, collector, monkeypatch):
    from opentelemetry import trace

    async def impostor(**kw):
        with trace.get_tracer("provrun").start_as_current_span("posing"):
            pass
        return reply()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    out = run(catalog, runs, backend=LiteLLMBackend(impostor), policy=policy(collector))
    assert "posing" not in {s["name"] for s in collector.spans}
    assert blocked(out, "opentelemetry.span")


def test_the_user_s_logfire_off_switch_is_kept(catalog, runs, collector, monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_us_t0ken")
    monkeypatch.setenv("LOGFIRE_BASE_URL", collector.url)
    monkeypatch.setenv("LOGFIRE_SEND_TO_LOGFIRE", "false")
    out = run(catalog, runs, policy=policy(collector, kind="logfire", path=""))
    assert collector.requests == []
    assert "LOGFIRE_SEND_TO_LOGFIRE=false" in created(out)["destinations"][0]["note"]


def test_a_namespace_package_s_scope_names_its_own_distribution():
    assert destinations._library("opentelemetry.exporter.otlp.proto.common.trace_encoder") == \
        "opentelemetry-exporter-otlp-proto-common"
    assert destinations._library("opentelemetry.sdk.trace") == "opentelemetry-sdk"


def test_a_header_variable_cannot_set_the_host_or_the_framing():
    got = destinations._headers("Host=evil.example,Content-Length=0,Transfer-Encoding=chunked,x-ok=1,bad")
    assert got == {"x-ok": "1"}


def _span(name, attributes=None, link_attributes=None, scope_attributes=None):
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, SpanContext, Status, StatusCode

    ctx = SpanContext(trace_id=1, span_id=2, is_remote=False)
    return ReadableSpan(name=name, context=ctx, attributes=attributes or {},
                        links=[Link(SpanContext(trace_id=3, span_id=4, is_remote=False), link_attributes or {})],
                        status=Status(StatusCode.ERROR, "the prompt was: secret"), start_time=1, end_time=2,
                        instrumentation_scope=InstrumentationScope("lib", "1.0", None, scope_attributes or {}))


def test_below_bodies_links_scope_status_and_name_are_cut():
    usage = {"gen_ai.usage.input_tokens": 3}
    span = _span("tell the model the secret prompt", usage, link_attributes=usage, scope_attributes={"x": "secret"})
    cut = destinations._cut(span, "names")
    assert cut.name == "lib"
    assert dict(cut.links[0].attributes) == {}
    assert not cut.instrumentation_scope.attributes
    assert cut.status.description is None and cut.events == ()
    assert destinations._cut(span, "bodies") is span


def test_a_span_name_that_may_hold_text_goes_to_the_payload():
    kept = []

    class Recorder:
        closed = False

        def emit(self, type_, kind, data, payload):
            kept.append((data, payload))

    routes = destinations.Routes(telemetry=type("T", (), {"recorder": Recorder()})())
    assert routes._record(_span("tell the model the secret prompt"), "lib")
    [(data, payload)] = kept
    assert data["name"] == "<in the payload>"
    assert payload["attributes"]["span.name"] == "tell the model the secret prompt"
    assert payload["attributes"]["status.description"] == "the prompt was: secret"
    assert "secret" not in str(data)


# ---- what the second review found -----------------------------------------------

def test_an_exporter_that_cannot_be_built_does_not_fail_the_run(catalog, runs, collector, monkeypatch):
    def old(*a, **k):
        raise TypeError("unexpected keyword argument 'channel_options'")

    monkeypatch.setattr(destinations, "_otlp_exporter", old)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.url)
    with pytest.warns(UserWarning, match="could not be built"):
        out = run(catalog, runs, policy=policy(collector))
    assert out.status == "completed", out.error
    assert created(out)["destinations"][0]["sending"] is False


def test_grpc_never_reads_the_headers_variable_itself():
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc")
    environ = {"OTEL_EXPORTER_OTLP_HEADERS": "host=evil.example"}
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, environ):
        exporter = destinations._otlp_exporter("grpc", "http://127.0.0.1:4317", environ)
    try:
        assert exporter._headers == ()
    finally:
        exporter.shutdown()


def test_a_span_that_ends_as_the_run_closes_is_dropped_quietly(collector, recwarn):
    class Telemetry:
        policy = policy(collector)
        recorder = None

    routes = destinations.Routes(Telemetry())
    route = destinations.Route("otlp", "OTEL_EXPORTER_OTLP_ENDPOINT", collector.url,
                               destination=Telemetry.policy.destinations["collector"])
    route.processor, route.sent = destinations._processor(destinations._HttpExporter(collector.url, {}))
    routes.routes.append(route)
    routes.close()
    routes.on_end(_span("late"), own=True)  # close took the processor away
    assert route.handed == 0 and not recwarn.list


def test_the_run_s_end_waits_once_for_all_places(catalog, runs, monkeypatch):
    import time

    release = threading.Event()
    hanging = [Collector(), Collector()]
    for c in hanging:
        monkeypatch.setattr(c.server.RequestHandlerClass, "do_POST", lambda h: release.wait(30))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", hanging[0].url)
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_us_t0ken")
    monkeypatch.setenv("LOGFIRE_BASE_URL", hanging[1].url)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "30000")
    monkeypatch.setattr(destinations, "FLUSH_MILLIS", 1000)
    both = TelemetryPolicy.from_dict({
        "version": 1, "libraries": {},
        "destinations": {"c": {"kind": "otlp", "receives": "names", "endpoint": hanging[0].url + "/v1/traces"},
                         "l": {"kind": "logfire", "receives": "names", "endpoint": hanging[1].url}},
        "hosts": ["127.0.0.1"]})
    started = time.monotonic()
    try:
        with pytest.warns(UserWarning, match="spans were not sent"):
            run(catalog, runs, policy=both)
    finally:
        release.set()
        for c in hanging:
            c.close()
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("name", ["GET /users/alice", "mail alice@example.com", "set key=value"])
def test_a_path_or_an_address_is_not_a_span_name(name):
    assert destinations._cut(_span(name), "names").name == "lib"


def test_a_model_name_is_a_span_name():
    assert destinations._cut(_span("chat gpt-4o"), "names").name == "chat gpt-4o"
