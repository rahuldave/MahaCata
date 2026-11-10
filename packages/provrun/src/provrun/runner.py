"""Run one agent spec from start to end.

Before the run folder is made, the broker decides the run's experiment, if
it names one, and provrun pins it if the broker allows it. Then the steps,
each of which writes its evidence:

1. Open the run context, and record the broker's decision on the experiment.
   A refusal fails the run here.
2. Ask the broker for each input.
3. Resolve each grant to a snapshot.
4. Stage the snapshots.
5. Run the backend.
6. Check that the inputs did not change, and collect the outputs.
7. Finish the run and write the bundle.

A failure at any step writes run_failed, and the bundle is still written.

A program runs a run file the way ``provr run`` does::

    outcome = run_file_sync(RunFile.load("run.yaml"), catalog=Catalog.open())
    outcome.to_dict()

``run_agent`` takes the parts one by one, and the broker, resolver, stager
and backend can be passed in.
"""
from __future__ import annotations

import asyncio
import re
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from provcat import Catalog, ProvcatError
from provevi import EvidenceStore, default_runs_root
from provevi.telemetry import TelemetryPolicy

from .access import AccessDenied, Broker, Grant, RunScope, StubBroker
from .backends import Backend, RunResult, load_backend
from . import otel
from .context import Bundle, RunContext, check_segment, evidence_mode, new_run_id
from .errors import RunSettingError
from .outputs import Artifact, Outputter
from .resolve import ProvcatResolver, Resolver
from .spec import AgentSpec, RunFile
from .stagers import InProcessStager, Stager
from .telemetry import Telemetry, load_policy


@dataclass
class RunOutcome:
    run_id: str
    status: str  # "completed" or "failed"
    folder: Path
    bundle: Bundle
    result: RunResult | None = None
    artifacts: list[Artifact] | None = None
    error: BaseException | None = None
    mlflow: dict | None = None  # where the sealed run was copied in MLflow, when the policy approves a place

    def to_dict(self) -> dict:
        """The summary that ``provr run`` prints, as plain values. It has
        ``mlflow`` only when the run was to be copied into MLflow."""
        d = {"run_id": self.run_id, "status": self.status, "folder": str(self.folder),
             "bundle": self.bundle.uri, "pinned": self.bundle.pinned, "note": self.bundle.note,
             "error": f"{self.error.__class__.__name__}: {self.error}" if self.error else None}
        if self.mlflow is not None:
            d["mlflow"] = self.mlflow
        return d


def input_name(uri: str, given: str | None) -> str:
    if given:
        return given
    from provcat import parse

    u = parse(uri)
    return u.segments[-1] if u.segments else u.authority


async def run_agent(agent: AgentSpec, *, catalog: Catalog, org: str, domain: str, env: str, actor: str,
                    allow: list[str] | None = None, broker: Broker | None = None,
                    resolver: Resolver | None = None, stager: Stager | None = None,
                    backend: Backend | None = None, runs_root: Path | None = None,
                    mode: str | None = None, bundle_scheme: str | None = None,
                    store: EvidenceStore | None = None, policy: TelemetryPolicy | str | Path | None = None,
                    telemetry: dict | None = None, feed: Any = None, experiment: str | None = None,
                    parent_run: str | None = None) -> RunOutcome:
    """Run agent once. With no broker, a StubBroker allows the inputs of the
    spec and the URIs in allow. The evidence goes through store, a provevi
    store, by default the file store in the run folder.

    policy is the operator's telemetry policy, or the path of its file; by
    default, the file that PROVRUN_TELEMETRY_POLICY names, else a policy that
    approves nothing. telemetry, a run file's telemetry section, narrows it.
    provrun.telemetry says what a run does with the policy.

    A bad setting, a telemetry section that widens the policy, or an unknown
    backend raises RunSettingError before the run folder is made; so does a
    telemetry problem in strict mode. Any later error fails the run, and the
    outcome holds it. feed, a provrun.live.LiveFeed, gets each event and each
    piece of a streamed reply as they come; start_run makes one.

    experiment is the URI of the experiment that the run belongs to. The run
    asks the broker for it first, in the mode pinned_read, and records the
    decision once it opens. A refusal is recorded as access_denied, and the
    run fails without reading the experiment. Otherwise the run
    pins it through catalog, so the catalog's checks and hooks see it, and
    run_created records the URI, the pin and the SHA-256 of its file. An
    experiment that cannot be pinned raises RunSettingError before the run
    folder is made. parent_run is the ID of the run that started this one,
    which run_created records too. It must be a run in the same evidence
    store, or under runs_root for the file store, or RunSettingError is
    raised; a store that cannot read runs back is not checked."""
    if parent_run is not None and (not isinstance(parent_run, str) or not _RUN_ID.fullmatch(parent_run)):
        raise RunSettingError(f"parent_run must be a run ID, such as run-20260101T000000Z-0a1b2c3d; got {parent_run!r}")
    tel = Telemetry.prepare(load_policy(policy), telemetry, evidence_mode(env, mode))
    try:
        with tel.applied(store, close=False):
            # The environment is set now, before the backend imports its library.
            outcome = await _run_agent(agent, tel, catalog=catalog, org=org, domain=domain, env=env, actor=actor,
                                       allow=allow, broker=broker, resolver=resolver, stager=stager,
                                       backend=backend, runs_root=runs_root, mode=mode,
                                       bundle_scheme=bundle_scheme, store=store, feed=feed,
                                       experiment=experiment, parent_run=parent_run)
    finally:
        if tel.routes is not None:
            # Sending the last spans can take seconds, so it goes in a thread too.
            await _off_loop(tel.routes.close)
    if tel.routes is not None and tel.routes.mlflow is not None:
        # The run is sealed, and the user's variables are back.
        outcome.mlflow = await _off_loop(tel.routes.copy, outcome.run_id, store, outcome.folder / "evidence")
    return outcome


