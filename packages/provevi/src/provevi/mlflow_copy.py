"""The copy in MLflow: a sealed run, rebuilt from its evidence in MLflow's tables.

The evidence is the record of a run, and MLflow holds a copy for its viewer,
its search and its assessments. ``copy_run`` reads a sealed run from a store,
checks its chain, and writes it to the place that the telemetry policy
approves:

- an MLflow tracking server, at an http or https URL;
- MLflow's tables in a database, such as ``postgresql://host/mlflow``;
- MLflow's tables in the store's own SQLite file, for an ``mlflow``
  destination with no endpoint.

``MLFLOW_TRACKING_URI`` names the place, and the policy's ``mlflow``
destination that matches it gives the level, which is what the copy holds.
With the variable unset, the copy goes to the store's own file. A place that
no destination matches gets nothing.

The copy makes an MLflow experiment named for the run's experiment, such as
``acme/demo/dev/revenue-agent``, and an MLflow run tagged with provrun's run
ID. MLflow picks its own run ID, so the tag is how provevi finds the copy
again. Copying a run again deletes the earlier copy first, so the copy can be
rebuilt at any time.

The run's trace keeps provrun's trace and span IDs. Each step with a span
becomes a span from its start to its end, and the run is the root span.
provevi writes whole spans through MLflow's store, so MLflow adds nothing of
its own, such as the user name or the git commit, and the copy does not
depend on MLflow's global tracking URI. MLflow is loaded only here, as the
optional extra ``provevi[mlflow]``, with its usage telemetry off.

What the copy holds follows the destination's level, as for any destination
of the policy. At every level it holds the experiment, the run with its tags
and its settings that are names, each input's SHA-256, and the spans with
their names, models and IDs. A name here is a short word with no URL, no
user and password, no ``=`` and no leading ``/``, as a secret or a path
could be written so. ``usage`` adds the token counts and
the counts of calls. ``bodies`` adds every other setting, each input's URIs,
each step's data and URIs, the requests and responses of model calls, and the events that had
no span, as events of the root span.

``prove mlflow RUN`` makes the copy, and ``prove ui`` opens MLflow's viewer
on the store's own file.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .checks import verify
from .errors import ProveviError
from .events import EvidenceEvent, sha256_bytes
from .store import EvidenceStore, FileStore
from .telemetry import TelemetryPolicy, _is_name, attributes_for, distribution_of, normal_endpoint

#: The MLflow minor release that the copy is tested against.
MLFLOW_TESTED = "3.17"
#: The MLflow run tag that holds provrun's run ID.
RUN_TAG = "provrun.run_id"

# Set before MLflow is imported: no usage telemetry, no note to agents, and
# no links printed to standard output.
_MLFLOW_ENV = {"MLFLOW_DISABLE_TELEMETRY": "true", "MLFLOW_DISABLE_AGENT_HINT": "1",
               "MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT": "true"}
# The GenAI operation of each step that has one, as provrun's spans name it.
_OPERATION = {"model_called": "chat", "tool_called": "execute_tool", "sandbox_started": "invoke_agent"}
# Step data that becomes a span attribute, with the attribute's name.
_ATTRIBUTES = {
    "model": "gen_ai.request.model",
    "tool": "gen_ai.tool.name",
    "agent": "gen_ai.agent.name",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
}
# The IDs and times that provrun stamps on an event's data.
_STAMPED = {"trace_id", "span_id", "parent_span_id", "started_at"}
# The event type of a library's span, which provrun records.
SPAN = "span_recorded"
# The settings of a run that become MLflow params.
_PARAMS = ("agent", "backend", "model", "mode", "tools", "limits")
# A run ID that is safe in an MLflow filter: no quote, no space.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}")
# Variables that send a connection somewhere other than its URL says. The
# HTTP client of MLflow reads the proxies, and libpq reads the PG variables,
# from the process's environment.
_PROXIES = ("http_proxy", "https_proxy", "all_proxy")
_PG = ("PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGSSLMODE",
       "PGREQUIRESSL")
# Variables that change which servers the copy trusts, or load an
# authentication plugin, which runs code of its own.
_TRUST = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "MLFLOW_TRACKING_SERVER_CERT_PATH", "MLFLOW_TRACKING_AUTH")
_MYSQL = ("MYSQL_HOST", "MYSQL_TCP_PORT", "MYSQL_UNIX_PORT", "MYSQL_PWD")


@dataclass(frozen=True)
class MlflowCopy:
    """Where a run was copied. tracking_uri holds no user or password."""
    run_id: str
    destination: str
    level: str
    tracking_uri: str
    experiment_id: str
    mlflow_run_id: str
    trace_id: str | None
    spans: int
    assessments: int = 0
    replaced: list[str] = field(default_factory=list)


def tracking_target(policy: TelemetryPolicy, store: EvidenceStore,
                    environ: Mapping[str, str] | None = None) -> tuple[str, str, str]:
    """The destination's name, its level and the tracking URI for a copy,
    from MLFLOW_TRACKING_URI in environ. Raises ProveviError when the policy
    approves no mlflow destination for it, or when the variable is unset and
    the store has no file of its own."""
    environ = os.environ if environ is None else environ
    url = environ.get("MLFLOW_TRACKING_URI") or None
    dest = policy.match_destination("mlflow", url)
    if dest is None:
        where = f"MLFLOW_TRACKING_URI {_shown(url)}" if url else "the store's own file (MLFLOW_TRACKING_URI is unset)"
        raise ProveviError(f"the telemetry policy approves no mlflow destination for {where}")
    if url is None:
        path = getattr(store, "path", None)
        if isinstance(store, FileStore) or path is None:
            raise ProveviError("the file store has no file for MLflow's tables; set MLFLOW_TRACKING_URI, "
                               "or use a sqlite store")
        url = f"sqlite:///{path}"
    return dest.name, dest.receives, url


def copy_run(store: EvidenceStore, run_id: str, policy: TelemetryPolicy, *,
             environ: Mapping[str, str] | None = None, folder: Path | None = None) -> MlflowCopy:
    """Copy one sealed run into MLflow, at the place and the level that the
    policy approves for MLFLOW_TRACKING_URI. folder is the run's evidence
    folder, which the file store needs. Raises ProveviError if the run does
    not verify, if the policy approves no place, or if MLflow cannot write
    there."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ProveviError(f"{run_id!r} is not a run ID that MLflow can be searched for")
    record = store.read_run(run_id, folder)
    problems = verify(record, run_id)
    if problems:
        raise ProveviError(f"{run_id} does not verify, so it is not copied: " + "; ".join(problems[:3]))
    events = [EvidenceEvent.from_line(line) for line in record.lines()]
    name, level, url = tracking_target(policy, store, environ)
    # A library's span goes in only if the policy lets that library feed
    # this destination, as provrun sends it only to such places.
    events = [e for e in events if e.type != SPAN or _feeds(policy, e.data.get("library"), name)]
    shown = _shown(url)
    refuse_redirects(url)
    sqlite_file = _sqlite_path(url)
    if sqlite_file is not None:
        _wal(sqlite_file)  # before MLflow makes its tables, so the file is in WAL mode from the start
    # MLflow prices a model call with LiteLLM when it can import it, and
    # importing LiteLLM fetches a price table from the network and reads a
    # .env file. Unless the process loaded LiteLLM already, MLflow uses the
    # price table it ships instead.
    blocked = "litellm" not in sys.modules
    with _lock(store), _mlflow_env(sqlite_file):
        if blocked:
            sys.modules["litellm"] = None
        try:
            client = _client(url)
            copy = _write(client, events, record, level, sqlite_file)
        except ProveviError:
            raise
        except Exception as e:  # MLflow's errors, and SQLAlchemy's and requests' errors that pass through it
            raise ProveviError(f"MLflow at {shown} cannot take the copy of {run_id}: "
                               f"{e.__class__.__name__}: {_shown_error(e, url)}") from e
        finally:
            if blocked and sys.modules.get("litellm", 0) is None:
                del sys.modules["litellm"]
    if sqlite_file is not None:
        _wal(sqlite_file)
    return MlflowCopy(run_id=run_id, destination=name, level=level, tracking_uri=shown, **copy)


