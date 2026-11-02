"""Control of what libraries record or send during a run.

In a controlled run, the evidence is the only record of the run, unless the
operator's telemetry policy approves another. provevi reads and checks the
policy (``provevi.telemetry``); this module applies it to the process.

The policy file comes from the ``policy`` argument of a run, or else from the
environment variable ``PROVRUN_TELEMETRY_POLICY``. With neither, the policy
approves nothing. A run file cannot name the policy file. Its ``telemetry``
section can only narrow the policy.

For each run, provrun:

- sets the environment before the backend is imported, so that a library reads
  the settings that keep it quiet, and removes each other ``OTEL_*`` and
  ``LOGFIRE_*`` variable and each debug variable, credentials included. It
  cleans the environment again as each run starts, and puts it back when the
  last run that is going on ends;
- sets the global OpenTelemetry tracer, meter and logger providers to its own,
  once for the process. Its tracer and logger providers refuse a processor
  that provrun did not add, and each refusal is written to each open run's
  evidence as ``telemetry_blocked``.

The providers stay for the rest of the process: OpenTelemetry lets a process
set each global provider only once. So a program that runs provrun, and sets
its own providers later, keeps provrun's.
"""
from __future__ import annotations

import os
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from provevi import ProveviError
from provevi.telemetry import BLOCKED, TelemetryPolicy, blocked_data

from .errors import RunSettingError

__all__ = ["POLICY_ENV", "SETTINGS", "Telemetry", "load_policy", "providers"]

#: The environment variable that names the operator's policy file.
POLICY_ENV = "PROVRUN_TELEMETRY_POLICY"

#: The settings that each run sets, before any library reads them.
SETTINGS = {
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # no fetch of the price table at import
    "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
    "LITELLM_MODE": "PRODUCTION",  # no .env file read at import
    "LITELLM_LOG": "ERROR",  # unset, LiteLLM's handler logs at DEBUG
    "PYDANTIC_AI_NO_BANNER": "1",
    "MLFLOW_USE_DEFAULT_TRACER_PROVIDER": "true",  # MLflow keeps off provrun's provider
    "MLFLOW_DISABLE_TELEMETRY": "true",
    "MLFLOW_TRACE_SAMPLING_RATIO": "0",
    # Off switches for a provider or an SDK that a library makes for itself.
    # OTEL_SDK_DISABLED is not one of them: it would stop provrun's spans too.
    "OTEL_TRACES_EXPORTER": "none",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_LOGS_EXPORTER": "none",
    "LOGFIRE_SEND_TO_LOGFIRE": "false",
    "LOGFIRE_CONSOLE": "false",
}
# Debug variables, which make an SDK log request bodies.
_DEBUG = ("ANTHROPIC_LOG", "OPENAI_LOG")
# The libraries whose import reads the settings; one imported before a run read
# the environment before provrun set it.
_READ_AT_IMPORT = ("litellm", "pydantic_ai", "mlflow", "logfire", "anthropic", "openai")


def load_policy(policy: TelemetryPolicy | str | Path | None = None) -> TelemetryPolicy:
    """The operator's policy: the one given, else the file that
    ``PROVRUN_TELEMETRY_POLICY`` names, else the policy that approves nothing.
    Raises RunSettingError if the file is bad."""
    if isinstance(policy, TelemetryPolicy):
        return policy
    path = policy if policy is not None else os.environ.get(POLICY_ENV) or None
    if path is None:
        return TelemetryPolicy.deny_all()
    try:
        return TelemetryPolicy.load(path)
    except ProveviError as e:
        raise RunSettingError(str(e)) from e


def _removed(environ: Mapping[str, str]) -> list[str]:
    # Credentials go too: no library may use them. When provrun sends spans to
    # a destination that the policy approves, provrun reads its credential.
    return sorted(k for k in environ
                  if (k.startswith(("OTEL_", "LOGFIRE_")) or k in _DEBUG) and k not in SETTINGS)


# ---- the environment, shared by the runs that are going on ------------------

_lock = threading.RLock()
_open: list[Telemetry] = []
_saved: dict[str, str | None] = {}  # each changed variable, and its value before the first run


def _enter_environment(t: Telemetry) -> None:
    # Each run cleans the environment again, so that a variable set during
    # another run does not reach this one. The run is counted, and every
    # value saved in one step, before anything changes, so that leaving after
    # an interrupt puts everything back. A variable first seen while a run is
    # going on was set during that run, so it is saved as absent.
    with _lock:
        first = not _open
        _open.append(t)
        removed = _removed(os.environ)
        _saved.update({k: (os.environ.get(k) if first else None)
                       for k in (*removed, *SETTINGS) if k not in _saved})
        t.removed = removed
        for key in removed:
            os.environ.pop(key, None)
        for key, value in SETTINGS.items():
            os.environ[key] = value


def _leave_environment(t: Telemetry) -> None:
    with _lock:
        if not any(o is t for o in _open):
            return
        _open[:] = [o for o in _open if o is not t]
        if not _open:
            # A variable that a run set, and that no later run start cleaned,
            # goes too: it was not there before the first run.
            for key in _removed(os.environ):
                _saved.setdefault(key, None)
            # Each entry leaves _saved as it is put back, so an interrupt here
            # leaves only the rest for the next run's end.
            while _saved:
                key, value = _saved.popitem()
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