async def _run_agent(agent: AgentSpec, tel: Telemetry, *, catalog: Catalog, org: str, domain: str, env: str,
                     actor: str, allow, broker, resolver, stager, backend, runs_root, mode, bundle_scheme,
                     store, feed=None, experiment=None, parent_run=None) -> RunOutcome:
    if backend is None:
        try:
            backend = load_backend(agent.backend)
        except ValueError as e:
            raise RunSettingError(str(e)) from e
    for i in agent.inputs:
        if i.mode == "pinned_read":  # the agent sees an input, and pinned_read is a read that it never sees
            raise RunSettingError(f"the input {i.uri} asks for pinned_read, which is only for provrun's own reads")
    if broker is None:
        allow = list(allow or []) + [i.uri for i in agent.inputs] + _own_experiment(experiment)
        broker = StubBroker(allow, physical_schemes=catalog.stores.keys())
    for name, value in (("org", org), ("domain", domain), ("env", env)):
        check_segment(name, value)  # before the broker or the catalog hears of the run
    run_id = new_run_id()
    scope = RunScope(run_id, org, domain, env, actor)
    lineage, grant = await _lineage(catalog, experiment, parent_run, store, runs_root, broker, scope)
    # The root span of the run: each top-level step is its child, so the run is
    # one trace. run_created and the event that ends the run record its IDs.
    with otel.run(run_id, agent.name) as root:
        ids = otel.ids(root)
        trace = {k: ids[k] for k in ("trace_id", "span_id") if k in ids}
        if feed is not None:
            feed.level = tel.policy.live_level()
        ctx = RunContext.open(org=org, domain=domain, env=env, actor=actor, broker=broker,
                              resolver=resolver or ProvcatResolver(catalog), catalog=catalog,
                              runs_root=runs_root, mode=mode, bundle_scheme=bundle_scheme, store=store,
                              run_id=run_id, trace=trace,
                              data={"agent": agent.name, "backend": agent.backend, "spec": agent.to_dict(),
                                    "telemetry": {**tel.record(), "live": _live(tel, feed)}, **lineage},
                              sinks=[feed] if feed is not None else None)
        ctx.telemetry = tel
        if feed is not None:
            feed.writer = ctx.recorder.writer
            ctx.on_text = feed.text
        tel.attach(ctx.recorder, trace.get("trace_id"))
        outcome, error = await _steps(ctx, agent, stager, backend, grant)
        if error is not None:
            root.set_status(otel.ERROR, f"{error.__class__.__name__}: {error}")
    if error is not None and not isinstance(error, Exception):
        raise error  # a cancel or an interrupt goes on after the run is recorded
    return outcome


_RUN_ID = re.compile(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}")