def _feeds(policy: TelemetryPolicy, library: Any, destination: str) -> bool:
    """Whether the policy lets the library of a span feed the destination,
    by the span's scope name or by the distribution of that module, as
    provrun decides where to send the span."""
    if not isinstance(library, str) or not library:
        return False
    names = {library, distribution_of(library.replace("-", "_"))} - {None}
    return any(d.name == destination for n in names for d in policy.destinations_for(n))


def _shown_error(e: Exception, url: str) -> str:
    """The error's text with no password of url in it."""
    from urllib.parse import urlsplit

    text = str(e).splitlines()[0][:500] if str(e) else ""
    try:
        password = urlsplit(url).password
    except ValueError:
        password = None
    return text.replace(password, "<password>") if password else text


def refuse_redirects(url: str, environ: Mapping[str, str] | None = None) -> None:
    """Raise ProveviError if a variable in environ, by default os.environ,
    would send a copy to url somewhere else, or change which servers it
    trusts. provrun calls it as a run starts, so that the run records a
    copy that would be refused."""
    env = os.environ if environ is None else environ
    scheme = url.split(":", 1)[0].lower().split("+", 1)[0]
    if scheme in ("http", "https"):
        found = sorted(k for k, v in env.items() if k.lower() in _PROXIES and v)
        if str(env.get("MLFLOW_TRACKING_INSECURE_TLS", "")).lower() in ("true", "1"):
            found.append("MLFLOW_TRACKING_INSECURE_TLS")
        found += sorted(k for k in _TRUST if env.get(k))
    elif scheme == "postgresql":
        found = sorted(k for k in _PG if env.get(k))
    elif scheme == "mysql":
        found = sorted(k for k in _MYSQL if env.get(k))
    else:
        found = []
    if found:
        raise ProveviError(f"{', '.join(found)} would send the copy somewhere other than the approved place, or change which servers it trusts; "
                           "unset them to copy")


