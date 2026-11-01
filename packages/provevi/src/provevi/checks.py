"""Checks of a run's evidence, its outputs and its committed bundle.

Each check returns a list of problems; an empty list means it passes. No
check prints or exits, so a program can call them as ``prove`` does.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import EvidenceError
from .events import EvidenceEvent, sha256_bytes

if TYPE_CHECKING:
    from provcat import Catalog

    from .store import EvidenceStore, RunRecord

#: The events that end a run. A run's evidence starts with run_created and ends with one of these.
ENDINGS = ("run_completed", "run_failed")


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _lines(folder: Path) -> list[str]:
    """The lines of events.jsonl. Raises EvidenceError if it cannot be read."""
    path = folder / "events.jsonl"
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise EvidenceError(f"{path} is missing") from None
    except (OSError, UnicodeDecodeError) as e:
        raise EvidenceError(f"cannot read {path}: {e}") from e
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_events(folder: Path) -> list[EvidenceEvent]:
    """The events of an evidence folder. Raises EvidenceError if events.jsonl
    cannot be read, or at a line that is not an event."""
    events = []
    for n, line in enumerate(_lines(folder)):
        try:
            events.append(EvidenceEvent.from_line(line))
        except (ValueError, TypeError) as e:
            raise EvidenceError(f"line {n}: not an event: {e}") from e
    return events


def _files(folder: Path) -> tuple[set[str], list[str]]:
    """The relative paths under folder, and a problem for each entry that is
    neither a file, a folder nor a link, and for each folder that cannot be
    read. Links to folders are not followed."""
    present: set[str] = set()
    problems: list[str] = []
    if not folder.is_dir():
        return present, problems

    def unreadable(e: OSError) -> None:
        rel = Path(e.filename).relative_to(folder).as_posix() if e.filename else "?"
        problems.append(f"{folder.name} {rel} cannot be read")

    for top, dirs, files in os.walk(folder, onerror=unreadable):
        here = Path(top)
        for name in files + [d for d in dirs if (here / d).is_symlink()]:
            f = here / name
            rel = f.relative_to(folder).as_posix()
            present.add(rel)
            if not f.is_symlink() and not f.is_file():
                problems.append(f"{folder.name} {rel} is not a regular file")
    return present, problems


def _digest(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def verify(folder: Path | RunRecord, run_id: str | None = None) -> list[str]:
    """Check the chain, the sequence, the payloads and the manifest of one
    evidence folder, or of a run that a store reads back, and that it is the whole record of one run: run_created
    first, one ending event last, as many events as the manifest says, the
    payloads that the manifest lists, and the run ID given, if one is. Returns
    the problems found; an empty list means it verifies. A malformed folder
    gives problems, never an exception.

    The check proves that the folder is consistent. Someone who can write the
    folder can still write a consistent false record. To rule that out,
    compare the folder with the bundle committed in the store, which
    ``verify_against`` does."""
    from .store import FolderRecord, RunRecord

    record = folder if isinstance(folder, RunRecord) else FolderRecord(Path(folder))
    problems: list[str] = []
    try:
        lines = record.lines()
    except EvidenceError as e:
        return [str(e)]
    prev = None
    expected = run_id
    run_id = None
    types: list[str] = []
    next_seq = 0  # after a gap, counting goes on from the seq found, so a gap is reported once
    for n, line in enumerate(lines):
        try:
            event = EvidenceEvent.from_line(line)
        except (ValueError, TypeError) as e:
            problems.append(f"line {n}: not an event: {e}")
            prev = sha256_bytes(line.encode())
            continue
        if event.line() != line:
            problems.append(f"line {n}: not in canonical form")
        if event.seq != next_seq:
            problems.append(f"line {n}: seq is {event.seq}, not {next_seq}")
        next_seq = event.seq + 1 if isinstance(event.seq, int) else next_seq + 1
        if event.prev != prev:
            problems.append(f"line {n}: the chain is broken")
        types.append(event.type)
        run_id = run_id or event.run_id
        if event.run_id != run_id:
            problems.append(f"line {n}: run {event.run_id}, not {run_id}")
        if event.payload is not None:
            if not isinstance(event.payload, str) or not _SHA256.fullmatch(event.payload):
                problems.append(f"line {n}: payload {event.payload!r} is not a SHA-256")
            else:
                try:
                    digest = record.payload_digest(event.payload)
                except (OSError, EvidenceError) as e:
                    problems.append(f"line {n}: cannot read payload {event.payload}: {e}")
                else:
                    if digest is None:
                        problems.append(f"line {n}: payload {event.payload} is missing")
                    elif digest != event.payload:
                        problems.append(f"line {n}: payload {event.payload} was changed")
        prev = sha256_bytes(line.encode())
    if expected is not None and run_id != expected:
        problems.append(f"the events are of run {run_id}, not {expected}")
    if not types or types[0] != "run_created":
        problems.append("the first event is not run_created")
    if not types or types[-1] not in ENDINGS:
        problems.append("the last event is not run_completed or run_failed")
    if sum(t in ENDINGS for t in types) > 1:
        problems.append("the run ends more than once")
    try:
        problems += record.problems()
        raw = record.manifest_bytes()
        present, odd = record.payload_files()
    except EvidenceError as e:
        return problems + [str(e)]
    except OSError:
        return problems + ["manifest.json cannot be read"]
    if raw is None:
        return problems + ["manifest.json is missing; the run did not close"]
    try:
        manifest = json.loads(raw)
    except (ValueError, RecursionError):
        return problems + ["manifest.json is not JSON"]
    if not isinstance(manifest, dict):
        return problems + ["manifest.json is not a JSON object"]
    if manifest.get("last") != prev:
        problems.append("the manifest does not end where the events end")
    if manifest.get("events") != len(lines):
        problems.append(f"the manifest counts {manifest.get('events')} events, and there are {len(lines)}")
    if manifest.get("run_id") != run_id:
        problems.append(f"the manifest is of run {manifest.get('run_id')}, not {run_id}")
    if not manifest.get("complete", False):
        lost = manifest.get("problems")
        problems.append("the recorder lost events: "
                        + ("; ".join(map(str, lost)) if isinstance(lost, list) else str(lost)))
    problems += sorted(odd)
    listed = manifest.get("payloads")
    if isinstance(listed, list):
        listed = set(map(str, listed))
        problems += [f"payload {rel} is not in the manifest" for rel in sorted(present - listed)]
        problems += [f"payload {rel} is in the manifest, and missing" for rel in sorted(listed - present)]
    elif "payloads" in manifest:
        problems.append("the manifest's payloads is not a list")
    return problems


def verify_against(folder: Path, events: bytes, manifest: bytes) -> list[str]:
    """Compare the local evidence with the events and the manifest read from
    the committed bundle. A difference means that one of them was changed."""
    problems = []
    for name, committed in (("events.jsonl", events), ("manifest.json", manifest)):
        try:
            same = (folder / name).read_bytes() == committed
        except OSError:
            same = False
        if not same:
            problems.append(f"{name} differs from the committed bundle")
    return problems


def verify_outputs(run_folder: Path) -> list[str]:
    """The files in output/ must be exactly the recorded outputs, with their
    bytes. Each must be a regular file, recorded once. An output event names
    its file by its reference, ``<run URI>/output/<path>``."""
    problems = []
    try:
        events = read_events(run_folder / "evidence")
    except EvidenceError as e:
        return [str(e)]
    recorded: dict[str, str] = {}
    for e in events:
        if e.type != "output_artifact_created":
            continue
        digest = e.data.get("sha256")
        marker = f"/{e.run_id}/output/"
        names = [r.split(marker, 1)[1] for r in e.refs if marker in r]
        if not isinstance(digest, str):
            problems.append(f"line {e.seq}: the output event has no sha256")
            continue
        if len(names) != 1 or not names[0] or ".." in names[0].split("/") or names[0].startswith("/"):
            problems.append(f"line {e.seq}: the output event names no one output of the run")
            continue
        rel = names[0]
        if rel in recorded:
            problems.append(f"output {rel} is recorded more than once")
        recorded[rel] = digest
    out = run_folder / "output"
    present, odd = _files(out)
    for rel in sorted(present - set(recorded)):
        problems.append(f"output {rel} is not in the evidence")
    problems += sorted(odd)
    for rel, digest in sorted(recorded.items()):
        f = out / rel
        if rel not in present:
            problems.append(f"output {rel} is missing")
        elif f.is_symlink() or not f.is_file() or _digest(f) != digest:
            problems.append(f"output {rel} was changed")
    return problems


def bundle_pin(run_folder: Path, given: str | None = None) -> str | None:
    """The commit to compare with: given, if there is one, else the one that
    bundle.json names. bundle.json is in the run folder, so whoever can change
    the folder can remove it or point it elsewhere; a commit given from outside
    the folder does not have that weakness. Raises EvidenceError if bundle.json
    cannot be read."""
    if given:
        return given
    record = run_folder / "bundle.json"
    if not record.exists():
        return None
    try:
        named = json.loads(record.read_bytes())
    except (OSError, ValueError) as e:
        raise EvidenceError(f"bundle.json is not JSON: {e}") from e
    if not isinstance(named, dict):
        raise EvidenceError("bundle.json is not a JSON object")
    pinned = named.get("pinned")
    if pinned is not None and not isinstance(pinned, str):
        raise EvidenceError("bundle.json does not name a bundle with a string \"pinned\"")
    return pinned


def verify_bundle(run_folder: Path, pinned: str | None, catalog: Catalog | Callable[[], Catalog] | None, *,
                  require: bool = False) -> list[str]:
    """Compare the evidence with the bundle committed at pinned, read through
    catalog. catalog may be a function that opens one, so that no catalog is
    opened when there is no bundle to read. A bad URI, a catalog that does not
    open and a bundle that cannot be read each give a problem."""
    if not pinned:
        return ["no committed bundle to compare with, and a bundle is required"] if require else []
    from provcat import ProvcatError, parse

    try:
        uri = parse(pinned)
    except ProvcatError as e:
        return [f"the bundle {pinned} is not a catalog URI: {e}"]
    if uri.selector is None:
        return [f"the bundle {pinned} is not pinned; give the URI with ?at= from the commit"]
    if catalog is None:
        return [f"no catalog was given to read the committed bundle {pinned}"]
    try:
        if callable(catalog):
            catalog = catalog()
    except Exception as e:
        return [f"cannot open the catalog to read the committed bundle {pinned}: {e}"]
    try:
        base = uri.child("evidence")
        events = catalog.read(base.child("events.jsonl"))
        manifest = catalog.read(base.child("manifest.json"))
    except Exception as e:
        return [f"cannot read the committed bundle {pinned}: {e}"]
    return verify_against(run_folder / "evidence", events, manifest)


@dataclass
class RunCheck:
    """The result of ``verify_run``."""

    run_id: str
    problems: list[str] = field(default_factory=list)
    pinned: str | None = None  # the committed bundle compared with, if any

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "ok": self.ok, "problems": self.problems, "pinned": self.pinned}


def verify_store_copy(run_folder: Path, store: EvidenceStore) -> list[str]:
    """Check the run as the store holds it, and that the files in the run's
    evidence folder, which are its bundle, are the same as the store's."""
    from .errors import CannotReadBack, ProveviError

    run_id = Path(run_folder).name
    ev = Path(run_folder) / "evidence"
    try:
        record = store.read_run(run_id, folder=ev)
    except CannotReadBack:
        return []  # a store that only writes leaves the check to the folder
    except ProveviError as e:  # RunNotFound, or a file that cannot be read
        return [f"in the store: {e}"]
    try:
        problems = [f"in the store: {p}" for p in verify(record, run_id=run_id)]
        if problems:
            return problems
        events = "".join(f"{line}\n" for line in record.lines()).encode()
        manifest = record.manifest_bytes()
        stored = store.run(run_id) if hasattr(store, "run") else None
    except ProveviError as e:
        return [f"in the store: {e}"]
    for name, want in (("events.jsonl", events), ("manifest.json", manifest)):
        try:
            same = (ev / name).read_bytes() == want
        except OSError:
            same = False
        if not same:
            problems.append(f"{name} in the run folder differs from the store")
    if stored is not None:
        try:
            noted = json.loads((Path(run_folder) / "bundle.json").read_bytes())
        except (OSError, ValueError, RecursionError):
            noted = {}
        noted = noted if isinstance(noted, dict) else {}
        # A store column left empty means the store did not record the bundle,
        # which provrun allows; a recorded value must match bundle.json.
        for key, col in (("uri", "bundle_uri"), ("pinned", "pinned_bundle")):
            if stored.get(col) is not None and noted.get(key) != stored.get(col):
                problems.append(f"the store's {col} differs from bundle.json")
    return problems


def verify_run(run_folder: Path, *, catalog: Catalog | Callable[[], Catalog] | None = None,
               bundle: str | None = None, require_bundle: bool = False,
               store: EvidenceStore | None = None) -> RunCheck:
    """Check a run as ``prove verify`` does: its evidence, then its outputs,
    then its committed bundle. With a store that keeps its own record, such as
    a SqliteStore, the run as the store holds it is checked too, and must
    match the folder. A malformed run gives problems, never an
    exception. bundle is the pinned URI of the bundle, taken
    from outside the run folder; without it, the one in bundle.json is used."""
    run_folder = Path(run_folder)
    check = RunCheck(run_folder.name)
    check.problems = verify(run_folder / "evidence", run_id=run_folder.name)
    if store is not None and not check.problems:
        from .store import FileStore

        if not isinstance(store, FileStore):
            check.problems = verify_store_copy(run_folder, store)
    if check.problems:
        return check
    check.problems = verify_outputs(run_folder)
    try:
        pinned = bundle_pin(run_folder, bundle)
    except EvidenceError as e:
        check.problems.append(str(e))
        return check
    check.problems += verify_bundle(run_folder, pinned, catalog, require=require_bundle)
    check.pinned = pinned
    return check
