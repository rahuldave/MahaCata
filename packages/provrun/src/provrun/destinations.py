"""Where a run's spans go: its evidence, and the destinations that the
operator's policy approves.

A run's spans are provrun's own, one for the run and one for each step, and
those of a library whose instrumentation the policy approves, such as
PydanticAI's. provrun's own spans are in the evidence already: each step's
event holds its span's IDs and data. A library's span is written to the
evidence as a ``span_recorded`` event, its bodies kept as a payload.

The user's variables say where spans may go. Each run reads them as it starts:

- ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` or ``OTEL_EXPORTER_OTLP_ENDPOINT``,
  with its protocol and headers, names an OpenTelemetry collector. provrun
  sends nothing there if ``OTEL_SDK_DISABLED`` is true, or
  ``OTEL_TRACES_EXPORTER`` is none;
- ``LOGFIRE_TOKEN`` names a Logfire project, at the base URL that
  ``LOGFIRE_BASE_URL`` names, or else at the region in the token. provrun
  sends nothing there if the user set ``LOGFIRE_SEND_TO_LOGFIRE`` to false.

A place gets spans only when the policy has a destination of its kind at that
endpoint (``TelemetryPolicy.match_destination``). It then gets provrun's spans,
and those of each library that the policy lets feed it, each cut down to the
level the destination receives. A place with no match gets nothing, and the
run writes a ``telemetry_blocked`` event with the path ``destination``.

provrun's exporters connect straight to the place the policy approved. They
ignore the proxy variables, such as ``HTTPS_PROXY`` and ``grpc_proxy``, so that
no variable sends the spans elsewhere. A run records which of these variables
were set, by name only, because a proxy URL can hold a password.

MLflow is a destination of another kind. ``MLFLOW_TRACKING_URI`` names it,
or, when the variable is unset, a sqlite evidence store's own file. MLflow
gets no span as the run goes. Once the run is sealed, provevi's
``copy_run`` copies it from the store, cut to the level the destination
receives (``Routes.copy``). The copy runs in a process of its own, for
COPY_SECONDS at most. A copy that fails gives a warning, and never fails the
run, whose evidence is closed by then.
"""
from __future__ import annotations

import base64
import functools
import json
import pickle
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from opentelemetry.sdk.trace import SpanProcessor
from provevi import (ProveviError, attributes_for, otlp_traces_target, plain_name, plain_span_name, span_libraries,
                     split_body)

from . import otel

__all__ = ["REDIRECTS", "SPAN", "Route", "Routes", "router"]

#: The event type of a span from an approved library's instrumentation.
SPAN = "span_recorded"

#: The variables that can send a connection to another place than its URL
#: names. provrun's exporters ignore the proxy ones; the Postgres ones matter
#: only for the MLflow destination. A run records each one set, by name.
REDIRECTS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy",
             "no_proxy", "grpc_proxy", "GRPC_PROXY", "no_grpc_proxy", "PGHOST", "PGHOSTADDR", "PGPORT",
             "PGSERVICE", "PGSERVICEFILE")

_LOGFIRE_REGIONS = {"us": "https://logfire-us.pydantic.dev", "eu": "https://logfire-eu.pydantic.dev"}
_LOGFIRE_TOKEN = re.compile(r"^pylf_v[0-9]+_(?P<region>[a-z]+)_")
#: How long a run's end waits, at most, for all its places to get its spans.
FLUSH_MILLIS = 10_000
#: How long a run's end waits, at most, for its copy into MLflow.
COPY_SECONDS = 120
# MLflow's retries of a server that cannot be reached, unless the user set them.
_MLFLOW_HTTP = {"MLFLOW_HTTP_REQUEST_MAX_RETRIES": "2", "MLFLOW_HTTP_REQUEST_TIMEOUT": "20"}
# The batches, fixed here, so that no OTEL_BSP_* variable changes them.
_QUEUE = 2048
_BATCH = 512
_DELAY_MILLIS = 1000
# Headers that a variable may not set: they would send the request to another
# host behind the same address, or break the request itself.
_REFUSED_HEADERS = {"host", "connection", "content-length", "content-type", "transfer-encoding", "te", "upgrade",
                    "keep-alive", "proxy-authorization", "proxy-connection", "trailer"}


def _warn(message: str) -> None:
    from .telemetry import _warn as warn

    warn(message)


