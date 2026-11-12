"""Control of what libraries record or send during a run.

In a controlled run, the evidence is the only record of the run, unless the
operator's telemetry policy approves another. provevi reads and checks the
policy (``provevi.telemetry``); this module applies it to the process.

The policy file comes from the ``policy`` argument of a run, or else from the
environment variable ``PROVRUN_TELEMETRY_POLICY``. With neither, the policy
approves nothing. A run file cannot name the policy file. Its ``telemetry``
section can only narrow the policy.

For each run, provrun:

- sets a few settings before the backend is imported, so that a library reads
  values that keep it quiet (``SETTINGS``), and puts them back when the last
  run that is going on ends. It leaves every other variable as the user set
  it, and records the telemetry variables (``variables``): where they point,
  never a credential;
- sets the global OpenTelemetry tracer, meter and logger providers to its own,
  once for the process. Its tracer and logger providers refuse a processor
  that provrun did not add;
- turns off MLflow's autolog and tracing when MLflow is loaded, empties
  LiteLLM's callback lists, and holds the loggers that can log request bodies
  at WARNING. It does this as each run starts and around each model call
  (``recheck``), and puts LiteLLM's lists and the logger levels back when the
  last run ends;
- finds a configured Logfire and an OpenTelemetry auto-configuration, which
  send past the policy and cannot be turned off;
- builds a PydanticAI agent with instrumentation off, or sent to provrun's
  providers when the policy approves it (``pydantic_ai_instrument``), and
  takes a model out of an InstrumentedModel (``pydantic_ai_model``);
- sends the run's spans only to the places that the policy approves, and
  keeps an approved library's spans as evidence (``destinations``).

Each refusal, and each thing turned off, is written to each open run's
evidence as ``telemetry_blocked``, with the distribution that did it.

The providers stay for the rest of the process: OpenTelemetry lets a process
set each global provider only once. MLflow's autolog and tracing stay off too.
So a program that runs provrun, and sets its own providers later, keeps provrun's.
"""
from __future__ import annotations

import functools
import importlib
import inspect
import logging
import os
import re
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from provevi import ProveviError
from provevi.telemetry import BLOCKED, TelemetryPolicy, blocked_data, distribution_of

from .errors import RunSettingError

__all__ = ["FAMILIES", "LITELLM_LISTS", "LOGGERS", "POLICY_ENV", "SETTINGS", "Telemetry", "load_policy",
           "providers", "pydantic_ai_instrument", "pydantic_ai_model", "recheck", "variables"]

#: The event type of a problem that a run found, but could not turn off.
PROBLEM = "telemetry_problem"

#: The environment variable that names the operator's policy file.
POLICY_ENV = "PROVRUN_TELEMETRY_POLICY"

#: The settings that each run sets, before any library reads them. None names
#: a destination; each stops a fetch, a usage report or a library's own export.
SETTINGS = {
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # no fetch of the price table at import
    "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "True",
    "LITELLM_MODE": "PRODUCTION",  # no .env file read at import
    "LITELLM_LOG": "ERROR",  # unset, LiteLLM's handler logs at DEBUG
    "PYDANTIC_AI_NO_BANNER": "1",
    "MLFLOW_USE_DEFAULT_TRACER_PROVIDER": "true",  # MLflow keeps off provrun's provider
    "MLFLOW_DISABLE_TELEMETRY": "true",
    "LOGFIRE_SEND_TO_LOGFIRE": "false",
    "LOGFIRE_CONSOLE": "false",
}
#: The families of telemetry variables that a run records.
FAMILIES = ("OTEL_", "LOGFIRE_", "MLFLOW_", "LITELLM_")
# The variables recorded by value; every other one is recorded by name only,
# because it can hold a token, a header or a key.
_BY_VALUE_ENDINGS = ("_ENDPOINT", "_URI", "_URL")
_BY_VALUE = ("OTEL_SERVICE_NAME", "OTEL_EXPORTER_OTLP_PROTOCOL")
# The libraries whose import reads the settings; one imported before a run read
# the environment before provrun set it.
_READ_AT_IMPORT = ("litellm", "pydantic_ai", "mlflow", "logfire", "anthropic", "openai")
# Modules that are loaded only when OpenTelemetry configures itself from the
# environment, by opentelemetry-instrument or a distro.
_AUTO_CONFIGURATION = ("opentelemetry.instrumentation.auto_instrumentation", "opentelemetry.distro")
# The libraries the full scan looks in for patches, and the mark of MLflow's.
_WATCHED = ("litellm", "pydantic_ai", "openai", "anthropic", "httpx", "httpcore", "requests", "urllib3", "aiohttp")
_GORILLA = "_gorilla_active_patch_"
_QUALNAME = type.__dict__["__qualname__"]  # a class's own name, past any metaclass
_CLASS_DICT = type.__dict__["__dict__"]
_MODULE_DICT = ModuleType.__dict__["__dict__"]