@contextlib.contextmanager
def _lock(store: EvidenceStore):
    """One copy at a time from a store, so that two copies of a run do not
    both survive."""
    path = getattr(store, "path", None)
    if path is not None:
        lock = Path(path).with_name(Path(path).name + ".mlflow-lock")
    else:
        from .runs import default_runs_root

        lock = Path(store.root if store.root is not None else default_runs_root()) / ".mlflow-lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextlib.contextmanager
def _mlflow_env(sqlite_file: Path | None):
    """The variables that MLflow reads, set for the copy and put back after.
    A SQL store makes a Default experiment, and without a root it records the
    current folder as its artifact place."""
    want = dict(_MLFLOW_ENV)
    # A proxy that the system settings name, as macOS and Windows do, is used
    # with no variable set. A no_proxy of * makes the HTTP client skip every proxy.
    want.update(NO_PROXY="*", no_proxy="*")
    want["_MLFLOW_SERVER_ARTIFACT_ROOT"] = ((sqlite_file.parent / "mlflow-artifacts").as_uri()
                                            if sqlite_file is not None else "mlflow-artifacts:/")
    old = {k: os.environ.get(k) for k in want}
    os.environ.update(want)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _shown(url: str | None) -> str:
    """url with no user or password, for messages and the result."""
    if url is None:
        return "<unset>"
    try:
        return normal_endpoint(url)
    except ProveviError:
        return "<not shown: not a URL that a policy can name>"


def _sqlite_path(url: str) -> Path | None:
    scheme, _, rest = url.partition(":")
    if scheme.lower().split("+", 1)[0] != "sqlite":  # with or without a driver, as sqlite+pysqlite
        return None
    path = rest.removeprefix("///")
    if not path.startswith("/"):
        raise ProveviError("write a sqlite tracking URI with an absolute path, as sqlite:////srv/mlflow.db")
    return Path(path)