@dataclass
class Route:
    """One place a run sends spans to: the policy's destination, the variable
    that named the place, and the processor that sends to it."""

    kind: str
    variable: str
    url: str  # where the variable points, without what can hold a secret
    destination: Any = None  # the policy's Destination, or None
    processor: Any = None  # a BatchSpanProcessor, or None when nothing is sent
    sent: Any = None  # the exporter behind it, which counts what it sent and lost
    handed: int = 0  # the spans handed to the processor
    copying: bool = False  # MLflow's place, which gets a copy of the sealed run
    note: str = ""

    def record(self) -> dict:
        d = self.destination
        return {"kind": self.kind, "variable": self.variable, "url": self.url,
                "destination": d.name if d is not None else None, "receives": d.receives if d is not None else None,
                "sending": self.processor is not None or self.copying,
                "note": self.note}


@dataclass(eq=False)
class Routes:
    """The places one run sends spans to, read from the variables as the run
    starts, and the run's spans as they end."""

    telemetry: Any  # the run's Telemetry
    routes: list[Route] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    trace_id: int | None = None
    refused: set[str] = field(default_factory=set)  # libraries whose spans were refused, each once
    lock: Any = field(default_factory=threading.Lock)  # spans end in many threads

    mlflow: Route | None = None  # the MLflow place, copied to after the seal
    environ: Mapping[str, str] = field(default_factory=dict)  # the user's variables, for the copy

    @classmethod
    def read(cls, telemetry: Any, environ: Mapping[str, str], store: Any = None) -> Routes:
        """The routes of a run under telemetry's policy, from environ, the
        variables as the user set them. store is the run's evidence store,
        None for the file store. A place that matches no destination is a
        telemetry_blocked event."""
        r = cls(telemetry, redirects=sorted(k for k in REDIRECTS if environ.get(k)))
        r._otlp(environ)
        r._logfire(environ)
        r._mlflow(environ, store)
        return r

    def _blocked(self, route: Route) -> None:
        self.routes.append(route)
        self.telemetry.blocked("destination", detail=f"{route.variable}: {route.note}")

    def _sending(self, route: Route, make) -> None:
        try:
            exporter = make()
        except _NoExporter as e:
            route.note = str(e)
            _warn(f"provrun: {route.variable}: {e}")
        except Exception as e:  # a sender that cannot be built never fails the run
            route.note = f"the exporter could not be built ({e.__class__.__name__}); nothing is sent"
            _warn(f"provrun: {route.variable}: {route.note}")
        else:
            route.processor, route.sent = _processor(exporter)
        self.routes.append(route)

    def _otlp(self, environ: Mapping[str, str]) -> None:
        variable = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" if environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                    else "OTEL_EXPORTER_OTLP_ENDPOINT")
        try:
            target = otlp_traces_target(environ)
        except ProveviError as e:
            self._blocked(Route("otlp", variable, _place(environ.get(variable, "")), note=str(e)))
            return
        if target is None:
            return
        protocol, url = target
        route = Route("otlp", variable, _place(url))
        if (environ.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true" or \
                (environ.get("OTEL_TRACES_EXPORTER") or "").strip().lower() == "none":
            route.note = "OTEL_SDK_DISABLED or OTEL_TRACES_EXPORTER turns the OTLP exporter off"
            self.routes.append(route)
            return
        route.destination = self.telemetry.policy.match_destination("otlp", url)
        if route.destination is None:
            route.note = "matches no otlp destination of the policy; nothing is sent"
            self._blocked(route)
            return
        self._sending(route, lambda: _otlp_exporter(protocol, url, environ))

    def _logfire(self, environ: Mapping[str, str]) -> None:
        tokens = [t.strip() for t in (environ.get("LOGFIRE_TOKEN") or "").split(",") if t.strip()]
        off = (environ.get("LOGFIRE_SEND_TO_LOGFIRE") or "").strip().lower() == "false"
        for token in tokens:
            base = environ.get("LOGFIRE_BASE_URL") or ""
            if not base:
                m = _LOGFIRE_TOKEN.match(token)
                base = _LOGFIRE_REGIONS.get(m.group("region") if m else "us", "")
            variable = "LOGFIRE_BASE_URL" if environ.get("LOGFIRE_BASE_URL") else "LOGFIRE_TOKEN"
            if not base:
                self._blocked(Route("logfire", variable, "<not recorded>",
                                    note="the token names a region that provrun does not know; nothing is sent"))
                continue
            route = Route("logfire", variable, _place(base))
            if off:
                route.note = "LOGFIRE_SEND_TO_LOGFIRE=false turns Logfire off"
                self.routes.append(route)
                continue
            route.destination = self.telemetry.policy.match_destination("logfire", base)
            if route.destination is None:
                route.note = "matches no logfire destination of the policy; nothing is sent"
                self._blocked(route)
                continue
            self._sending(route, lambda: _HttpExporter(base.rstrip("/") + "/v1/traces", {"Authorization": token}))

    def _mlflow(self, environ: Mapping[str, str], store: Any) -> None:
        from provevi import FileStore
        from provevi.mlflow_copy import refuse_redirects, tracking_target

        url = environ.get("MLFLOW_TRACKING_URI") or None
        policy = self.telemetry.policy
        # Unset, the variable names no place; a policy's mlflow destination
        # with no endpoint then means the sqlite store's own file.
        if url is None and policy.match_destination("mlflow", None) is None:
            return
        route = Route("mlflow", "MLFLOW_TRACKING_URI", _place(url) if url else "<the store's own file>")
        # The evidence gets provrun's own words: provevi's message names the
        # URL with its path, which can be a local one.
        if url is not None and policy.match_destination("mlflow", url) is None:
            route.note = "matches no mlflow destination of the policy; nothing is copied"
            self._blocked(route)
            return
        try:
            name, _, target = tracking_target(policy, store if store is not None else FileStore(), environ)
        except ProveviError:  # approved, but the file store has no file for MLflow's tables
            route.destination = policy.match_destination("mlflow", url)
            route.note = ("the file store has no file for MLflow's tables; set MLFLOW_TRACKING_URI, or use a "
                          "sqlite store; nothing is copied")
            self.routes.append(route)
            _warn(f"provrun: MLFLOW_TRACKING_URI: {route.note}")
            return
        route.destination = policy.destinations[name]
        try:
            refuse_redirects(target, environ)
        except ProveviError as e:  # the copy would refuse; its message names variables only
            route.note = f"{e}; nothing is copied"
            self._blocked(route)
            return
        route.note = "copied into MLflow after the run is sealed"
        route.copying = True
        self.mlflow = route
        self.environ = dict(environ)
        self.routes.append(route)

    def copy(self, run_id: str, store: Any, folder: Any) -> dict | None:
        """Copy the sealed run into the approved MLflow place, if any, and
        say where it went. The copy runs in a process of its own, with the
        user's variables, for COPY_SECONDS at most. A copy that fails gives a
        warning and a result with its error; it never raises."""
        if self.mlflow is None:
            return None
        route = self.mlflow
        done = {"destination": route.destination.name, "receives": route.destination.receives, "copied": False}
        try:
            got = _copy_in_process(run_id, store, Path(folder), self.telemetry.policy, self.environ)
        except Exception as e:  # a copy never fails the run
            got = {"error": f"{e.__class__.__name__}: {e}"}
        if "error" in got:
            done["error"] = got["error"]
            _warn(f"provrun: MLFLOW_TRACKING_URI: {run_id} was not copied into MLflow: {done['error']}")
            return done
        c = got["copy"]
        done.update(copied=True, **{k: c[k] for k in ("tracking_uri", "experiment_id", "mlflow_run_id",
                                                      "trace_id", "spans")})
        return done

    def record(self) -> dict:
        """What run_created records: each place, and the redirecting variables set."""
        return {"destinations": [r.record() for r in self.routes], "redirects": list(self.redirects)}

    def start(self, trace_id: str | None) -> None:
        """Take the spans of the run's trace from now on."""
        if not trace_id:
            return
        self.trace_id = int(trace_id, 16)
        with _lock:
            _runs[self.trace_id] = self

    def on_end(self, span: Any, own: bool) -> None:
        policy = self.telemetry.policy
        allowed: set[str] = set()
        if not own:
            library = getattr(span.instrumentation_scope, "name", None) or "<unnamed>"
            names = _names(library)
            if not any(policy.library(name) for name in names):
                with self.lock:
                    first = library not in self.refused
                    self.refused.add(library)
                if first:
                    self.telemetry.blocked("opentelemetry.span", library=(names or [library])[0],
                                           detail=f"a span from {library}, which the policy does not approve")
                return
            # The evidence holds at least what any place gets: a span that
            # the evidence can no longer keep goes nowhere.
            if not self._record(span, library):
                return
            allowed = {d.name for name in names for d in policy.destinations_for(name)}
        for route in self.routes:
            with self.lock:
                processor = route.processor  # close may take it away at any time
                if processor is None or (not own and route.destination.name not in allowed):
                    continue
                route.handed += 1
            processor.on_end(_cut(span, route.destination.receives))

    def _record(self, span: Any, library: str) -> bool:
        recorder = self.telemetry.recorder
        if recorder is None or recorder.closed:
            return False
        kept, body = split_body(dict(span.attributes or {}))
        events = [{"name": e.name, "timestamp": e.timestamp, "attributes": dict(e.attributes or {})}
                  for e in span.events]
        links = [{"trace_id": format(link.context.trace_id, "032x"), "span_id": format(link.context.span_id, "016x"),
                  "attributes": dict(link.attributes or {})} for link in span.links]
        name = span.name
        if not plain_span_name(name):
            body["span.name"], name = name, "<in the payload>"
        if span.status.description:
            body["status.description"] = span.status.description
        ctx, parent = span.context, span.parent
        data = {"name": name, "library": library, "trace_id": format(ctx.trace_id, "032x"),
                "span_id": format(ctx.span_id, "016x"),
                "parent_span_id": format(parent.span_id, "016x") if parent is not None else None,
                "start_time": span.start_time, "end_time": span.end_time,
                "status": span.status.status_code.name, "attributes": kept}
        payload = {"attributes": body, "events": events, "links": links} if body or events or links else None
        recorder.emit(SPAN, "telemetry", data=data, payload=_jsonable(payload))
        return True

    def close(self) -> None:
        """Stop taking the run's spans, and send what is left, to all places at
        once, for FLUSH_MILLIS at most. A place that did not get every span
        gives a warning; the run's evidence is closed by then."""
        with _lock:
            if self.trace_id is not None and _runs.get(self.trace_id) is self:
                del _runs[self.trace_id]
        with self.lock:
            sending = [r for r in self.routes if r.processor is not None]
            processors = {id(r): r.processor for r in sending}
            for route in sending:
                route.processor = None  # no span is handed on from now
        deadline = time.monotonic() + FLUSH_MILLIS / 1000
        for route in sending:
            route.sent.deadline = deadline
        threads = [threading.Thread(target=_shut, args=(route, processors[id(route)]), daemon=True)
                   for route in sending]
        for t in threads:
            t.start()
        last = deadline + 1  # one wait for all, however many places
        for t in threads:
            t.join(max(0.0, last - time.monotonic()))
        for route in sending:
            lost = route.handed - route.sent.exported
            if lost > 0:
                _warn(f"provrun: {route.variable}: {lost} of {route.handed} spans were not sent to {route.url}"
                      f" ({route.sent.error or 'not sent in time'})")


def _shut(route: Route, processor: Any) -> None:
    try:
        processor.shutdown()  # sends what is queued; each export stops at the deadline
    except Exception as e:
        route.sent.error = route.sent.error or f"{e.__class__.__name__}: {e}"


# ---- the router on provrun's tracer provider ---------------------------------

_lock = threading.Lock()
_runs: dict[int, Routes] = {}  # each open run's routes, by its trace ID


class _Router(SpanProcessor):
    """A span processor that hands each span to the run whose trace it is in.
    A span in no run's trace goes nowhere. It never raises into the code that
    ends the span."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        pass

    def on_end(self, span: Any) -> None:
        try:
            span_id = span.context.span_id
            own = span_id in otel.OURS
            otel.OURS.discard(span_id)
            with _lock:
                routes = _runs.get(span.context.trace_id)
            if routes is not None:
                routes.on_end(span, own)
        except Exception as e:  # a destination never breaks the run, nor the library's call
            _warn(f"provrun: a span was not kept or sent: {e.__class__.__name__}: {e}")

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


router = _Router()


# ---- helpers -------------------------------------------------------------------

class _NoExporter(Exception):
    pass


def _copy_in_process(run_id: str, store: Any, folder: Path, policy: Any, environ: Mapping[str, str]) -> dict:
    from provevi import FileStore, SqliteStore

    if isinstance(store, SqliteStore):
        where = {"kind": "sqlite", "path": str(Path(store.path).resolve())}
    elif store is None or isinstance(store, FileStore):
        # The runs root of this run, so that the copy's lock file is beside its runs.
        root = store.root if store is not None and store.root is not None else folder.parent.parent
        where = {"kind": "file", "root": str(Path(root).resolve())}
    else:
        return {"error": f"the copy reads only the file and sqlite stores, not {store.__class__.__name__}"}
    # The policy as this run holds it: a run file's narrowing may not pass
    # from_dict's checks again. The child is provrun's own, so pickle is safe.
    folder = folder.resolve()  # the child works in another folder, so every path is absolute
    request = {"store": where, "run_id": run_id, "folder": str(folder),
               "policy": base64.b64encode(pickle.dumps(policy)).decode()}
    env = {**_MLFLOW_HTTP, **environ}
    try:
        # -P keeps the working folder off sys.path, so a file there, such as
        # an mlflow.py, cannot stand in for a library. The child works in the
        # evidence folder, which holds only what provrun wrote.
        done = subprocess.run([sys.executable, "-P", "-m", "provrun._copy"], input=json.dumps(request), env=env,
                              cwd=folder, capture_output=True, text=True, timeout=COPY_SECONDS)
    except subprocess.TimeoutExpired:
        return {"error": f"the copy took more than {COPY_SECONDS} seconds and was stopped; "
                         "run prove mlflow to copy again"}
    lines = done.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        # Not its error text, which can hold the URL with its password.
        return {"error": f"the copy ended with status {done.returncode}, and gave no result"}


def _place(url: str) -> str:
    from .telemetry import _without_secrets

    return _without_secrets(url)


def _names(library: str) -> list[str]:
    # The names the policy may know a library by, as provevi's copy in MLflow
    # decides them. They come only from an installed package that shows the
    # scope, such as pydantic-ai-slim for pydantic_ai; a scope that none shows
    # has no name, so the policy approves none of its spans.
    return list(_span_libraries(library))


@functools.cache
def _span_libraries(scope: str) -> tuple[str, ...]:
    # span_libraries reads every installed package's metadata, which takes
    # most of a second, and a run asks for each span. The packages do not
    # change while the process runs.
    return span_libraries(scope)


class _Sent:
    """An exporter that counts the spans it sent, and stops sending at its
    deadline, so that a run's end waits a known time and can say what was
    lost. A span exporter only logs a failure."""

    def __init__(self, exporter: Any):
        self.exporter = exporter
        self.exported = 0
        self.error = ""
        self.deadline: float | None = None

    def left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        left = self.left()
        if left is not None and left <= 0:
            self.error = self.error or "not sent in time"
            return SpanExportResult.FAILURE
        try:
            if isinstance(self.exporter, _HttpExporter):
                result = self.exporter.export(spans, timeout=left)
            else:
                result = self.exporter.export(spans)
        except Exception as e:
            result, self.error = SpanExportResult.FAILURE, f"{e.__class__.__name__}: {e}"
        if result is SpanExportResult.SUCCESS:
            self.exported += len(spans)
        else:
            self.error = self.error or getattr(self.exporter, "error", "") or "the export failed"
        return result

    def shutdown(self):
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)


class _HttpExporter:
    """OTLP over HTTP, with protobuf, as provrun sends it: to the one URL the
    policy approved, with only the headers given, through no proxy, and with
    no redirect. It reads no variable itself, so that no setting of one place
    reaches another."""

    def __init__(self, endpoint: str, headers: Mapping[str, str], *, timeout: float = 10.0,
                 verify: bool | str = True, cert: Any = None):
        try:
            import requests
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        except ImportError as e:
            raise _NoExporter("the OTLP encoder is not installed; install provrun[otlp]. Nothing is sent") from e
        self.encode = encode_spans
        self.endpoint = endpoint
        self.headers = {**headers, "Content-Type": "application/x-protobuf"}
        self.timeout = timeout
        self.verify = verify
        self.cert = cert
        self.session = requests.Session()
        self.session.trust_env = False  # no proxy variable sends the spans elsewhere
        self.error = ""

    def export(self, spans, timeout: float | None = None):
        from opentelemetry.sdk.trace.export import SpanExportResult

        wait = self.timeout if timeout is None else max(0.1, min(self.timeout, timeout))
        try:
            r = self.session.post(self.endpoint, data=self.encode(spans).SerializeToString(), headers=self.headers,
                                  timeout=wait, verify=self.verify, cert=self.cert, allow_redirects=False)
        except Exception as e:
            self.error = e.__class__.__name__
            return SpanExportResult.FAILURE
        if 200 <= r.status_code < 300:
            return SpanExportResult.SUCCESS
        self.error = f"HTTP {r.status_code}"
        return SpanExportResult.FAILURE

    def shutdown(self):
        self.session.close()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _headers(value: str) -> dict[str, str]:
    """The headers of an OTLP headers variable, key=value pairs separated by
    commas, with %-escapes. A pair that is not one, or that names a header in
    _REFUSED_HEADERS, is left out. Nothing is logged, because a value can be a
    token."""
    out = {}
    for pair in value.split(","):
        key, sep, val = pair.partition("=")
        key, val = unquote(key).strip(), unquote(val).strip()
        if sep and key and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) and key.lower() not in _REFUSED_HEADERS:
            out[key] = val
    return out


def _otlp_settings(environ: Mapping[str, str]) -> dict:
    """The headers, timeout and certificates that the OTLP variables give, by
    the OpenTelemetry rules: the traces variable wins over the general one."""
    def get(name):
        return environ.get(f"OTEL_EXPORTER_OTLP_TRACES_{name}") or environ.get(f"OTEL_EXPORTER_OTLP_{name}")

    try:
        timeout = float(get("TIMEOUT") or 10_000) / 1000  # the variable is in milliseconds
    except ValueError:
        timeout = 10.0
    cert, key = get("CLIENT_CERTIFICATE"), get("CLIENT_KEY")
    return {"headers": _headers(get("HEADERS") or ""), "timeout": timeout, "verify": get("CERTIFICATE") or True,
            "cert": (cert, key) if cert and key else cert}


def _otlp_exporter(protocol: str, url: str, environ: Mapping[str, str]):
    if protocol == "http/protobuf":
        return _HttpExporter(url, **_otlp_settings(environ))
    if protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as e:
            raise _NoExporter("the OTLP gRPC exporter is not installed; install provrun[otlp-grpc]. "
                              "Nothing is sent") from e
        settings = _otlp_settings(environ)
        exporter = OTLPSpanExporter(endpoint=url, insecure=urlsplit(url).scheme.lower() == "http",
                                    headers=settings["headers"], timeout=settings["timeout"],
                                    channel_options=(("grpc.enable_http_proxy", 0),))
        # With no headers, the exporter reads the headers variable itself, and
        # unfiltered: so set the filtered ones, even when there are none.
        exporter._headers = tuple(settings["headers"].items())
        return exporter
    raise _NoExporter(f"OpenTelemetry for Python has no {protocol} exporter; nothing is sent")


def _processor(exporter):
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    sent = _Sent(exporter)
    return BatchSpanProcessor(sent, max_queue_size=_QUEUE, schedule_delay_millis=_DELAY_MILLIS,
                              max_export_batch_size=_BATCH, export_timeout_millis=FLUSH_MILLIS), sent


def _cut(span: Any, level: str):
    """span as a destination at level receives it. Below bodies, its events,
    such as an exception with its message, its status's description and a
    name that is not a name are left out, and its links and its scope keep
    only what the level allows."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import Link, Status

    if level == "bodies":
        return span
    scope = span.instrumentation_scope
    name = span.name if plain_span_name(span.name) else getattr(scope, "name", None) or "span"
    return ReadableSpan(name=name, context=span.context, parent=span.parent, resource=span.resource,
                        attributes=_plain(dict(span.attributes or {}), level), events=(),
                        links=[Link(link.context, _plain(dict(link.attributes or {}), level)) for link in span.links],
                        kind=span.kind, status=Status(span.status.status_code),
                        start_time=span.start_time, end_time=span.end_time,
                        instrumentation_scope=InstrumentationScope(scope.name, scope.version) if scope else None)


def _plain(attributes: dict, level: str) -> dict:
    # The attributes at level, as provevi's copy in MLflow cuts them: below
    # bodies, a text that passes as a name but holds a URL, a user and
    # password, a setting or a path is left out.
    return {k: v for k, v in attributes_for(attributes, level).items()
            if not isinstance(v, (str, list, tuple)) or plain_name(v)}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)