def _warn(message: str) -> None:
    # A warning that never raises, even when warnings are errors.
    try:
        warnings.warn(message, stacklevel=3)
    except Exception:
        pass


def load_policy(
    policy: TelemetryPolicy | str | Path | None = None,  # A policy, or the path of its file; None reads PROVRUN_TELEMETRY_POLICY
) -> TelemetryPolicy:  # The operator's policy
    """Find the operator's telemetry policy.

    It is the policy given, else the file that ``PROVRUN_TELEMETRY_POLICY``
    names, else the policy that approves nothing. Raises RunSettingError if
    the file is bad."""
    if isinstance(policy, TelemetryPolicy):
        return policy
    path = policy if policy is not None else os.environ.get(POLICY_ENV) or None
    if path is None:
        return TelemetryPolicy.deny_all()
    try:
        return TelemetryPolicy.load(path)
    except ProveviError as e:
        raise RunSettingError(str(e)) from e


def _without_secrets(value: str) -> str:
    """Where value points, and nothing that can hold a secret. A URL with a
    host gives its scheme, host and port: the user, password, path, query and
    fragment are left out, because a token, a password or a local path can sit
    in each. A URL without a host, such as sqlite:///path, gives its scheme
    alone. An @ outside the user part, as an unescaped password with a ? or
    a # leaves, is not recorded. A bare name of letters, such as databricks, is kept. Nothing else
    is recorded."""
    if re.fullmatch(r"[A-Za-z][A-Za-z-]*", value):
        return value
    try:
        u = urlsplit(value)
        port = f":{u.port}" if u.port is not None else ""
    except ValueError:
        return "<not recorded: not a valid URL>"
    if not u.scheme or ("@" in value and "@" not in u.netloc) or ("@" in value and not u.hostname):
        return "<not recorded: not a URL>"
    if not u.hostname:  # only a URL such as sqlite:///path; alice:password is not one
        return f"{u.scheme}:<path not recorded>" if value.startswith(f"{u.scheme}://") else "<not recorded: not a URL>"
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    return f"{u.scheme}://{host}{port}"


def variables(
    environ: Mapping[str, str],  # The environment variables, such as os.environ
) -> dict[str, str | None]:  # Each telemetry variable, in name order, with what may be recorded of its value
    """Say which telemetry variables are set, and where they point, never a credential.

    The variables are those of FAMILIES, less provrun's SETTINGS. A variable
    that names a place, such as an endpoint or a URI, maps to its value
    without credentials; a protocol or service name maps to its value when
    that is a plain name, else to "<not recorded>"; every
    other one maps to None, so that only its name is recorded."""
    out = {}
    for key in sorted(environ):
        if not key.startswith(FAMILIES) or key in SETTINGS:
            continue
        if key in _BY_VALUE:  # a protocol such as http/protobuf, or a service name
            out[key] = environ[key] if re.fullmatch(r"[A-Za-z0-9._/-]+", environ[key]) else "<not recorded>"
        else:
            out[key] = _without_secrets(environ[key]) if key.endswith(_BY_VALUE_ENDINGS) else None
    return out