async def _lineage(catalog: Catalog, experiment: str | None, parent_run: str | None,
                   store: EvidenceStore | None, runs_root: Path | None, broker: Broker,
                   scope: RunScope) -> tuple[dict, Grant | None]:
    """What run_created records of where the run belongs: the experiment, its
    pin and the SHA-256 of its file, and the parent run, each if given, with
    the broker's decision for the experiment. The broker decides the
    experiment before it is read, and the parent run must be one that the
    run's store holds. A refused experiment is not read, and run_created does
    not name it: the run records the refusal once it opens, and fails."""
    out: dict = {}
    grant = None
    if experiment is not None:
        from provevi import ProveviError, read_experiment

        try:
            grant = await _off_loop(broker.decide_early, scope, experiment, "pinned_read", "experiment")
        except AccessDenied as e:
            if not isinstance(e.grant, Grant):
                raise RunSettingError(f"the broker refused the experiment with no grant: {e}") from e
            # A deny, of this experiment, whatever the broker's grant says.
            grant = replace(e.grant, decision="deny", uri=experiment, mode="pinned_read")
        except Exception as e:  # such as a broker that reads a field a RunScope does not have
            raise RunSettingError(f"the broker could not decide the experiment: {e.__class__.__name__}: {e}") from e
        if not isinstance(grant, Grant) or (grant.uri, grant.mode) != (experiment, "pinned_read"):
            raise RunSettingError(f"the broker answered for another request than the experiment {experiment}: "
                                  f"{grant!r}")
        if grant.granted:
            try:
                pinned = await _off_loop(read_experiment, catalog, experiment)
            except (ProveviError, ProvcatError, OSError) as e:  # OSError from a store or a hook outside provcat
                raise RunSettingError(f"cannot pin the experiment {experiment}: {e}") from e
            out.update(experiment=experiment, experiment_pin=pinned.pin, experiment_sha256=pinned.sha256)
    if parent_run is not None:
        await _off_loop(_check_parent, parent_run, store, runs_root)
        out["parent_run"] = parent_run
    return out, grant


def _own_experiment(experiment: str | None) -> list[str]:
    """The run's experiment, for the default broker's allow list. A selector
    other than a commit can name another branch, and so another environment,
    so an experiment with one must be in the allow list itself. So must one
    whose commit does not look like a commit ID, because some stores take a
    branch name there too."""
    if experiment is None or experiment.endswith("/"):  # an allow entry that ends with / allows a whole folder
        return []
    from provcat import parse

    try:
        selector = parse(experiment).selector
    except ProvcatError as e:
        raise RunSettingError(f"the experiment {experiment!r} is not a URI: {e}") from e
    return [experiment] if selector is None or (selector.kind == "commit" and _COMMIT_ID.fullmatch(selector.value)) else []


_COMMIT_ID = re.compile(r"[0-9a-f]{7,64}")  # a commit ID in hex, as lakeFS, git and the object stores write it


def _check_parent(parent_run: str, store: EvidenceStore | None, runs_root: Path | None) -> None:
    from provevi import CannotReadBack, FileStore, ProveviError, RunNotFound

    # The file store writes each run under runs_root, so that is where to look.
    if store is None or isinstance(store, FileStore):
        store = FileStore(runs_root if runs_root is not None else default_runs_root())
    try:
        store.read_run(parent_run)
    except CannotReadBack:
        return
    except RunNotFound as e:
        raise RunSettingError(f"the parent run {parent_run} is not in the evidence store: {e}") from e
    except (ProveviError, OSError) as e:
        raise RunSettingError(f"cannot check the parent run {parent_run}: {e}") from e


def _live(tel: Telemetry, feed: Any) -> dict:
    """What run_created records of the live viewer: the policy's destination
    of kind live and its level, and whether the run has a feed."""
    viewer = next((d for d in tel.policy.destinations.values() if d.kind == "live"), None)
    return {"destination": viewer.name if viewer is not None else None,
            "receives": viewer.receives if viewer is not None else None, "feed": feed is not None}


async def _in_thread(fn, *args) -> tuple[asyncio.Future, bool]:
    """Run a blocking step in a thread, so a server's other requests go on
    meanwhile, and give back its finished future and whether a cancel came.
    A thread cannot be stopped, so a cancel waits for the step to end: no
    later step, such as sealing the run, overlaps it."""
    task = asyncio.ensure_future(asyncio.to_thread(fn, *args))  # to_thread carries the current run and span
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task, cancelled


