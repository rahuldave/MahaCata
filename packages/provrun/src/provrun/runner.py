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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from provcat import Catalog
from provevi import EvidenceStore

from .access import Broker, StubBroker
from .backends import Backend, RunResult, load_backend
from .context import Bundle, RunContext
from .errors import RunSettingError
from .outputs import Artifact, Outputter
from .resolve import ProvcatResolver, Resolver
from .spec import AgentSpec, RunFile
from .stagers import InProcessStager, Stager


@dataclass
class RunOutcome:
    run_id: str
    status: str  # "completed" or "failed"
    folder: Path
    bundle: Bundle
    result: RunResult | None = None
    artifacts: list[Artifact] | None = None
    error: BaseException | None = None

    def to_dict(self) -> dict:
        """The summary that ``provr run`` prints, as plain values."""
        return {"run_id": self.run_id, "status": self.status, "folder": str(self.folder),
                "bundle": self.bundle.uri, "pinned": self.bundle.pinned, "note": self.bundle.note,
                "error": f"{self.error.__class__.__name__}: {self.error}" if self.error else None}


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
                    store: EvidenceStore | None = None) -> RunOutcome:
    """Run agent once. With no broker, a StubBroker allows the inputs of the
    spec and the URIs in allow. The evidence goes through store, a provevi
    store, by default the file store in the run folder. A bad setting or an unknown backend raises
    RunSettingError before the run folder is made; any later error fails the
    run, and the outcome holds it."""
    if backend is None:
        try:
            backend = load_backend(agent.backend)
        except ValueError as e:
            raise RunSettingError(str(e)) from e
    if broker is None:
        allow = list(allow or []) + [i.uri for i in agent.inputs]
        broker = StubBroker(allow, physical_schemes=catalog.stores.keys())
    ctx = RunContext.open(org=org, domain=domain, env=env, actor=actor, broker=broker,
                          resolver=resolver or ProvcatResolver(catalog), catalog=catalog,
                          runs_root=runs_root, mode=mode, bundle_scheme=bundle_scheme, store=store,
                          data={"agent": agent.name, "backend": agent.backend, "spec": agent.to_dict()})
    stager = stager or InProcessStager()
    outcome = RunOutcome(ctx.run_id, "failed", ctx.folder, Bundle(ctx.folder))
    error = None
    run = None
    try:
        snapshots = []
        for i in agent.inputs:
            grant = ctx.broker.request(ctx, i.uri, i.mode, purpose=agent.name)
            snapshots.append((input_name(i.uri, i.name), ctx.resolver.snapshot(ctx, grant)))
        run = stager.stage(ctx, snapshots)
        outcome.result = await backend.run(ctx, agent, run)
        stager.verify_inputs(ctx, run)
        outcome.artifacts = Outputter().collect(ctx, run)
    except BaseException as e:  # the run failed; its evidence says why
        error = e
        if run is not None and outcome.artifacts is None:
            # Record what a failed run left in output/, so the bundle holds no file the evidence lacks.
            try:
                outcome.artifacts = Outputter().collect(ctx, run)
            except Exception:
                pass
    outcome.bundle = ctx.finish(error)
    outcome.status = ctx.status
    outcome.error = error
    if error is not None and not isinstance(error, Exception):
        raise error  # a cancel or an interrupt goes on after the run is recorded
    return outcome


async def run_file(rf: RunFile, *, catalog: Catalog, runs_root: Path | None = None, mode: str | None = None,
                   bundle_scheme: str | Literal[False] | None = None, **parts) -> RunOutcome:
    """Run a checked run file. bundle_scheme, when given, takes the place of
    the run file's; False writes no bundle through the catalog. The other
    keyword arguments (broker, resolver, stager, backend, store) go to
    run_agent."""
    s = rf.settings
    scheme = s.bundle_scheme if bundle_scheme is None else (None if bundle_scheme is False else bundle_scheme)
    return await run_agent(rf.agent, catalog=catalog, org=s.org, domain=s.domain, env=s.env, actor=s.actor,
                           allow=list(s.allow), runs_root=runs_root, mode=mode, bundle_scheme=scheme, **parts)


def run_file_sync(rf: RunFile, **kw) -> RunOutcome:
    """run_file, for a caller with no event loop running."""
    return asyncio.run(run_file(rf, **kw))
