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
import asyncio
import json
import sys
from pathlib import Path

from .context import default_runs_root
from .evidence import EvidenceError, read_events, sha256_bytes, verify, verify_against


def _run_folder(run: str, root: Path) -> Path:
    p = Path(run)
    if p.is_dir() and (p / "evidence").is_dir():
        return p
    p = root / run
    if not (p / "evidence").is_dir():
        raise SystemExit(f"provr: no run {run!r} under {root}")
    return p


def _verify_outputs(folder: Path) -> list[str]:
    """The files in output/ must be exactly the recorded outputs, with their bytes."""
    problems = []
    recorded = {}
    for e in read_events(folder / "evidence"):
        if e.type == "output_artifact_created":
            recorded[e.data["path"].split("/", 2)[-1]] = e.data["sha256"]
    out = folder / "output"
    present = {f.relative_to(out).as_posix() for f in out.rglob("*") if f.is_file() or f.is_symlink()} \
        if out.is_dir() else set()
    for rel in sorted(present - set(recorded)):
        problems.append(f"output {rel} is not in the evidence")
    for rel, digest in sorted(recorded.items()):
        f = out / rel
        if rel not in present:
            problems.append(f"output {rel} is missing")
        elif f.is_symlink() or sha256_bytes(f.read_bytes()) != digest:
            problems.append(f"output {rel} was changed")
    return problems


def _bundle_pin(folder: Path, args) -> str | None:
    """The commit to compare with: --bundle if given, else the one that
    bundle.json names. bundle.json is in the run folder, so whoever can change
    the folder can remove it or point it elsewhere; --bundle takes the commit
    from outside the folder."""
    record = folder / "bundle.json"
    named = json.loads(record.read_text()).get("pinned") if record.exists() else None
    return args.bundle or named


def _verify_bundle(folder: Path, args) -> list[str]:
    """Compare the evidence with the committed bundle, if the run has one."""
    pinned = _bundle_pin(folder, args)
    if not pinned:
        return ["no committed bundle to compare with, and --require-bundle is set"] if args.require_bundle else []
    from provcat import Catalog, parse

    if parse(pinned).selector is None:
        return [f"the bundle {pinned} is not pinned; give the URI with ?at= from the commit"]
    catalog = Catalog.open(args.config, tenant=args.tenant)
    base = parse(pinned).child("evidence")
    try:
        events = catalog.read(base.child("events.jsonl"))
        manifest = catalog.read(base.child("manifest.json"))
    except Exception as e:
        return [f"cannot read the committed bundle {pinned}: {e}"]
    return verify_against(folder / "evidence", events, manifest)


def cmd_run(args) -> int:
    from provcat import Catalog

    from .runner import run_agent
    from .spec import load_run_file

    run_cfg, agent = load_run_file(Path(args.runfile))
    missing = [k for k in ("org", "domain", "env", "actor") if not run_cfg.get(k)]
    if missing:
        raise SystemExit(f"provr: the run section needs {missing}")
    catalog = Catalog.open(args.config, tenant=args.tenant)
    outcome = asyncio.run(run_agent(
        agent, catalog=catalog, org=run_cfg["org"], domain=run_cfg["domain"], env=run_cfg["env"],
        actor=run_cfg["actor"], allow=run_cfg.get("allow"), runs_root=args.runs, mode=args.mode,
        bundle_scheme=args.bundle_scheme or run_cfg.get("bundle_scheme")))
    print(json.dumps({"run_id": outcome.run_id, "status": outcome.status, "folder": str(outcome.folder),
                      "bundle": outcome.bundle.uri, "pinned": outcome.bundle.pinned,
                      "note": outcome.bundle.note,
                      "error": f"{outcome.error.__class__.__name__}: {outcome.error}" if outcome.error else None},
                     indent=2))
    return 0 if outcome.status == "completed" else 1


def cmd_show(args) -> int:
    folder = _run_folder(args.run, args.runs)
    for e in read_events(folder / "evidence"):
        refs = " ".join(e.refs)
        data = json.dumps(e.data, ensure_ascii=False, default=str)
        print(f"{e.seq:4} {e.time} {e.type:24} {e.component:24} {refs} {data}")
    return 0


def cmd_verify(args) -> int:
    folder = _run_folder(args.run, args.runs)
    problems = verify(folder / "evidence", run_id=folder.name)
    if not problems:
        problems = _verify_outputs(folder) + _verify_bundle(folder, args)
    for p in problems:
        print(f"provr: {p}", file=sys.stderr)
    if not problems:
        anchored = _bundle_pin(folder, args)
        print(f"{folder.name}: the evidence and the outputs verify"
              + (", and match the committed bundle" if anchored else "; no committed bundle to compare with"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="provr", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=Path, default=default_runs_root(), help="the runs root")
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
    v = sub.add_parser("verify", help="check the evidence and the outputs of a run")
    v.add_argument("run")
    v.add_argument("--config", help="the provcat configuration, to read the committed bundle")
    v.add_argument("--tenant", help="the provcat tenant")
    v.add_argument("--bundle", help="the pinned URI of the committed bundle, taken from outside the run folder; "
                                    "used instead of bundle.json")
    v.add_argument("--require-bundle", action="store_true",
                   help="fail if there is no committed bundle to compare with")
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except EvidenceError as e:
        print(f"provr: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