async def _off_loop(fn, *args):
    """A blocking step in a thread; a cancel goes on once the step has ended."""
    task, cancelled = await _in_thread(fn, *args)
    if cancelled:
        task.exception()  # the step's own error, if any, gives way to the cancel
        raise asyncio.CancelledError
    return task.result()


def _snapshots(ctx: RunContext, agent: AgentSpec) -> list:
    out = []
    for i in agent.inputs:
        grant = ctx.broker.request(ctx, i.uri, i.mode, purpose=agent.name)
        out.append((input_name(i.uri, i.name), ctx.resolver.snapshot(ctx, grant)))
    return out


async def _steps(ctx: RunContext, agent: AgentSpec, stager: Stager | None, backend: Backend,
                 grant: Grant | None = None) -> tuple[RunOutcome, BaseException | None]:
    stager = stager or InProcessStager()
    outcome = RunOutcome(ctx.run_id, "failed", ctx.folder, Bundle(ctx.folder))
    error = None
    run = None
    cancelled = False
    try:
        if grant is not None:  # the experiment's, decided before the run opened; a deny fails the run here
            ctx.broker.record(ctx, grant, purpose="experiment")
        # Fetching, staging, checking and sealing block, so each runs in a thread.
        snapshots = await _off_loop(_snapshots, ctx, agent)
        run = await _off_loop(stager.stage, ctx, snapshots)
        outcome.result = await backend.run(ctx, agent, run)
        if ctx.telemetry is not None:
            ctx.telemetry.end()  # the full scan; a strict run with a problem fails here
        await _off_loop(stager.verify_inputs, ctx, run)
        outcome.artifacts = await _off_loop(Outputter().collect, ctx, run)
    except BaseException as e:  # the run failed; its evidence says why
        error = e
        if ctx.telemetry is not None and isinstance(e, Exception):
            try:  # the end scan still runs, but the run keeps its own error
                ctx.telemetry.end(fail=False)
            except Exception as end_error:
                try:
                    warnings.warn(f"provrun: telemetry: the end check failed: {end_error}", stacklevel=2)
                except Exception:
                    pass
        if run is not None and outcome.artifacts is None:
            # Record what a failed run left in output/, so the bundle holds no file the evidence lacks.
            # A cancel here waits too, and is passed on once the run is sealed.
            collected, cancelled = await _in_thread(Outputter().collect, ctx, run)
            if not collected.exception():
                outcome.artifacts = collected.result()
    sealed, cancelled_sealing = await _in_thread(ctx.finish, error)
    outcome.bundle = sealed.result()
    outcome.status = ctx.status
    outcome.error = error
    if cancelled or cancelled_sealing:
        # The run is sealed with its own error, or none; the cancel goes on to the caller.
        raise asyncio.CancelledError
    return outcome, error


async def run_file(rf: RunFile, *, catalog: Catalog, runs_root: Path | None = None, mode: str | None = None,
                   bundle_scheme: str | Literal[False] | None = None, **parts) -> RunOutcome:
    """Run a checked run file. bundle_scheme, when given, takes the place of
    the run file's; False writes no bundle through the catalog. The other
    keyword arguments (broker, resolver, stager, backend, store, policy, feed,
    parent_run) go to run_agent, with the run file's telemetry section and
    experiment; an experiment argument takes the place of the run file's. Without store, the run file's store setting opens one, and
    without that the file store is used."""
    s = rf.settings
    if parts.get("store") is None and s.store is not None:
        from provevi import open_store

        parts["store"] = open_store(s.store)
    scheme = s.bundle_scheme if bundle_scheme is None else (None if bundle_scheme is False else bundle_scheme)
    return await run_agent(rf.agent, catalog=catalog, org=s.org, domain=s.domain, env=s.env, actor=s.actor,
                           allow=list(s.allow), runs_root=runs_root, mode=mode, bundle_scheme=scheme,
                           telemetry=rf.telemetry, experiment=parts.pop("experiment", rf.experiment), **parts)


def run_file_sync(rf: RunFile, **kw) -> RunOutcome:
    """run_file, for a caller with no event loop running."""
    return asyncio.run(run_file(rf, **kw))