def _wal(path: Path) -> None:
    """Put the file in WAL mode, and refuse it if it cannot be. provevi uses
    SQLite's WAL mode only, and the mode stays in the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    finally:
        conn.close()
    if str(mode).lower() != "wal":
        raise ProveviError(f"{path} is in SQLite's {mode} mode, not WAL; provevi uses WAL only")


def _client(url: str):
    loaded = "mlflow" in sys.modules
    try:
        import mlflow
        from mlflow import MlflowClient
    except ImportError as e:
        raise ProveviError("the copy in MLflow needs MLflow; install provevi[mlflow]") from e
    if not mlflow.__version__.startswith(MLFLOW_TESTED + "."):
        raise ProveviError(f"the copy is tested with MLflow {MLFLOW_TESTED}, and MLflow {mlflow.__version__} "
                           "is installed")
    if loaded:
        # MLflow reads MLFLOW_DISABLE_TELEMETRY at import; one imported earlier
        # must read it again.
        from mlflow.telemetry.client import set_telemetry_client

        set_telemetry_client()
    return MlflowClient(tracking_uri=url)


def _ns(time: str) -> int:
    t = datetime.fromisoformat(time)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    delta = t - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


# What a name may not hold in the copy: a URL, a user and password, a
# setting such as token=..., or a path from the root.
_NOT_A_NAME = re.compile(r"://|^[^/@]*:[^/@]*@|=|^/")


# A library span's name below bodies, by provrun's rule: one word, or two,
# such as "chat gpt-4o".
_SPAN_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,200}( [A-Za-z0-9_.:-]{1,200})?")


def _name(value: Any) -> bool:
    """A name, as the policy's levels say, that holds no URL, no password,
    no setting and no path from the root, as a secret or a path could pass
    the policy's test of a name."""
    if not _is_name(value):
        return False
    texts = [value] if isinstance(value, str) else value if isinstance(value, (list, tuple)) else []
    return not any(_NOT_A_NAME.search(t) for t in texts)


def _keep(value: Any, level: str) -> bool:
    """Whether a value of the run, such as a tag or a param, goes into a copy
    at level. A name goes in at every level; anything else only with bodies."""
    return level == "bodies" or _name(value)


def _write(client, events: list[EvidenceEvent], record, level: str, sqlite_file: Path | None) -> dict:
    created, ending = events[0], events[-1]
    data = created.data
    # Each part of the experiment's name goes in below bodies only if it is a
    # name, as an agent's name may be any text.
    experiment = "/".join(str(data.get(k, "")) if level == "bodies" or _name(data.get(k)) else k
                          for k in ("org", "domain", "env", "agent"))
    exp = client.get_experiment_by_name(experiment)
    if exp is None:
        place = (sqlite_file.parent / "mlflow-artifacts").as_uri() if sqlite_file is not None else None
        experiment_id = client.create_experiment(experiment, artifact_location=place)
    else:
        experiment_id = exp.experiment_id
    replaced = _delete_earlier(client, experiment_id, created.run_id)

    tags = {RUN_TAG: created.run_id, "provrun.org": data.get("org"), "provrun.domain": data.get("domain"),
            "provrun.env": data.get("env"), "provrun.actor": created.actor, "provrun.mode": data.get("mode")}
    tags = {k: str(v) for k, v in tags.items() if v is not None and _keep(v, level)}
    run = client.create_run(experiment_id, start_time=_ns(created.time) // 1_000_000, tags=tags,
                            run_name=created.run_id)
    mlflow_run_id = run.info.run_id
    trace = created.data.get("trace_id") or ending.data.get("trace_id")
    try:
        return _fill(client, experiment_id, mlflow_run_id, events, record, level, replaced)
    except BaseException:
        # Leave no part of a copy: the trace has a known ID, as it may not be
        # linked to the run yet.
        _remove(client, experiment_id, mlflow_run_id, f"tr-{trace}" if isinstance(trace, str) else None)
        raise