# ---- the environment, shared by the runs that are going on ------------------

_lock = threading.RLock()
_open: list[Telemetry] = []
_saved: dict[str, str | None] = {}  # each setting's value before the first run


def _enter_environment(t: Telemetry) -> None:
    # Each run sets the settings again, so that a value changed during another
    # run does not reach this one. The run is counted, and every value saved,
    # before anything changes, so that leaving after an interrupt puts
    # everything back.
    with _lock:
        first = not _open
        _open.append(t)
        if first:
            _saved_at_start.update(m for m in ("litellm",) if m in sys.modules)
            for k in SETTINGS:  # a value that an interrupted end left is still the user's
                _saved.setdefault(k, os.environ.get(k))
        t.variables = variables(os.environ)
        # What the user had set, before the first run, where it differs.
        t.overridden = {k: v for k, v in _saved.items() if v is not None and v != SETTINGS[k]}
        for key, value in SETTINGS.items():
            os.environ[key] = value


def _users_environment() -> dict[str, str]:
    """The environment with the user's own value of each setting that runs
    set, such as LOGFIRE_SEND_TO_LOGFIRE, so that it says what the user wants."""
    with _lock:
        env = dict(os.environ)
        for key in SETTINGS:
            if key in _saved:
                if _saved[key] is None:
                    env.pop(key, None)
                else:
                    env[key] = _saved[key]
    return env


def _leave_environment(t: Telemetry) -> None:
    with _lock:
        if not any(o is t for o in _open):
            return
        _open[:] = [o for o in _open if o is not t]
        if not _open:
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


def providers() -> _Providers:  # The providers: tracer, meter and logger, and foreign, each global one someone else set first
    """Give provrun's OpenTelemetry providers, made and set as the global ones on the first call.

    The tracer provider takes no span processor but provrun's router, which
    the first call adds."""
    global _providers
    with _lock:
        if _providers is None:
            _providers = _Providers()
            from . import otel

            otel.use(_providers.tracer)
            from opentelemetry.sdk.trace import TracerProvider

            from .destinations import router

            # The base method: the guarded one refuses every processor.
            TracerProvider.add_span_processor(_providers.tracer, router)
        return _providers


@functools.cache
def _distribution(module: str) -> str | None:
    """The distribution that installed module, the name pip knows it by, such
    as opentelemetry-sdk for opentelemetry.sdk.trace. It is provevi's
    distribution_of, cached, so that the copy in MLflow names a library as
    provrun does."""
    return distribution_of(module)


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


def _found() -> list[str]:
    """What sends past the policy and cannot be turned off: a configured
    Logfire, an OpenTelemetry auto-configuration, or a global provider that
    is not provrun's. It reads sys.modules and imports nothing."""
    found = []
    config = getattr(sys.modules.get("logfire._internal.config"), "GLOBAL_CONFIG", None)
    if getattr(config, "_initialized", False) is True:
        found.append("logfire.configure() has run, and Logfire's own exporters send past the policy")
    for name in _AUTO_CONFIGURATION:
        if sys.modules.get(name) is not None:
            found.append(f"{name} is loaded, and an OpenTelemetry auto-configuration sends past the policy")
    p = _providers
    if p is not None and not isinstance(p, SimpleNamespace):
        from opentelemetry import _logs, metrics, trace

        for name, now, ours in (("tracer", trace.get_tracer_provider(), p.tracer),
                                ("meter", metrics.get_meter_provider(), p.meter),
                                ("logger", _logs.get_logger_provider(), p.logger)):
            if now is not ours and name not in p.foreign:
                found.append(f"the global {name} provider was replaced during the run")
    return found


