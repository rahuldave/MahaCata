"""provr: run an agent spec, and show or verify the evidence of a run.

    provr run RUNFILE        run the agent in a run file and print the run ID
    provr show RUN           print the events of a run
    provr verify RUN         check the hash chain, the payloads and the outputs

RUN is a run ID under the runs root, or the path of a run folder. A run file
has a ``run`` section, with org, domain, env and actor, and an ``agent``
section; docs/provrun.md shows one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import provevi
from provcat import ProvcatError
import provevi.cli
from provevi.cli import add_verify_arguments, verify_command

from .context import default_runs_root
from .errors import ProvrunError


def cmd_run(args) -> int:
    from provcat import Catalog

    from .runner import run_file_sync
    from .spec import RunFile

    rf = RunFile.load(args.runfile)
    catalog = Catalog.open(args.config, tenant=args.tenant)
    store = provevi.open_store(args.store) if args.store else None
    outcome = run_file_sync(rf, catalog=catalog, runs_root=args.runs, mode=args.mode,
                            bundle_scheme=args.bundle_scheme, store=store)
    print(json.dumps(outcome.to_dict(), indent=2))
    return 0 if outcome.status == "completed" else 1


def cmd_show(args) -> int:
    return provevi.cli.cmd_show(args)


def cmd_verify(args) -> int:
    return verify_command(args, "provr")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="provr", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=Path, default=default_runs_root(), help="the runs root")
    ap.add_argument("--store", help="the evidence store: file or sqlite:///path. For run, it takes the place of "
                                    "the run file's store; show and verify read the run from it")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the agent in a run file")
    r.add_argument("runfile")
    r.add_argument("--config", help="the provcat configuration")
    r.add_argument("--tenant", help="the provcat tenant")
    r.add_argument("--mode", choices=["strict", "lenient"], help="the evidence mode")
    r.add_argument("--bundle-scheme", help="the provcat scheme that holds run bundles, for example run")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("show", help="print the events of a run")
    s.add_argument("run")
    s.set_defaults(fn=cmd_show)
    v = sub.add_parser("verify", help="check the evidence and the outputs of a run, as prove verify does")
    add_verify_arguments(v)
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except provevi.EvidenceError as e:  # provrun's EvidenceError is one too
        print(f"provr: {e}", file=sys.stderr)
        return 2
    except (ProvrunError, provevi.ProveviError, ProvcatError) as e:
        print(f"provr: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