# ---- the providers, set once for the process --------------------------------

class _Providers:
    """provrun's tracer, meter and logger providers, and whether they are the
    global ones."""

    def __init__(self):
        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import SpanLimits, TracerProvider
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON

        class GuardedTracerProvider(TracerProvider):
            """A tracer provider that takes only the span processors provrun adds."""

            def add_span_processor(self, span_processor) -> None:
                _refused("opentelemetry.span_processor", span_processor)

        class GuardedLoggerProvider(LoggerProvider):
            """A logger provider that takes only the log processors provrun adds."""

            def add_log_record_processor(self, log_record_processor) -> None:
                _refused("opentelemetry.log_record_processor", log_record_processor)

        # A fixed sampler, resource and limits, so that no OTEL_* variable
        # changes provrun's spans. An SDK provider reads OTEL_SDK_DISABLED
        # when it is made, and would then record nothing for good, so the
        # variable is held back while provrun's are made.
        resource = Resource({"service.name": "provrun"})
        disabled = os.environ.pop("OTEL_SDK_DISABLED", None)
        try:
            self.tracer = GuardedTracerProvider(
                sampler=ALWAYS_ON, resource=resource,
                span_limits=SpanLimits(max_attributes=128, max_events=128, max_links=128,
                                       max_attribute_length=SpanLimits.UNSET))
            self.meter = MeterProvider(resource=resource, metric_readers=[])
            self.logger = GuardedLoggerProvider(resource=resource)
        finally:
            if disabled is not None:
                os.environ.setdefault("OTEL_SDK_DISABLED", disabled)
        trace.set_tracer_provider(self.tracer)
        metrics.set_meter_provider(self.meter)
        _logs.set_logger_provider(self.logger)
        self.foreign = sorted(
            name for name, now, ours in (("tracer", trace.get_tracer_provider(), self.tracer),
                                         ("meter", metrics.get_meter_provider(), self.meter),
                                         ("logger", _logs.get_logger_provider(), self.logger))
            if now is not ours)


_providers: _Providers | None = None


def providers() -> _Providers:
    """provrun's providers, made and set as the global ones on the first call.
    ``foreign`` names each global provider that someone else set first."""
    global _providers
    with _lock:
        if _providers is None:
            _providers = _Providers()
            from . import otel

            otel.use(_providers.tracer)
        return _providers


def _library_of(obj: Any) -> str | None:
    module = getattr(type(obj), "__module__", "") or ""
    return module.split(".")[0] or None


def _refused(path: str, obj: Any) -> None:
    detail = f"refused {type(obj).__module__}.{type(obj).__qualname__}"
    with _lock:
        runs = list(_open)
    if not runs:
        warnings.warn(f"provrun: {path}: {detail}", stacklevel=3)
    for t in runs:
        t.blocked(path, library=_library_of(obj), detail=detail)


# ---- one run ----------------------------------------------------------------

@dataclass(eq=False)  # runs are told apart by identity, never by their values
class Telemetry:
    """The telemetry control of one run: its policy, and what it found."""

    policy: TelemetryPolicy
    mode: str
    removed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    imported_before: list[str] = field(default_factory=list)
    recorder: Any = None  # the run's EvidenceRecorder, once the run is open

    @classmethod
    def prepare(cls, policy: TelemetryPolicy, section: Any, mode: str) -> Telemetry:
        """Narrow policy by the run file's telemetry section, and check the
        installed libraries. Raises RunSettingError if the section widens
        the policy, or, in strict mode, on any problem."""
        try:
            policy = policy.narrow(section)
        except ProveviError as e:
            raise RunSettingError(str(e)) from e
        t = cls(policy, mode)
        t.problems = policy.check_installed()
        # A library that a program imported before the run read the environment
        # before provrun set it. The run records this; it is not a failure,
        # because a program that passes a model in has imported its library.
        t.imported_before = [m for m in _READ_AT_IMPORT if m in sys.modules]
        return t

    @contextmanager
    def applied(self) -> Iterator[Telemetry]:
        """Set the environment and the providers for the length of the run."""
        try:
            _enter_environment(self)
            p = providers()
            if p.foreign:
                self.problems.append(f"the global {', '.join(p.foreign)} provider was set by someone else "
                                     "before provrun")
            if self.problems:
                message = "telemetry: " + "; ".join(self.problems)
                if self.mode == "strict":
                    raise RunSettingError(message)
                warnings.warn(f"provrun: {message}", stacklevel=3)
            yield self
        finally:
            _leave_environment(self)

    def record(self) -> dict:
        """What run_created records: the policy, and what this run changed and found."""
        return {"policy": self.policy.digest(), "policy_source": self.policy.source,
                "removed": list(self.removed), "problems": list(self.problems),
                "imported_before": list(self.imported_before)}

    def blocked(self, path: str, *, library: str | None = None, detail: str = "") -> None:
        """Write a telemetry_blocked event, or warn when the run is not open yet."""
        if self.recorder is not None and not self.recorder.closed:
            try:
                self.recorder.emit(BLOCKED, "telemetry", data=blocked_data(path, library=library, detail=detail))
                return
            except Exception as e:  # never raise into the library's own call
                detail = f"{detail}; not recorded: {e}"
        warnings.warn(f"provrun: {path}: {detail}", stacklevel=3)