def recheck(
    telemetry: Telemetry | None = None,  # The telemetry of the run that calls, which fail checks
    *,
    fail: bool = True,  # Raise RunSettingError if telemetry is strict and has found a problem
) -> list[str]:  # The problems found; empty outside a run
    """Turn off again what a library switched on during a run.

    That is MLflow, a LiteLLM callback or a logger level. Then look for what cannot be turned
    off, such as a configured Logfire. A backend calls it around each model
    call. Each problem goes to each open run once, as Telemetry.found says.
    Outside a run it does nothing."""
    with _lock:
        if not _open:
            return []
        runs = list(_open)
    problems = _quiet_mlflow()
    _quiet_litellm()
    _quiet_loggers()
    problems += _found()
    for t in runs:
        for problem in problems:
            t.found(problem)
    if fail and telemetry is not None:
        telemetry.fail_if_found()
    return problems


def _watched(name: str) -> bool:
    # A watched library's own module, but not a types module: the SDKs' hold
    # thousands of data models, which no telemetry library patches, and which
    # would take most of the time.
    top, _, rest = name.partition(".")
    return top in _WATCHED and not (rest == "types" or rest.endswith(".types") or ".types." in f".{rest}.")


def _scan() -> tuple[list[str], dict[str, tuple[Any, bool]]]:
    """A full scan of the watched libraries' modules for patches. It gives
    MLflow's gorilla patches, by their markers, and each function and method
    by its place, with the function and whether it wraps another. It reads
    each member without running its code, so that a lazy module or proxy is
    not loaded. It takes a few hundred milliseconds, so a run does it only at
    its start and its end."""
    gorilla, found = [], {}
    for name, module in list(sys.modules.items()):
        if module is None or not isinstance(name, str) or not _watched(name):
            continue
        try:  # past a module's own __getattribute__, which can load a lazy module
            members = list(_MODULE_DICT.__get__(module).items())
        except (TypeError, AttributeError):
            continue
        for attr, obj in members:
            try:
                keys = dict(_CLASS_DICT.__get__(obj)) if issubclass(type(obj), type) else None  # past a metaclass
                if keys is not None and keys.get("__module__") == name:
                    where = f"{name}.{_QUALNAME.__get__(obj)}"
                    gorilla += [f"{where}.{k[len(_GORILLA):]}" for k in keys if k.startswith(_GORILLA)]
                    for k, v in keys.items():
                        _note(found, f"{where}.{k}", v)
                else:
                    _note(found, f"{name}.{attr}", obj)
            except Exception:  # a member that raises when read tells nothing
                continue
    return sorted(set(gorilla)), found


def _note(found: dict[str, tuple[Any, bool]], where: str, obj: Any) -> None:
    if type(obj) in (staticmethod, classmethod):
        obj = obj.__func__
    if callable(obj):
        found[where] = (obj, inspect.getattr_static(obj, "__wrapped__", None) is not None)


def _scan_problems(baseline: dict[str, tuple[Any, bool]] | None) -> tuple[list[str], dict[str, tuple[Any, bool]]]:
    """The problems of a scan: each MLflow patch, and, against baseline, each
    function that was there at the start and was replaced by a wrapper. A
    function first seen during the run, as a module loads, is not one. The
    baseline holds the functions themselves, so that a new one cannot take
    the address of one that was freed."""
    gorilla, found = _scan()
    problems = [f"MLflow's patch on {where} is still on" for where in gorilla]
    if baseline is not None:
        problems += [f"{where} was replaced by a wrapper during the run" for where, (obj, wraps) in sorted(found.items())
                     if wraps and where in baseline and baseline[where][0] is not obj]
    return problems, found


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


