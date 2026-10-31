"""prove: list, show and verify the evidence of runs.

prove only reads and checks. Runs are written by provrun, through a provevi
store. Each command calls a library function and prints what it returns.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checks import read_events, verify_run
from .errors import EvidenceError, ProveviError
from .runs import default_runs_root, find_run, list_runs


def cmd_runs(args) -> int:
    for run_id in list_runs(args.runs):
        print(run_id)
    return 0


def cmd_show(args) -> int:
    folder = find_run(args.run, args.runs)
    for e in read_events(folder / "evidence"):
        refs = " ".join(e.refs)
        data = json.dumps(e.data, ensure_ascii=False, default=str)
        print(f"{e.seq:4} {e.time} {e.type:24} {e.component:24} {refs} {data}")
    return 0


def verify_command(args, prog: str, folder: Path | None = None) -> int:
    """The verify command of prove, and of provr, which calls it with the
    run folder it found."""
    folder = folder if folder is not None else find_run(args.run, args.runs)

    def open_catalog():
        from provcat import Catalog

        return Catalog.open(args.config, tenant=args.tenant)

    check = verify_run(folder, catalog=open_catalog, bundle=args.bundle, require_bundle=args.require_bundle)
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("runs", help="list the runs under the runs root").set_defaults(fn=cmd_runs)
    s = sub.add_parser("show", help="print the events of a run")
    s.add_argument("run")
    s.set_defaults(fn=cmd_show)
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
