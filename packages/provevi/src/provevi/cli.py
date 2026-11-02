"""prove: list, show, query and verify the evidence of runs, and check a telemetry policy.

prove reads and checks. Runs are written by provrun, through a provevi
store. The one command that writes, prove mlflow, writes only the copy of a
run in MLflow. Each command calls a library function and prints what it
returns.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .checks import read_events, verify_run
from .errors import EvidenceError, ProveviError, RunNotFound
from .events import EvidenceEvent
from .runs import default_runs_root, find_run
from .store import EvidenceStore, FileStore, open_store


def store_of(args) -> EvidenceStore:
    """The store that --store names, opened to read, or the file store under --runs."""
    return open_store(getattr(args, "store", None), args.runs, create=False)


def run_folder(args, store: EvidenceStore) -> Path:
    """The folder of args.run: a run ID under the runs root or the path of a
    run folder, or else the folder that the store records for the run."""
    try:
        return find_run(args.run, args.runs)
    except RunNotFound:
        folder = None if isinstance(store, FileStore) else store.run_folder(str(args.run))
        if folder is not None and (folder / "evidence").is_dir():
            return folder
        raise


def _sqlite(store: EvidenceStore, what: str):
    if isinstance(store, FileStore):
        raise ProveviError(f"the file store cannot answer {what}; give --store sqlite:///path")
    return store


def cmd_runs(args) -> int:
    store = store_of(args)
    if args.reading and args.experiment:
        raise ProveviError("give --reading or --experiment, not both")
    if args.reading:
        runs = _sqlite(store, "which runs read a URI").runs_reading(args.reading)
    elif args.experiment:
        runs = _sqlite(store, "which runs belong to an experiment").runs_of_experiment(args.experiment)
    else:
        runs = store.list_runs()
    for run_id in runs:
        print(run_id)
    return 0


def cmd_experiments(args) -> int:
    for e in _sqlite(store_of(args), "which experiments there are").experiments():
        print(f"{e['id']}  created by {e['created_by']} at {e['created_at']}")
    return 0


def cmd_access(args) -> int:
    store = _sqlite(store_of(args), "the grants and refusals of a run")
    if store.run(args.run) is None:
        raise RunNotFound(f"no run {args.run!r} in the store")
    for a in store.access(args.run):
        print(f"{a['seq']:4} {a['decision']:8} {a['mode'] or '':18} {a['uri']}  {a['reason'] or ''}")
    return 0


def cmd_show(args) -> int:
    for e in _events(args):
        refs = " ".join(e.refs)
        data = json.dumps(e.data, ensure_ascii=False, default=str)
        print(f"{e.seq:4} {e.time} {e.type:24} {e.component:24} {refs} {data}")
    return 0


def _events(args) -> list[EvidenceEvent]:
    """The events of args.run, from the store if it holds the run, else from its folder."""
    store = store_of(args)
    if isinstance(store, FileStore) or store.run(str(args.run)) is None:
        return read_events(run_folder(args, store) / "evidence")
    events = []
    for n, line in enumerate(store.read_run(str(args.run)).lines()):
        try:
            events.append(EvidenceEvent.from_line(line))
        except (ValueError, TypeError) as e:
            raise EvidenceError(f"line {n}: not an event: {e}") from e
    return events


def cmd_policy(args) -> int:
    from .telemetry import TelemetryPolicy

    policy = TelemetryPolicy.load(args.file)
    print(f"{policy.source}: sha256 {policy.digest()}")
    for name, lib in sorted(policy.libraries.items()):
        print(f"  library {name} {lib.versions} feeds {', '.join(lib.telemetry) or 'nothing'}")
    for name, d in sorted(policy.destinations.items()):
        print(f"  destination {name} ({d.kind}) receives {d.receives}" + (f" at {d.endpoint}" if d.endpoint else ""))
    print(f"  hosts: {', '.join(policy.hosts) or 'none'}")
    problems = policy.check_installed()
    for p in problems:
        print(f"prove: {p}", file=sys.stderr)
    return 1 if problems else 0


def cmd_telemetry(args) -> int:
    from .telemetry import BLOCKED, PROBLEM

    found = [e for e in _events(args) if e.type in (BLOCKED, PROBLEM)]
    for e in found:
        if e.type == BLOCKED:
            lib = e.data.get("library") or ""
            line = f"blocked {e.data.get('path', '')} {lib}  {e.data.get('detail', '')}"
        else:
            line = f"problem {e.data.get('problem', '')}"
        print(f"{e.seq:4} {e.time} {line}".rstrip())
    if not found:
        print(f"{args.run}: no blocked attempts and no problems")
    return 0


#: The variable that names the telemetry policy, as provrun reads it.
POLICY_ENV = "PROVRUN_TELEMETRY_POLICY"


def _policy(path: str | None):
    from .telemetry import TelemetryPolicy

    path = path or os.environ.get(POLICY_ENV)
    return TelemetryPolicy.load(path) if path else TelemetryPolicy.deny_all()


def cmd_mlflow(args) -> int:
    from .mlflow_copy import copy_run

    # MLflow sets its logger's level as it is imported, from this variable;
    # without it, MLflow logs a note to the terminal as it makes its tables.
    os.environ.setdefault("MLFLOW_LOGGING_LEVEL", "WARNING")
    store = store_of(args)
    folder = None
    if isinstance(store, FileStore):
        folder = run_folder(args, store) / "evidence"
    c = copy_run(store, str(args.run), _policy(args.policy), folder=folder)
    print(f"{c.run_id}: copied to MLflow at {c.tracking_uri}, destination {c.destination}, level {c.level}")
    print(f"  experiment {c.experiment_id}, MLflow run {c.mlflow_run_id}, "
          + (f"trace {c.trace_id} with {c.spans} spans" if c.trace_id else "no trace: the run recorded no spans"))
    for old in c.replaced:
        print(f"  deleted the earlier copy, MLflow run {old}")
    return 0


def cmd_ui(args) -> int:
    import shutil
    import subprocess

    from .mlflow_copy import ui_command

    store = store_of(args)
    if isinstance(store, FileStore):
        raise ProveviError("prove ui opens the store's own file; give --store sqlite:///path")
    command, env = ui_command(store.path, args.port)
    if shutil.which(command[0]) is None:
        raise ProveviError("prove ui needs MLflow; install provevi[mlflow]")
    print(f"MLflow's viewer on {store.path}: http://127.0.0.1:{args.port}", flush=True)
    return subprocess.call(command, env=env)


def verify_command(args, prog: str, folder: Path | None = None) -> int:
    """The verify command of prove, and of provr, which calls it with the
    run folder it found."""
    store = store_of(args)
    folder = folder if folder is not None else run_folder(args, store)

    def open_catalog():
        from provcat import Catalog

        return Catalog.open(args.config, tenant=args.tenant)

    check = verify_run(folder, catalog=open_catalog, bundle=args.bundle, require_bundle=args.require_bundle,
                       store=store)
    for p in check.problems:
        print(f"{prog}: {p}", file=sys.stderr)
    if check.ok:
        print(f"{check.run_id}: the evidence and the outputs verify"
              + (", and match the committed bundle" if check.pinned else "; no committed bundle to compare with"))
    return 0 if check.ok else 1


def add_verify_arguments(v: argparse.ArgumentParser) -> None:
    v.add_argument("run", help="a run ID under the runs root, or the path of a run folder")
    v.add_argument("--config", help="the provcat configuration, to read the committed bundle")
    v.add_argument("--tenant", help="the provcat tenant")
    v.add_argument("--bundle", help="the pinned URI of the committed bundle, taken from outside the run folder; "
                                    "used instead of bundle.json")
    v.add_argument("--require-bundle", action="store_true",
                   help="fail if there is no committed bundle to compare with")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prove", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=Path, default=default_runs_root(), help="the runs root")
    ap.add_argument("--store", help="the evidence store: file (the default) or sqlite:///path")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("runs", help="list the runs in the store, or those that read a URI or belong to an experiment")
    r.add_argument("--reading", metavar="URI", help="only the runs that staged this URI as an input")
    r.add_argument("--experiment", metavar="NAME", help="only the runs of this experiment, such as acme/demo/dev/q")
    r.set_defaults(fn=cmd_runs)
    sub.add_parser("experiments", help="list the experiments in the store").set_defaults(fn=cmd_experiments)
    a = sub.add_parser("access", help="print the grants and refusals of a run")
    a.add_argument("run")
    a.set_defaults(fn=cmd_access)
    s = sub.add_parser("show", help="print the events of a run")
    s.add_argument("run")
    s.set_defaults(fn=cmd_show)
    t = sub.add_parser("telemetry", help="print what the telemetry policy blocked in a run, and the problems the run found")
    t.add_argument("run")
    t.set_defaults(fn=cmd_telemetry)
    p = sub.add_parser("policy", help="check a telemetry policy file, and the installed versions of its libraries")
    p.add_argument("file")
    p.set_defaults(fn=cmd_policy)
    m = sub.add_parser("mlflow", help="copy a sealed run into MLflow, at the place that MLFLOW_TRACKING_URI names "
                                      "and the telemetry policy approves")
    m.add_argument("run")
    m.add_argument("--policy", help=f"the telemetry policy file; the default is the file that {POLICY_ENV} names, "
                                    "else a policy that approves nothing")
    m.set_defaults(fn=cmd_mlflow)
    u = sub.add_parser("ui", help="open MLflow's viewer on the store's own file, on this machine only")
    u.add_argument("--port", type=int, default=5000, help="the port, on 127.0.0.1 (default 5000)")
    u.set_defaults(fn=cmd_ui)
    v = sub.add_parser("verify", help="check the evidence, the outputs and the committed bundle of a run")
    add_verify_arguments(v)
    v.set_defaults(fn=lambda a: verify_command(a, "prove"))
    args = ap.parse_args(argv)
    from provcat import ProvcatError

    try:
        return args.fn(args)
    except EvidenceError as e:
        print(f"prove: {e}", file=sys.stderr)
        return 2
    except (ProveviError, ProvcatError) as e:
        print(f"prove: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