def _fill(client, experiment_id: str, mlflow_run_id: str, events: list[EvidenceEvent], record, level: str,
          replaced: list[str]) -> dict:
    created, ending = events[0], events[-1]
    data = created.data
    spec = data.get("spec") if isinstance(data.get("spec"), dict) else {}
    params = {k: spec.get(k, data.get(k)) for k in _PARAMS if spec.get(k, data.get(k)) is not None}
    params = {k: v if isinstance(v, str) else json.dumps(v, sort_keys=True) for k, v in params.items()}
    from mlflow.entities import Dataset, DatasetInput, Param

    client.log_batch(mlflow_run_id, params=[Param(k, v) for k, v in sorted(params.items()) if _keep(v, level)],
                     metrics=_metrics(events, created.time) if level != "names" else [])
    datasets = [_dataset(e, Dataset, level) for e in events if e.type == "input_staged" and e.refs]
    if datasets:
        client.log_inputs(mlflow_run_id, datasets=[DatasetInput(d) for d in datasets])

    trace_id, n = _write_trace(client, experiment_id, mlflow_run_id, events, record, level)
    assessed = _write_assessments(client, trace_id, record, events, level) if trace_id else 0
    status = "FINISHED" if ending.type == "run_completed" else "FAILED"
    client.set_terminated(mlflow_run_id, status=status, end_time=_ns(ending.time) // 1_000_000)
    return {"experiment_id": experiment_id, "mlflow_run_id": mlflow_run_id, "trace_id": trace_id, "spans": n,
            "assessments": assessed, "replaced": replaced}


def _delete_earlier(client, experiment_id: str, run_id: str) -> list[str]:
    """Delete the copies of run_id made before, with their traces. run_id
    holds no quote, as copy_run checks, and each match is checked again."""
    earlier = [r for r in client.search_runs([experiment_id], filter_string=f"tags.`{RUN_TAG}` = '{run_id}'")
               if r.data.tags.get(RUN_TAG) == run_id]
    for r in earlier:
        traces = client.search_traces(locations=[experiment_id], run_id=r.info.run_id)
        _remove(client, experiment_id, r.info.run_id, *[t.info.trace_id for t in traces])
    return [r.info.run_id for r in earlier]


def _remove(client, experiment_id: str, mlflow_run_id: str, *trace_ids: str | None) -> None:
    """Delete a copy's traces and its run. In a database the run is deleted
    for good, with its params, tags, metrics and inputs. A tracking server
    only marks it deleted, until its owner runs mlflow gc."""
    ids = [t for t in trace_ids if t]
    if ids:
        with contextlib.suppress(Exception):
            client.delete_traces(experiment_id, trace_ids=ids)
    with contextlib.suppress(Exception):
        client.delete_run(mlflow_run_id)
    store = client._tracking_client.store
    if hasattr(store, "_hard_delete_run"):  # MLflow's SQL store; mlflow gc calls the same
        store._hard_delete_run(mlflow_run_id)
        _delete_inputs(store, experiment_id, mlflow_run_id)


def _delete_inputs(store, experiment_id: str, mlflow_run_id: str) -> None:
    """Delete a run's inputs, which MLflow's hard delete leaves, and each
    dataset of the experiment that no input names any more. MLflow keeps one
    dataset row for a name and digest, and a later run would reuse it."""
    from mlflow.store.tracking.dbmodels.models import SqlDataset, SqlInput, SqlInputTag

    with store.ManagedSessionMaker() as session:
        inputs = session.query(SqlInput.input_uuid).filter(SqlInput.destination_id == mlflow_run_id)
        session.query(SqlInputTag).filter(SqlInputTag.input_uuid.in_(inputs.scalar_subquery())).delete(
            synchronize_session=False)
        session.query(SqlInput).filter(SqlInput.destination_id == mlflow_run_id).delete(synchronize_session=False)
        named = session.query(SqlInput.source_id).filter(SqlInput.source_type == "DATASET")
        session.query(SqlDataset).filter(SqlDataset.experiment_id == int(experiment_id),
                                         SqlDataset.dataset_uuid.not_in(named.scalar_subquery())).delete(
            synchronize_session=False)


def _metrics(events: list[EvidenceEvent], start: str) -> list:
    from mlflow.entities import Metric

    totals = {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0}
    for e in events:
        if e.type == "model_called":
            totals["model_calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                v = e.data.get(k)
                if isinstance(v, int) and not isinstance(v, bool):
                    totals[k] += v
        elif e.type == "tool_called":
            totals["tool_calls"] += 1
    ms = _ns(events[-1].time) // 1_000_000
    return [Metric(k, v, ms, 0) for k, v in totals.items()]


def _dataset(event: EvidenceEvent, Dataset, level: str):
    """An input as an MLflow dataset. Its source holds the SHA-256, and with
    bodies its URIs too, as a URI can hold a signed query or a token, as a
    span's refs go only with bodies. Its digest, 32 characters, the longest
    MLflow takes, is of the name and the source, because MLflow shares one
    row for a name and digest among the runs of an experiment."""
    sha = str(event.data.get("sha256") or "")
    pinned = event.refs[1] if len(event.refs) > 1 else event.refs[0]
    name = str(event.data.get("name") or event.refs[0].rsplit("/", 1)[-1])
    if level != "bodies" and not _name(name):
        name = "input"
    source = {"sha256": sha}
    if level == "bodies":
        source.update(uri=event.refs[0], pinned=pinned)
    text = json.dumps(source, sort_keys=True)
    digest = sha256_bytes(json.dumps([name[:500], text]).encode())[:32]
    return Dataset(name=name[:500], digest=digest, source_type="provcat", source=text)


def _write_trace(client, experiment_id: str, mlflow_run_id: str, events: list[EvidenceEvent], record,
                 level: str) -> tuple[str | None, int]:
    """Write the run's spans, and give the trace's MLflow ID and the number of
    spans. A run whose steps recorded no spans has no trace."""
    created, ending = events[0], events[-1]
    trace = created.data.get("trace_id") or ending.data.get("trace_id")
    root = created.data.get("span_id") or ending.data.get("span_id")
    if not isinstance(trace, str) or not isinstance(root, str):
        return None, 0
    from mlflow.entities import Span

    root_attrs = {"provrun.run_id": created.run_id, "provrun.step": "run", "provrun.component": created.component,
                  "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": created.data.get("agent")}
    loose = [e for e in events if e.data.get("span_id") is None or e.data.get("trace_id") != trace]
    protos = [_proto(trace, root, None, "run", _ns(created.time), _ns(ending.time), root_attrs, level,
                     error=ending.type != "run_completed",
                     span_events=loose if level == "bodies" else [])]
    for e in events:
        if e.data.get("trace_id") != trace or e.data.get("span_id") in (None, root):
            continue
        if e.type == SPAN:
            protos.append(_library_proto(trace, e, record, level))
            continue
        attrs = {"provrun.run_id": e.run_id, "provrun.step": e.type, "provrun.component": e.component}
        if e.type in _OPERATION:
            attrs["gen_ai.operation.name"] = _OPERATION[e.type]
        for key, name in _ATTRIBUTES.items():
            if key in e.data:
                attrs[name] = e.data[key]
        if level == "bodies":
            rest = {k: v for k, v in e.data.items() if k not in _STAMPED and k not in _ATTRIBUTES}
            attrs["provrun.data"] = json.dumps(rest, sort_keys=True, default=str)
            attrs["provrun.refs"] = json.dumps(list(e.refs))
            body = _payload(record, e.payload)
            if isinstance(body, dict) and {"request", "response"} <= body.keys():
                attrs["mlflow.spanInputs"] = json.dumps(body["request"])
                attrs["mlflow.spanOutputs"] = json.dumps(body["response"])
            elif body is not None:
                attrs["mlflow.spanOutputs"] = json.dumps(body)
        # A step with no start time is a span of no length, so that its
        # children keep their parent.
        start = _ns(e.data["started_at"]) if e.data.get("started_at") else _ns(e.time)
        protos.append(_proto(trace, e.data["span_id"], e.data.get("parent_span_id"), e.type,
                             start, _ns(e.time), attrs, level, error=e.type == "step_failed"))
    # With no location, the trace's MLflow ID is tr- and provrun's trace ID,
    # which is the ID that a tracking server gives the same spans.
    spans = [Span.from_otel_proto(p) for p in protos]
    client._tracking_client.store.log_spans(experiment_id, spans)
    trace_id = spans[0].trace_id
    got = _read_back(client, trace_id, len(spans))
    if got is None or len(got.data.spans) != len(spans):
        raise ProveviError(f"MLflow kept {0 if got is None else len(got.data.spans)} of the {len(spans)} spans "
                           f"of {created.run_id}")
    client.link_traces_to_run([trace_id], mlflow_run_id)
    client.set_trace_tag(trace_id, RUN_TAG, created.run_id)
    return trace_id, len(spans)


def _read_back(client, trace_id: str, n: int, *, wait: float = 30.0):
    """The trace once MLflow holds all n spans of it, or what it holds when
    wait seconds have passed. A tracking server writes spans after it answers."""
    import time

    from mlflow.exceptions import MlflowException

    end = time.monotonic() + wait
    while True:
        try:
            got = client.get_trace(trace_id)
        except MlflowException:
            got = None
        if (got is not None and len(got.data.spans) >= n) or time.monotonic() > end:
            return got
        time.sleep(0.2)


_SOURCES = ("HUMAN", "LLM_JUDGE", "CODE")


def _write_assessments(client, trace_id: str, record, events: list[EvidenceEvent], level: str) -> int:
    """Copy the store's assessments of the run and of its spans onto the
    trace. A name and a value that is a name go in at every level; a
    rationale and any other value only with bodies."""
    rows = getattr(record, "assessments", lambda: [])()
    if not rows:
        return 0
    from mlflow.entities import AssessmentSource, Feedback

    spans = {e.data.get("span_id") for e in events if isinstance(e.data.get("span_id"), str)}
    n = 0
    for row in rows:
        if row["target_kind"] == "span" and row["target_id"] not in spans:
            continue
        if level != "bodies" and not _name(row["name"]):
            continue  # an assessment's name is all that marks it, so one that is not a name stays out
        try:
            value = json.loads(row["value"]) if row["value"] is not None else None
        except ValueError:
            value = row["value"]
        if level != "bodies" and not _value_shown(value, level):
            value = None
        who = str(row["created_by"] or "provevi")
        if level != "bodies" and not _name(who):
            who = "provevi"
        source = str(row["source"] or "").upper()
        feedback = Feedback(name=row["name"], value=value,
                            source=AssessmentSource(source_type=source if source in _SOURCES else "CODE",
                                                    source_id=who),
                            rationale=row["rationale"] if level == "bodies" else None,
                            span_id=row["target_id"] if row["target_kind"] == "span" else None,
                            metadata={"provevi.assessment_id": str(row["id"])})
        client._tracing_client.log_assessment(trace_id, feedback)  # MlflowClient has no public call for it
        n += 1
    return n


def _value_shown(value: Any, level: str) -> bool:
    """Whether an assessment's value goes in below bodies: a word or a yes or
    no at every level, and a number from usage up, as a score is a number."""
    if isinstance(value, bool) or (isinstance(value, str) and _name(value)):
        return True
    return level == "usage" and isinstance(value, (int, float)) and value == value and abs(value) != float("inf")


def _payload(record, digest: str | None) -> Any:
    if not digest:
        return None
    raw = record.payload_bytes(digest)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _library_proto(trace: str, e: EvidenceEvent, record, level: str):
    """A library's span from its span_recorded event: its names and usage
    from the event, and with bodies its other attributes and its events from
    the payload."""
    data = e.data
    attrs = dict(data.get("attributes") or {})
    attrs.update({"provrun.run_id": e.run_id, "provrun.library": data.get("library")})
    name = str(data.get("name") or "span")
    if level != "bodies" and not (_SPAN_NAME.fullmatch(name) and all(_name(w) for w in name.split(" "))):
        name = "span"
    body = _payload(record, e.payload) if level == "bodies" else None
    span_events = []
    if isinstance(body, dict):
        extra = dict(body.get("attributes") or {})
        name = str(extra.pop("span.name", name))
        attrs.update(extra)
        span_events = [ev for ev in body.get("events") or [] if isinstance(ev, dict)]
        if body.get("links"):
            attrs["provrun.links"] = json.dumps(body["links"], sort_keys=True, default=str)
    start = data.get("start_time") if isinstance(data.get("start_time"), int) else _ns(e.time)
    end = data.get("end_time") if isinstance(data.get("end_time"), int) else start
    p = _proto(trace, data["span_id"], data.get("parent_span_id"), name, start, max(start, end), attrs, level,
               error=data.get("status") == "ERROR")
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    for ev in span_events:
        added = p.events.add(time_unix_nano=ev["timestamp"] if isinstance(ev.get("timestamp"), int) else start,
                             name=str(ev.get("name") or "event"))
        for k, v in (ev.get("attributes") or {}).items():
            added.attributes.append(KeyValue(key=str(k), value=AnyValue(
                string_value=v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str))))
    return p


def _proto(trace: str, span: str, parent: str | None, name: str, start: int, end: int, attrs: dict, level: str,
           *, error: bool = False, span_events: list[EvidenceEvent] = ()):
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1 import trace_pb2

    def value(v: Any) -> AnyValue:
        if isinstance(v, bool):
            return AnyValue(bool_value=v)
        if isinstance(v, int):
            return AnyValue(int_value=v)
        if isinstance(v, float):
            return AnyValue(double_value=v)
        return AnyValue(string_value=v if isinstance(v, str) else json.dumps(v, sort_keys=True, default=str))

    p = trace_pb2.Span(trace_id=bytes.fromhex(trace), span_id=bytes.fromhex(span), name=name,
                       kind=trace_pb2.Span.SPAN_KIND_INTERNAL, start_time_unix_nano=start, end_time_unix_nano=end)
    if parent:
        p.parent_span_id = bytes.fromhex(parent)
    kept = {k: v for k, v in attrs.items() if v is not None}
    for k, v in attributes_for(kept, level).items():
        if level != "bodies" and isinstance(v, (str, list, tuple)) and not _name(v):
            continue  # a value that the policy calls a name, but holds a URL, a setting or a path
        p.attributes.append(KeyValue(key=k, value=value(v)))
    for e in span_events:
        ev = p.events.add(time_unix_nano=_ns(e.time), name=e.type)
        ev.attributes.append(KeyValue(key="provrun.component", value=value(e.component)))
        ev.attributes.append(KeyValue(key="provrun.data", value=value(e.data)))
        ev.attributes.append(KeyValue(key="provrun.refs", value=value(list(e.refs))))
    p.status.code = trace_pb2.Status.STATUS_CODE_ERROR if error else trace_pb2.Status.STATUS_CODE_OK
    return p



def ui_command(path: Path, port: int) -> tuple[list[str], dict[str, str]]:
    """The command that serves MLflow's viewer on the store's file at path,
    on 127.0.0.1 only, and its environment. The file stays in WAL mode."""
    path = Path(path)
    _wal(path)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MLFLOW_", "OTEL_"))}
    env.update(_MLFLOW_ENV)
    command = [str(Path(sys.executable).with_name("mlflow")), "server", "--host", "127.0.0.1", "--port", str(port),
               "--backend-store-uri", f"sqlite:///{path}",
               "--default-artifact-root", (path.parent / "mlflow-artifacts").as_uri()]
    return command, env
