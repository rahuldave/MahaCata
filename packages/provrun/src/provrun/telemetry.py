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
  that provrun did not add;
- turns off MLflow's autolog and tracing when MLflow is loaded, empties
  LiteLLM's callback lists, and holds the loggers that can log request bodies
  at WARNING. It does this as each run starts and around each model call
  (``recheck``), and puts LiteLLM's lists and the logger levels back when the
  last run ends;
- builds a PydanticAI agent with instrumentation off, or sent to provrun's
  providers when the policy approves it (``pydantic_ai_instrument``), and
  takes a model out of an InstrumentedModel (``pydantic_ai_model``).

Each refusal, and each thing turned off, is written to each open run's
evidence as ``telemetry_blocked``, with the distribution that did it.

The providers stay for the rest of the process: OpenTelemetry lets a process
set each global provider only once. MLflow's autolog and tracing stay off too. So a program that runs provrun, and sets
its own providers later, keeps provrun's.
"""
from __future__ import annotations

import functools
import importlib
import inspect
import logging
import os
import sys
import threading
import warnings
from importlib import metadata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from provevi import ProveviError
from provevi.telemetry import BLOCKED, TelemetryPolicy, blocked_data

from .errors import RunSettingError

__all__ = ["LITELLM_LISTS", "LOGGERS", "POLICY_ENV", "SETTINGS", "Telemetry", "load_policy", "providers",
           "pydantic_ai_instrument", "pydantic_ai_model", "recheck"]

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
        if first:
            _saved_at_start.update(m for m in ("litellm",) if m in sys.modules)
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
            # After the environment, so that an error here leaves it put back.
            _restore_libraries()


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


@functools.cache
def _distribution(module: str) -> str | None:
    """The distribution that installed module, the name pip knows it by, such
    as opentelemetry-sdk for opentelemetry.sdk.trace."""
    top = module.split(".")[0]
    if not top:
        return None
    try:
        names = metadata.packages_distributions().get(top, [])
    except Exception:  # a broken install elsewhere; the module still has a name
        return top
    if len(names) == 1:
        return names[0]
    # A namespace package, such as opentelemetry, comes from many
    # distributions; the one whose files hold the module installed it.
    # The longest path wins, so mlflow.sklearn goes to the one that ships it,
    # not to the first that ships mlflow/__init__.py.
    parts = module.split(".")
    held = {}
    for name in names:
        try:
            held[name] = {str(f).replace("\\", "/") for f in metadata.files(name) or ()}
        except Exception:  # a broken install names no files
            held[name] = set()
    for n in range(len(parts), 0, -1):
        stem = "/".join(parts[:n])
        for name in names:
            if f"{stem}.py" in held[name] or f"{stem}/__init__.py" in held[name]:
                return name
    # Not installed by a distribution, or not found: the module name, as a
    # program may name its own.
    return names[0] if names else top


def _library_of(obj: Any) -> str | None:
    if isinstance(obj, type) or inspect.isfunction(obj) or inspect.ismethod(obj):
        module = getattr(obj, "__module__", None) or ""
    else:
        module = type(obj).__module__ or ""
    # A program's own script, or a builtin, comes from no library.
    return _distribution(module) if module and module not in ("builtins", "__main__") else None


def _describe(obj: Any) -> str:
    if isinstance(obj, str):
        return repr(obj)
    if isinstance(obj, type) or inspect.isfunction(obj) or inspect.ismethod(obj):
        return f"{obj.__module__}.{obj.__qualname__}"
    return f"a {type(obj).__module__}.{type(obj).__qualname__}"


def _to_open_runs(path: str, obj: Any, detail: str, library: str | None = None) -> None:
    with _lock:
        runs = list(_open)
    if not runs:
        warnings.warn(f"provrun: {path}: {detail}", stacklevel=4)
    for t in runs:
        t.blocked(path, library=library or _library_of(obj), detail=detail)


def _refused(path: str, obj: Any) -> None:
    _to_open_runs(path, obj, f"refused {_describe(obj)}")


# ---- the libraries' own telemetry -------------------------------------------
#
# Each function here finds what a library has switched on, turns it off, and
# writes a telemetry_blocked event for it to each open run. A run calls them
# all as it starts, and a backend calls ``recheck`` around each model call, so
# that a callback, a logger level or an MLflow flavor that comes back during
# the run goes again.

#: LiteLLM's callback lists. A logger in any of them sees each model call.
LITELLM_LISTS = ("callbacks", "input_callback", "success_callback", "failure_callback", "service_callback",
                 "audit_log_callbacks", "_async_input_callback", "_async_success_callback",
                 "_async_failure_callback")
#: The loggers that a run holds at WARNING, because some log request bodies at
#: DEBUG. botocore, which provcat uses for S3, logs request signatures and headers.
LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "anthropic", "anthropic._base_client",
           "openai", "openai._base_client", "botocore")
# The MLflow flavors whose library a run may use. Each is turned off by name
# when its library is loaded, so that a later mlflow.autolog() or
# mlflow.genai.evaluate() leaves it off.
_MLFLOW_FLAVORS = ("litellm", "pydantic_ai", "openai", "anthropic")

_saved_callbacks: dict[str, list] = {}  # LiteLLM's lists before the first run, to put back
_saved_levels: dict[str, int] = {}  # each logger's level before the first run, to put back
_saved_at_start: set[str] = set()  # the libraries loaded when the first run started


def _quiet_mlflow() -> list[str]:
    """Turn MLflow's autolog and tracing off, if MLflow is loaded. The calls
    import MLflow, so nothing is done when it is not. Returns the problems."""
    if sys.modules.get("mlflow") is None:  # not loaded, or blocked by the program
        return []
    try:
        return _quiet_loaded_mlflow()
    except Exception as e:  # an old MLflow, with no tracing module, or a broken one
        return [f"cannot turn off MLflow: {e.__class__.__name__}: {e}"]


def _quiet_loaded_mlflow() -> list[str]:
    import mlflow
    from mlflow.tracing.provider import is_tracing_enabled
    from mlflow.utils.autologging_utils import AUTOLOGGING_INTEGRATIONS

    # A flavor is on when its last autolog call did not disable it. The entry
    # "mlflow" is the general call's, and has no value until it is made.
    on = sorted(f for f, conf in AUTOLOGGING_INTEGRATIONS.items() if conf.get("disable") is False)
    names = {f for f in _MLFLOW_FLAVORS if f in sys.modules}
    off = {f for f, conf in AUTOLOGGING_INTEGRATIONS.items() if conf.get("disable") is True}
    if not on and names <= off and not is_tracing_enabled():
        return []  # already off, the common case of a recheck
    for flavor in on:
        what = "general" if flavor == "mlflow" else flavor
        _to_open_runs("mlflow.autolog", mlflow, f"turned off MLflow's {what} autolog",
                      _distribution(f"mlflow.{flavor}" if flavor != "mlflow" else "mlflow"))
    problems = []
    # The general call does not undo a flavor's own call, so each flavor goes by name.
    try:
        mlflow.autolog(disable=True, silent=True)
    except Exception as e:
        problems.append(f"mlflow.autolog(disable=True) failed: {e}")
    for flavor in sorted(set(on) | names):
        if flavor == "mlflow":
            continue
        try:
            importlib.import_module(f"mlflow.{flavor}").autolog(disable=True, silent=True)
        except Exception as e:
            problems.append(f"mlflow.{flavor}.autolog(disable=True) failed: {e}")
    try:
        mlflow.tracing.disable()
    except Exception as e:
        problems.append(f"mlflow.tracing.disable() failed: {e}")
    return problems


def _mlflow_usage_off() -> list[str]:
    # MLflow reads MLFLOW_DISABLE_TELEMETRY once, at import. If it was imported
    # before a run set the variable, its usage client must read it again.
    if sys.modules.get("mlflow") is None:
        return []
    try:
        from mlflow.telemetry.client import set_telemetry_client
    except ImportError:
        return []  # MLflow 2 has no usage telemetry
    try:
        set_telemetry_client()
    except Exception as e:
        return [f"cannot turn off MLflow's usage telemetry: {e}"]
    return []


def _quiet_litellm() -> None:
    """Empty LiteLLM's callback lists. A policy cannot keep a logger in them:
    each sends to its own service, past the levels of the policy, and the one
    that writes to the global tracer provider, "otel", does not start without
    LiteLLM's proxy. Approved spans of LiteLLM calls are provrun's own."""
    litellm = sys.modules.get("litellm")
    if litellm is None:
        return
    for name in LITELLM_LISTS:
        entries = getattr(litellm, name, None)
        if not isinstance(entries, list):
            continue
        with _lock:
            if name not in _saved_callbacks:
                # Only what was there before the first run goes back at the end.
                _saved_callbacks[name] = list(entries) if "litellm" in _saved_at_start else []
        if not entries:
            continue
        for e in list(entries):
            # A string names one of LiteLLM's own integrations.
            _to_open_runs("litellm.callbacks", e, f"removed {_describe(e)} from litellm.{name}",
                          "litellm" if isinstance(e, str) else None)
        entries.clear()
    utils = sys.modules.get("litellm.utils")
    if utils is not None and getattr(utils, "user_logger_fn", None) is not None:
        fn = utils.user_logger_fn
        _to_open_runs("litellm.logger_fn", fn, f"removed {_describe(fn)}, given as logger_fn")
        utils.user_logger_fn = None


def _quiet_loggers() -> None:
    """Hold each logger in LOGGERS at WARNING or above, so that a root
    logger at DEBUG cannot turn it on."""
    for name in LOGGERS:
        log = logging.getLogger(name)
        with _lock:
            _saved_levels.setdefault(name, log.level)
        if log.level >= logging.WARNING:
            continue
        if log.getEffectiveLevel() < logging.WARNING:
            _to_open_runs("logging", log, f"raised the logger {name!r} from "
                          f"{logging.getLevelName(log.getEffectiveLevel())} to WARNING",
                          _distribution(name.split()[0].lower()))
        log.setLevel(logging.WARNING)


def recheck(*, warn: bool = True) -> list[str]:
    """Turn off again what a library switched on during a run: MLflow, a
    LiteLLM callback, a logger level. A backend calls it around each model
    call. Returns the problems that MLflow's calls raised, after a warning
    for each unless warn is False. Outside a run it does nothing."""
    with _lock:
        if not _open:
            return []
    problems = _quiet_mlflow()
    _quiet_litellm()
    _quiet_loggers()
    if warn:
        for problem in problems:
            warnings.warn(f"provrun: telemetry: {problem}", stacklevel=2)
    return problems


def _restore_libraries() -> None:
    # Called when the last open run ends. LiteLLM's lists get back what they
    # held before the first run; the loggers get back their levels. MLflow's
    # autolog and tracing stay off: MLflow has no call that puts them back.
    litellm = sys.modules.get("litellm")
    if litellm is not None:
        for name, entries in _saved_callbacks.items():
            current = getattr(litellm, name, None)
            if isinstance(current, list):
                current[:] = entries
    _saved_callbacks.clear()
    for name, level in _saved_levels.items():
        logging.getLogger(name).setLevel(level)
    _saved_levels.clear()
    _saved_at_start.clear()


def pydantic_ai_instrument(policy: TelemetryPolicy):
    """The value for ``agent.instrument`` of an agent that provrun builds:
    False, or, when the policy approves PydanticAI's telemetry, settings that
    send its spans to provrun's providers. The policy may name PydanticAI as
    pydantic-ai or as pydantic-ai-slim. The spans hold prompts and replies only
    when every one of its destinations receives bodies. The value set on the agent
    wins over ``Agent.instrument_all``."""
    destinations = [*policy.destinations_for("pydantic-ai"), *policy.destinations_for("pydantic-ai-slim")]
    if not destinations:
        return False
    from pydantic_ai.models.instrumented import InstrumentationSettings

    p = providers()
    # Prompts and replies go into the spans only when every destination may receive bodies.
    return InstrumentationSettings(tracer_provider=p.tracer, meter_provider=p.meter, include_binary_content=False,
                                   include_content=all(d.receives == "bodies" for d in destinations))


def pydantic_ai_model(model: Any, telemetry: Telemetry | None = None) -> Any:
    """model without an InstrumentedModel around it, which would send spans
    by its own settings. Records each one it takes off. Raises ValueError for
    an InstrumentedModel inside another model, such as a FallbackModel:
    PydanticAI looks only at the outer model, so the inner one's spans would
    go to its own provider, and provrun does not change the caller's model.
    The events go to telemetry, the run whose model it is, else to each open run."""
    try:
        from pydantic_ai.models import Model
        from pydantic_ai.models.instrumented import InstrumentedModel
    except ImportError:
        return model
    while isinstance(model, InstrumentedModel):
        detail = "took the model out of its InstrumentedModel"
        if telemetry is not None:
            telemetry.blocked("pydantic_ai.instrumented_model", library=_library_of(model), detail=detail)
        else:
            _to_open_runs("pydantic_ai.instrumented_model", model, detail)
        model = model.wrapped
    if isinstance(model, Model) and _holds(model, InstrumentedModel, Model):
        raise ValueError("the model holds an InstrumentedModel inside another model, whose spans provrun "
                         "cannot turn off; pass the model without it")
    return model


def _holds(model: Any, kind: type, base: type, seen: set[int] | None = None) -> bool:
    # PydanticAI's models hold others in wrapped (WrapperModel) or in a list,
    # models (FallbackModel). Only models are followed, so an odd attribute
    # or a mock ends the walk.
    seen = set() if seen is None else seen
    if id(model) in seen:
        return False
    seen.add(id(model))
    inner = []
    try:
        wrapped = getattr(model, "wrapped", None)
        models = getattr(model, "models", None)
    except Exception:
        return False
    inner.append(wrapped)
    if isinstance(models, (list, tuple)):
        inner.extend(models)
    return any(isinstance(m, kind) or _holds(m, kind, base, seen) for m in inner if isinstance(m, base))


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
    pending: list[tuple[str, str | None, str]] = field(default_factory=list)  # blocked before it was open

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
            # What this finds waits in pending until the run's evidence is open.
            self.problems.extend(_mlflow_usage_off() + recheck(warn=False))
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
            for path, library, detail in self.pending:  # the run never opened its evidence
                warnings.warn(f"provrun: {path}: {detail}", stacklevel=3)
            self.pending.clear()

    def record(self) -> dict:
        """What run_created records: the policy, and what this run changed and found."""
        return {"policy": self.policy.digest(), "policy_source": self.policy.source,
                "removed": list(self.removed), "problems": list(self.problems),
                "imported_before": list(self.imported_before)}

    def attach(self, recorder: Any) -> None:
        """Write the run's telemetry_blocked events to recorder from now on,
        starting with those found before the run's evidence was open."""
        with _lock:
            self.recorder = recorder
            pending, self.pending = self.pending, []
        for path, library, detail in pending:
            self.blocked(path, library=library, detail=detail)

    def blocked(self, path: str, *, library: str | None = None, detail: str = "") -> None:
        """Write a telemetry_blocked event. Before the run's evidence is open,
        the event waits for ``attach``; after it closes, it is a warning."""
        with _lock:
            if self.recorder is None:
                self.pending.append((path, library, detail))
                return
        if not self.recorder.closed:
            try:
                self.recorder.emit(BLOCKED, "telemetry", data=blocked_data(path, library=library, detail=detail))
                return
            except Exception as e:  # never raise into the library's own call
                detail = f"{detail}; not recorded: {e}"
        warnings.warn(f"provrun: {path}: {detail}", stacklevel=3)
