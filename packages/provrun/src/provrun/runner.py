"""Run one agent spec from start to end.

The steps, each of which writes its evidence:

1. Open the run context.
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
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from provcat import Catalog
from provevi import EvidenceStore
from provevi.telemetry import TelemetryPolicy

from .access import Broker, StubBroker
from .backends import Backend, RunResult, load_backend
from . import otel
from .context import Bundle, RunContext, evidence_mode, new_run_id
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
                    telemetry: dict | None = None, feed: Any = None) -> RunOutcome:
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
    piece of a streamed reply as they come; start_run makes one."""
    tel = Telemetry.prepare(load_policy(policy), telemetry, evidence_mode(env, mode))
    try:
        with tel.applied(store, close=False):
            # The environment is set now, before the backend imports its library.
            outcome = await _run_agent(agent, tel, catalog=catalog, org=org, domain=domain, env=env, actor=actor,
                                       allow=allow, broker=broker, resolver=resolver, stager=stager,
                                       backend=backend, runs_root=runs_root, mode=mode,
                                       bundle_scheme=bundle_scheme, store=store, feed=feed)
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
                     store, feed=None) -> RunOutcome:
    if backend is None:
        try:
            backend = load_backend(agent.backend)
        except ValueError as e:
            raise RunSettingError(str(e)) from e
    if broker is None:
        allow = list(allow or []) + [i.uri for i in agent.inputs]
        broker = StubBroker(allow, physical_schemes=catalog.stores.keys())
    run_id = new_run_id()
    # The root span of the run: each top-level step is its child, so the run is
    # one trace. run_created and the event that ends the run record its IDs.
    with otel.run(run_id, agent.name) as root:
        ids = otel.ids(root)
        trace = {k: ids[k] for k in ("trace_id", "span_id") if k in ids}
        ctx = RunContext.open(org=org, domain=domain, env=env, actor=actor, broker=broker,
                              resolver=resolver or ProvcatResolver(catalog), catalog=catalog,
                              runs_root=runs_root, mode=mode, bundle_scheme=bundle_scheme, store=store,
                              run_id=run_id, trace=trace,
                              data={"agent": agent.name, "backend": agent.backend, "spec": agent.to_dict(),
                                    "telemetry": tel.record()},
                              sinks=[feed] if feed is not None else None)
        ctx.telemetry = tel
        if feed is not None:
            ctx.on_text = feed.text
        tel.attach(ctx.recorder, trace.get("trace_id"))
        outcome, error = await _steps(ctx, agent, stager, backend)
        if error is not None:
            root.set_status(otel.ERROR, f"{error.__class__.__name__}: {error}")
    if error is not None and not isinstance(error, Exception):
        raise error  # a cancel or an interrupt goes on after the run is recorded
    return outcome


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


async def _steps(ctx: RunContext, agent: AgentSpec, stager: Stager | None,
                 backend: Backend) -> tuple[RunOutcome, BaseException | None]:
    stager = stager or InProcessStager()
    outcome = RunOutcome(ctx.run_id, "failed", ctx.folder, Bundle(ctx.folder))
    error = None
    run = None
    cancelled = False
    try:
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
    keyword arguments (broker, resolver, stager, backend, store, policy, feed) go
    to run_agent, with the run file's telemetry section. Without store, the run file's store setting opens one, and
    without that the file store is used."""
    s = rf.settings
    if parts.get("store") is None and s.store is not None:
        from provevi import open_store

        parts["store"] = open_store(s.store)
    scheme = s.bundle_scheme if bundle_scheme is None else (None if bundle_scheme is False else bundle_scheme)
    return await run_agent(rf.agent, catalog=catalog, org=s.org, domain=s.domain, env=s.env, actor=s.actor,
                           allow=list(s.allow), runs_root=runs_root, mode=mode, bundle_scheme=scheme,
                           telemetry=rf.telemetry, **parts)


def run_file_sync(rf: RunFile, **kw) -> RunOutcome:
    """run_file, for a caller with no event loop running."""
    return asyncio.run(run_file(rf, **kw))