def pydantic_ai_instrument(
    policy: TelemetryPolicy,  # The run's policy
) -> Any:  # False, or PydanticAI's InstrumentationSettings on provrun's providers
    """Give the value for ``agent.instrument`` of an agent that provrun builds.

    It is False unless the policy approves PydanticAI's telemetry; then it is
    settings that send its spans to provrun's providers. The policy may name PydanticAI as
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


def pydantic_ai_model(
    model: Any,  # A PydanticAI model, or anything else, which comes back as it is
    telemetry: Telemetry | None = None,  # The run whose model it is, which gets the events; None sends them to each open run
) -> Any:  # The model, with each InstrumentedModel around it taken off
    """Take the InstrumentedModel off a model, which would send spans by its own settings.

    Each one taken off is written as telemetry_blocked. Raises ValueError for
    an InstrumentedModel inside another model, such as a FallbackModel:
    PydanticAI looks only at the outer model, so the inner one's spans would
    go to its own provider, and provrun does not change the caller's model."""
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

    policy: TelemetryPolicy  # The operator's policy, narrowed by the run file's telemetry section
    mode: str  # The evidence mode, "strict" or "lenient"; strict fails the run on a problem
    variables: dict[str, str | None] = field(default_factory=dict)  # The telemetry variables, as variables() records them
    overridden: dict[str, str] = field(default_factory=dict)  # Each setting that the user had set otherwise, with the user's value
    problems: list[str] = field(default_factory=list)  # The problems found before the run opened, which run_created records
    imported_before: list[str] = field(default_factory=list)  # The libraries imported before the run set the environment
    recorder: Any = None  # The run's EvidenceRecorder, once the run is open
    pending: list[tuple[str, str | None, str]] = field(default_factory=list)  # Blocked before it was open
    seen: set[str] = field(default_factory=set)  # Each problem, reported once
    failed: list[str] = field(default_factory=list)  # In strict mode, problems found during the run
    baseline: dict[str, tuple[Any, bool]] | None = None  # Each function at the run's start
    recorded: int | None = None  # How many problems run_created's record holds
    ended: bool = False  # The end checks have run
    checked: bool = False  # The start's check is past; a problem now fails or warns as it is found
    routes: Any = None  # Where the run's spans go, a destinations.Routes

    @classmethod
    def prepare(
        cls,
        policy: TelemetryPolicy,  # The operator's policy
        section: Any,  # The run file's telemetry section, or None
        mode: str,  # The evidence mode, "strict" or "lenient"
    ) -> Telemetry:  # The run's telemetry, with the problems the installed libraries give
        """Narrow the policy by the run file's telemetry section, and check the installed libraries.

        Raises RunSettingError if the section widens the policy. A problem
        found here fails a strict run when ``applied`` starts."""
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
    def applied(
        self,
        store: Any = None,  # The run's evidence store; None is the file store
        *,
        close: bool = True,  # Close routes at the end; the runner passes False and closes them in a thread
    ) -> Iterator[Telemetry]:  # This telemetry, inside a with block
        """Set the environment and the providers for the length of the run.

        A strict run with a problem raises RunSettingError as it starts; a
        lenient one warns. Sending the last spans as routes close can take
        seconds."""
        try:
            _enter_environment(self)
            p = providers()
            # What this finds before the run is open goes into run_created's
            # problems, through found; what it blocks waits in pending.
            for problem in [*_mlflow_usage_off(), *recheck(fail=False)]:
                self.found(problem)
            # After the recheck, which takes MLflow's patches off.
            scanned, self.baseline = _scan_problems(None)
            for problem in scanned:
                self.found(problem)
            from .destinations import Routes

            self.routes = Routes.read(self, _users_environment(), store)
            if p.foreign:
                self.problems.append(f"the global {', '.join(p.foreign)} provider was set by someone else "
                                     "before provrun")
            if self.problems:
                message = "telemetry: " + "; ".join(self.problems)
                if self.mode == "strict":
                    raise RunSettingError(message)
                warnings.warn(f"provrun: {message}", stacklevel=3)
            self.checked = True
            yield self
        finally:
            try:
                if close and self.routes is not None:
                    self.routes.close()  # after the run's root span has ended, so it is sent too
            finally:
                _leave_environment(self)
            for path, library, detail in self.pending:  # the run never opened its evidence
                _warn(f"provrun: {path}: {detail}")
            self.pending.clear()

    def record(
        self,
    ) -> dict:  # The policy's digest and source, the variables, the overridden settings, the problems, imported_before and the routes
        """Give what run_created records: the policy, and what this run changed and found."""
        with _lock:
            self.recorded = len(self.problems)
            problems = list(self.problems)
        return {"policy": self.policy.digest(), "policy_source": self.policy.source,
                "variables": dict(self.variables), "overridden": dict(self.overridden),
                "problems": problems,
                "imported_before": list(self.imported_before),
                **(self.routes.record() if self.routes is not None else {"destinations": [], "redirects": []})}

    def found(
        self,
        problem: str,  # What was found, in words
    ) -> None:  # Nothing
        """Take a problem found during the run.

        It is written as a telemetry_problem event; a strict run then fails at its next check, and a lenient one
        warns. Each problem counts once. Before the start's check, it goes
        into run_created's problems, and that check fails or warns."""
        with _lock:
            if problem in self.seen:
                return
            self.seen.add(problem)
            if self.recorder is None:  # run_created records it, or attach writes it
                self.problems.append(problem)
                if not self.checked:
                    return
            if self.mode == "strict":
                self.failed.append(problem)
            if self.recorder is None:
                if self.mode != "strict":
                    _warn(f"provrun: telemetry: {problem}")
                return
        self._write(problem)

    def _write(self, problem: str, *, warn: bool = True) -> None:
        # Never raises into a library's own call, even when warnings are
        # errors. A problem that cannot be recorded is always a warning.
        note = None
        if self.recorder is not None and not self.recorder.closed:
            try:
                self.recorder.emit(PROBLEM, "telemetry", data={"problem": problem})
            except Exception as e:
                note = f"; not recorded: {e}"
        else:
            note = "; not recorded: the run's evidence is closed"
        if note is not None or (warn and self.mode != "strict"):
            _warn(f"provrun: telemetry: {problem}{note or ''}")

    def fail_if_found(
        self,
    ) -> None:  # Nothing
        """Raise RunSettingError if a strict run found a problem."""
        if self.failed:
            raise RunSettingError("telemetry: " + "; ".join(self.failed))

    def end(
        self,
        *,
        fail: bool = True,  # Raise RunSettingError if a strict run found a problem; a run that has failed passes False, to keep its own error
    ) -> None:  # Nothing
        """Run the checks at the run's end: a last recheck, and the full scan.

        The scan finds a function wrapped during the run. Each check runs once."""
        if not self.ended:
            self.ended = True
            recheck(fail=False)
            problems, _ = _scan_problems(self.baseline)
            for problem in problems:
                self.found(problem)
        if fail:
            self.fail_if_found()

    def attach(
        self,
        recorder: Any,  # The run's EvidenceRecorder, now open
        trace_id: str | None = None,  # The run's trace ID in hex, whose spans go to the run's destinations from now on
    ) -> None:  # Nothing
        """Write the run's telemetry events to its evidence from now on.

        The telemetry_blocked events found before the evidence was open come
        first. A problem found after run_created's record was taken is
        written as a telemetry_problem event."""
        with _lock:
            self.recorder = recorder
            pending, self.pending = self.pending, []
            late = self.problems[self.recorded:] if self.recorded is not None else []
        for path, library, detail in pending:
            self.blocked(path, library=library, detail=detail)
        for problem in late:  # found failed or warned already
            self._write(problem, warn=False)
        if self.routes is not None:
            self.routes.start(trace_id)

    def blocked(
        self,
        path: str,  # What was blocked, such as pydantic_ai.instrumented_model or destination
        *,
        library: str | None = None,  # The distribution that did it, when known
        detail: str = "",  # What happened, in words
    ) -> None:  # Nothing
        """Write a telemetry_blocked event.

        Before the run's evidence is open, the event waits for ``attach``;
        after it closes, it is a warning."""
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
