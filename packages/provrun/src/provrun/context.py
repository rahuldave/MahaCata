"""The run context: the identity of one run and the parts it uses.

provrun is the caller of record. It makes the run ID and opens the evidence
before anything else happens, and it closes the evidence and commits the
bundle after everything else. Every other part takes the context, so no part
can run outside a run.

A run keeps its files in one folder::

    <runs root>/<run_id>/evidence/   the evidence, see provrun.evidence
    <runs root>/<run_id>/output/     what the agent wrote
    <runs root>/<run_id>/input/      the staged inputs, not part of the bundle
    <runs root>/<run_id>/scratch/    the agent's working space, not part of the bundle

The bundle is the evidence and the output folders. At the end of a run they
are written through provcat under the run URI,
``<bundle_scheme>://<org>/<domain>/<env>/<run_id>``, and committed in one
commit, if the run sets ``bundle_scheme``. Otherwise they stay in the run
folder. Code deep inside a tool finds the run
with ``current_run()``.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import re
import secrets
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import provevi
from provevi import EvidenceStore, default_runs_root, verify_against

from .errors import RunNotFound, RunSettingError
from .evidence import MODES, EvidenceError, EvidenceRecorder

if TYPE_CHECKING:
    from provcat import Catalog

    from .access import Broker
    from .resolve import Resolver

__all__ = ["Bundle", "RunContext", "current_run", "find_run"]

_CURRENT: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar("provrun_run", default=None)


_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")


def check_segment(name: str, value) -> None:
    """Refuse an org, domain or env that is not one segment of the run URI.
    ``domain: demo/prod`` would put the bundle under the prod environment."""
    if not isinstance(value, str) or not value or value in (".", "..") or re.search(r"[/?#\\\s]", value):
        raise RunSettingError(f"{name} must be one part of a URI, with no '/', '?', '#' or space; got {value!r}")


def check_scheme(value, catalog: Catalog | None = None) -> None:
    """Refuse a bundle scheme that is not a plain scheme name, or that names a
    store of the catalog: the bundle goes through a logical scheme."""
    if not isinstance(value, str) or not _SCHEME.fullmatch(value):
        raise RunSettingError(f"bundle_scheme must be a scheme name such as 'run'; got {value!r}")
    if catalog is not None and value in catalog.stores:
        raise RunSettingError(f"bundle_scheme {value!r} is a store of the catalog; use a logical scheme")


def evidence_mode(env: str, mode: str | None = None) -> str:
    """The evidence mode of a run: mode if given, else lenient in dev and
    strict in every other environment. Raises RunSettingError on a bad mode."""
    if mode is not None and mode not in MODES:
        raise RunSettingError(f"mode must be one of {MODES}; got {mode!r}")
    return mode or ("lenient" if env == "dev" else "strict")


def new_run_id() -> str:
    return f"run-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def find_run(
    run: str | Path,  # A run ID under the root, or the path of a run folder
    root: str | Path | None = None,  # The runs root; None is provevi's default runs root
    *,
    ids_only: bool = False,  # Take run as a run ID only, never as a path
) -> Path:  # The run's folder
    """The folder of a run, as provevi.find_run finds it. Raises RunNotFound."""
    try:
        return provevi.find_run(run, root, ids_only=ids_only)
    except provevi.RunNotFound as e:
        raise RunNotFound(str(e)) from None


def current_run() -> RunContext:  # The context of the run that is active here; RuntimeError when none is
    """The run that is going on, for code deep inside a tool or a backend.

    A run is active inside ``RunContext.active``, which an in-process backend
    enters around the agent."""
    ctx = _CURRENT.get()
    if ctx is None:
        raise RuntimeError("no run is active; open a RunContext first")
    return ctx


@dataclass
class Bundle:
    """Where the bundle of a run went."""

    folder: Path  # The run folder
    uri: str | None = None  # The run URI, if the bundle was written through provcat
    pinned: str | None = None  # The commit, if the store has commits
    note: str = ""  # A note on the bundle, such as why it stayed in the run folder or was not pinned


@dataclass
class RunContext:
    """One run: its identity, its folder, its evidence recorder and the parts it uses.

    Every part of a run takes it. ``RunContext.open`` makes one."""

    run_id: str  # The run's ID, such as run-20260101T000000Z-0a1b2c3d
    org: str  # The org, the first part of the run URI
    domain: str  # The domain, the second part
    env: str  # The environment, the third part, such as dev or prod
    actor: str  # Who runs it; each event records it
    folder: Path  # The run folder, <runs root>/<run_id>
    recorder: EvidenceRecorder  # Writes the run's evidence; every part calls its emit or step
    broker: Broker  # Decides each input request
    resolver: Resolver  # Makes each granted input's snapshot
    catalog: Catalog | None = None  # The catalog that the bundle is written through
    bundle_scheme: str | None = None  # The provcat scheme for run URIs, for example "run"
    status: str = "open"  # "open", then "completed" or "failed" once finish has run
    trace: dict = field(default_factory=dict)  # The trace and span IDs of the run's root span
    telemetry: Any = None  # The run's provrun.telemetry.Telemetry, when run_agent runs it
    on_text: Any = None  # Called with each piece of a streamed reply, as it comes
    pieces: Any = None  # The pieces of the model request in hand, a provrun.backends.Pieces

    def text(
        self,
        piece: str,  # One piece of a streamed reply; an empty piece is dropped
    ) -> None:  # Nothing
        """Hand on a piece of a streamed reply, as it comes, to ``on_text``.

        The evidence does not record pieces: it records each whole response."""
        if self.on_text is not None and piece:
            self.on_text(piece)

    @classmethod
    def open(
        cls,
        *,
        org: str,  # The org, one part of a URI
        domain: str,  # The domain, one part of a URI
        env: str,  # The environment, one part of a URI; dev makes the evidence mode lenient by default
        actor: str,  # Who runs it
        broker: Broker,  # Decides each input request
        resolver: Resolver,  # Makes each granted input's snapshot
        catalog: Catalog | None = None,  # The catalog that the bundle is written through
        runs_root: Path | None = None,  # The folder that holds run folders; None is provevi's default runs root
        mode: str | None = None,  # The evidence mode, "strict" or "lenient"; None is lenient in dev and strict elsewhere
        bundle_scheme: str | None = None,  # A logical scheme to write the bundle through; None keeps it in the run folder
        run_id: str | None = None,  # The run's ID; None makes a new one
        data: dict | None = None,  # More data for run_created, such as the agent spec
        store: EvidenceStore | None = None,  # The provevi store for the evidence; None is the file store
        trace: dict | None = None,  # The trace and span IDs of the run's root span, which run_created and the run's last event record
        sinks: list | None = None,  # More sinks that get each evidence line from run_created on, such as a live feed
    ) -> RunContext:  # The open run, with run_created written
        """Make the run ID and the run folder, and write run_created.

        A bad setting raises RunSettingError before the folder is made."""
        for name, value in (("org", org), ("domain", domain), ("env", env)):
            check_segment(name, value)
        if mode is not None and mode not in MODES:
            raise RunSettingError(f"mode must be one of {MODES}; got {mode!r}")
        if bundle_scheme is not None:
            check_scheme(bundle_scheme, catalog)
        run_id = run_id or new_run_id()
        folder = Path(runs_root if runs_root is not None else default_runs_root()) / run_id
        folder.mkdir(parents=True, exist_ok=False)
        for sub in ("output", "input", "scratch"):
            (folder / sub).mkdir()
        recorder = EvidenceRecorder(run_id, actor, folder / "evidence", mode=evidence_mode(env, mode), store=store,
                                    sinks=sinks)
        ctx = cls(run_id, org, domain, env, actor, folder, recorder, broker, resolver, catalog, bundle_scheme,
                  trace=dict(trace or {}))
        recorder.emit("run_created", "RunContext", refs=[ctx.run_uri],
                      data={"org": org, "domain": domain, "env": env, "mode": recorder.mode, **(data or {}),
                            **ctx.trace})
        return ctx

    @property
    def run_uri(self) -> str:
        "The run URI, <bundle_scheme or run>://<org>/<domain>/<env>/<run_id>."
        return f"{self.bundle_scheme or 'run'}://{self.org}/{self.domain}/{self.env}/{self.run_id}"

    @property
    def output_dir(self) -> Path:
        "The run's output folder, <run folder>/output."
        return self.folder / "output"

    @contextlib.contextmanager
    def active(
        self,
    ) -> Iterator[RunContext]:  # This context, which current_run gives until the block ends
        "Make this the run that current_run gives, inside a with block."
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)

    def finish(
        self,
        error: BaseException | None = None,  # The error that failed the run; None for a run that completed
    ) -> Bundle:  # Where the bundle went
        """Write run_completed or run_failed, close the evidence, and write the bundle.

        A run can finish once: a second finish raises RuntimeError.
        An EvidenceError from writing or closing the evidence is raised once
        the bundle is written; in strict mode, a bundle that cannot be written
        raises EvidenceError too."""
        if self.status != "open":
            raise RuntimeError(f"{self.run_id} has already finished")
        self.status = "failed" if error else "completed"
        lost = None
        try:
            if error is None:
                self.recorder.emit("run_completed", "RunContext", refs=[self.run_uri], data=dict(self.trace))
            else:
                self.recorder.emit("run_failed", "RunContext", refs=[self.run_uri],
                                   data={"error": f"{error.__class__.__name__}: {error}", **self.trace})
        except EvidenceError as e:
            lost = e  # the manifest records it; close the evidence and write the bundle anyway
            self.status = "failed"
        try:
            self.recorder.close()
        except EvidenceError as e:
            lost = lost or e
        bundle = self._write_bundle()
        (self.folder / "bundle.json").write_text(json.dumps(
            {"uri": bundle.uri, "pinned": bundle.pinned, "note": bundle.note}, indent=2) + "\n")
        try:
            record = getattr(self.recorder.writer, "record_bundle", None)
            if record is not None:
                record(bundle.uri, bundle.pinned)
        except Exception as e:  # the bundle is written; only the store's note of it is lost
            warnings.warn(f"the store did not record the bundle of {self.run_id}: {e}", stacklevel=2)
        if lost is not None:
            raise lost
        return bundle

    def _write_bundle(self) -> Bundle:
        bundle = Bundle(self.folder)
        if self.catalog is None or self.bundle_scheme is None:
            bundle.note = "no store for runs is set; the bundle stays in the run folder"
            return bundle
        from provcat import StoreError

        try:
            self.catalog.put(f"{self.run_uri}/evidence", self.folder / "evidence")
            if any(self.output_dir.iterdir()):
                self.catalog.put(f"{self.run_uri}/output", self.output_dir)
            bundle.uri = self.run_uri
        except Exception as e:
            return self._bundle_failed(bundle, f"cannot write the bundle to {self.run_uri}: {e}")
        try:
            bundle.pinned = str(self.catalog.commit(self.run_uri, f"provrun {self.run_id}",
                                                   {"provrun.run_id": self.run_id}))
        except StoreError as e:
            if "has no commits" in str(e):
                bundle.note = "the store for runs has no commits; the bundle is written but not pinned"
                return bundle
            # Another run's commit on the same branch can take in this bundle first, and then
            # this commit has nothing to commit. Accept the newest commit if it holds this bundle.
            pinned = self._pinned_holding_bundle()
            if pinned is None:
                return self._bundle_failed(bundle, f"cannot commit the bundle at {self.run_uri}: {e}")
            bundle.pinned = pinned
            bundle.note = "another commit on the branch took in this bundle"
        return bundle

    def _pinned_holding_bundle(self) -> str | None:
        from provcat import parse

        try:
            pinned = self.catalog.pin(self.run_uri)
            base = parse(str(pinned)).child("evidence")
            events, manifest = (self.catalog.read(base.child(n)) for n in ("events.jsonl", "manifest.json"))
        except Exception:
            return None
        return None if verify_against(self.folder / "evidence", events, manifest) else str(pinned)

    def _bundle_failed(self, bundle: Bundle, message: str) -> Bundle:
        if self.recorder.mode == "strict":
            raise EvidenceError(message)
        warnings.warn(message, stacklevel=3)
        bundle.note = message
        return bundle
