import json
import shutil
from pathlib import Path

import pytest

import provevi
from provevi import (EvidenceError, EvidenceEvent, FileStore, RunNotFound, find_run, list_runs, read_events,
                     sha256_bytes, verify, verify_bundle, verify_outputs, verify_run)
from provevi.cli import main

RUN = "run-20261031T164032Z-451450d6"


def write_run(root: Path, run_id: str = RUN, types=("run_created", "run_completed"),
              outputs: dict[str, bytes] | None = None, payload: bytes | None = None) -> Path:
    """A run folder written through the file store, as provrun's recorder writes one."""
    folder = root / run_id
    (folder / "output").mkdir(parents=True)
    writer = FileStore().open_run(run_id, folder / "evidence")
    prev, seq = None, 0
    events = list(types[:-1])
    for rel in (outputs or {}):
        events.append("output_artifact_created")
    events.append(types[-1])
    out_items = iter((outputs or {}).items())
    for t in events:
        data, ref, refs = {}, None, ()
        if t == "output_artifact_created":
            rel, raw = next(out_items)
            (folder / "output" / rel).write_bytes(raw)
            data = {"path": f"/output/{rel}", "sha256": sha256_bytes(raw)}
            refs = (f"run://a/b/dev/{run_id}/output/{rel}",)
        if t == "run_created" and payload is not None:
            ref = writer.put_payload(payload)
        line = EvidenceEvent(run_id, seq, provevi.now(), t, "test", "you", refs, data, ref, "observed", prev).line()
        writer.append(line)
        prev, seq = sha256_bytes(line.encode()), seq + 1
    writer.close()
    writer.seal({"run_id": run_id, "events": seq, "last": prev, "payloads": writer.payload_names(),
                 "complete": True, "problems": []})
    return folder


def test_a_run_written_through_the_file_store_verifies(tmp_path):
    folder = write_run(tmp_path, payload=b"a prompt")
    assert verify(folder / "evidence", run_id=RUN) == []
    assert [e.type for e in read_events(folder / "evidence")] == ["run_created", "run_completed"]
    check = verify_run(folder)
    assert check.ok and check.pinned is None and check.to_dict()["run_id"] == RUN


def test_the_file_store_writes_each_payload_once(tmp_path):
    writer = FileStore().open_run(RUN, tmp_path / "evidence")
    a = writer.put_payload(b"x")
    assert writer.put_payload("x") == a == sha256_bytes(b"x")
    assert writer.payload_names() == [a] and writer.get_payload(a) == b"x"
    writer.close()
    writer.seal({"run_id": RUN})
    assert writer.manifest() == {"run_id": RUN}


@pytest.mark.parametrize("change, problem", [
    (lambda f: f.write_text(f.read_text().replace('"test"', '"other"', 1)), "chain is broken"),
    (lambda f: f.write_text(f.read_text().splitlines()[0] + "\n"), "last event"),
    (lambda f: (f.parent / "manifest.json").unlink(), "manifest.json is missing"),
])
def test_a_changed_record_fails(tmp_path, change, problem):
    folder = write_run(tmp_path)
    change(folder / "evidence" / "events.jsonl")
    assert any(problem in p for p in verify(folder / "evidence"))
    assert not verify_run(folder).ok


def test_a_changed_payload_fails(tmp_path):
    folder = write_run(tmp_path, payload=b"a prompt")
    [p] = (folder / "evidence" / "payloads").iterdir()
    p.write_bytes(b"another prompt")
    assert any("was changed" in p for p in verify(folder / "evidence"))


def test_outputs_must_be_exactly_the_recorded_ones(tmp_path):
    folder = write_run(tmp_path, outputs={"answer.txt": b"42"})
    assert verify_outputs(folder) == []
    (folder / "output" / "extra.txt").write_text("x")
    (folder / "output" / "answer.txt").write_text("41")
    assert verify_outputs(folder) == ["output extra.txt is not in the evidence", "output answer.txt was changed"]
    (folder / "output" / "answer.txt").unlink()
    assert "output answer.txt is missing" in verify_outputs(folder)


def test_the_bundle_check_opens_no_catalog_without_a_bundle(tmp_path):
    folder = write_run(tmp_path)

    def never():
        raise AssertionError("opened a catalog")

    assert verify_bundle(folder, None, never) == []
    assert verify_bundle(folder, None, never, require=True) == [
        "no committed bundle to compare with, and a bundle is required"]
    assert "not pinned" in verify_bundle(folder, "run://a/b/dev/x", never)[0]
    assert "no catalog" in verify_bundle(folder, "run://a/b/dev/x?at=commit:abc", None)[0]


def test_the_bundle_check_compares_with_the_committed_files(tmp_path):
    folder = write_run(tmp_path)
    ev = folder / "evidence"

    class Catalog:
        def __init__(self, events):
            self.files = {"events.jsonl": events, "manifest.json": (ev / "manifest.json").read_bytes()}

        def read(self, uri):
            return self.files[str(uri).split("?")[0].rsplit("/", 1)[-1]]

    pinned = f"run://a/b/dev/{RUN}?at=commit:abc"
    assert verify_bundle(folder, pinned, Catalog((ev / "events.jsonl").read_bytes())) == []
    assert verify_bundle(folder, pinned, lambda: Catalog(b"other")) == [
        "events.jsonl differs from the committed bundle"]
    check = verify_run(folder, catalog=Catalog(b"other"), bundle=pinned)
    assert not check.ok and check.pinned == pinned


def test_bundle_json_names_the_bundle_unless_one_is_given(tmp_path):
    folder = write_run(tmp_path)
    (folder / "bundle.json").write_text(json.dumps({"pinned": "run://a/b/dev/x?at=commit:1"}))
    assert provevi.bundle_pin(folder) == "run://a/b/dev/x?at=commit:1"
    assert provevi.bundle_pin(folder, "run://a/b/dev/x?at=commit:2") == "run://a/b/dev/x?at=commit:2"


def test_find_run_by_id_or_path(tmp_path):
    folder = write_run(tmp_path)
    assert find_run(RUN, tmp_path) == folder.resolve()
    assert find_run(folder, tmp_path / "elsewhere") == folder.resolve()
    with pytest.raises(RunNotFound):
        find_run(folder, tmp_path / "elsewhere", ids_only=True)
    with pytest.raises(RunNotFound):
        find_run("run-nothing", tmp_path)
    assert list_runs(tmp_path) == [RUN] and list_runs(tmp_path / "none") == []


def test_read_events_refuses_a_line_that_is_not_an_event(tmp_path):
    folder = write_run(tmp_path)
    with (folder / "evidence" / "events.jsonl").open("a") as f:
        f.write("not json\n")
    with pytest.raises(EvidenceError, match="line 2"):
        read_events(folder / "evidence")


def test_prove_lists_shows_and_verifies(tmp_path, capsys):
    folder = write_run(tmp_path)
    assert main(["--runs", str(tmp_path), "runs"]) == 0
    assert capsys.readouterr().out.split() == [RUN]
    assert main(["--runs", str(tmp_path), "show", RUN]) == 0
    assert "run_created" in capsys.readouterr().out
    assert main(["--runs", str(tmp_path), "verify", RUN]) == 0
    assert "the evidence and the outputs verify; no committed bundle" in capsys.readouterr().out
    assert main(["--runs", str(tmp_path), "verify", RUN, "--require-bundle"]) == 1
    assert main(["--runs", str(tmp_path), "verify", "run-nothing"]) == 1
    with (folder / "evidence" / "events.jsonl").open("a") as f:
        f.write("not json\n")
    assert main(["--runs", str(tmp_path), "show", RUN]) == 2
    assert main(["--runs", str(tmp_path), "verify", RUN]) == 1


OLD = Path(__file__).parent / "data" / "run-20261031T182026Z-f9c3b25f"


def test_a_run_written_before_provevi_still_verifies(tmp_path, capsys):
    """data/ holds a run that provr wrote at commit ce09b72, before provevi existed."""
    assert verify_run(OLD).ok
    assert main(["--runs", str(OLD.parent), "verify", OLD.name]) == 0
    copy = tmp_path / OLD.name
    shutil.copytree(OLD, copy)
    (copy / "output" / "response.txt").write_text("tampered")
    assert verify_run(copy).problems == ["output response.txt was changed"]


# Each test below is one finding of the review of Phase 1.

def rechain(folder: Path, edit) -> None:
    """Apply edit to the events, as dicts, and write a new chain and manifest
    that agree with them, as someone who can write the folder could."""
    ev = folder / "evidence"
    events = [json.loads(line) for line in (ev / "events.jsonl").read_text().splitlines()]
    edit(events)
    prev, lines = None, []
    for n, e in enumerate(events):
        if isinstance(e, dict):
            e.update(seq=n, prev=prev)
        lines.append(provevi.canonical(e))
        prev = sha256_bytes(lines[-1].encode())
    (ev / "events.jsonl").write_text("".join(f"{x}\n" for x in lines))
    manifest = json.loads((ev / "manifest.json").read_text())
    manifest.update(events=len(lines), last=prev)
    (ev / "manifest.json").write_text(json.dumps(manifest))


def copy_old(tmp_path) -> Path:
    folder = tmp_path / OLD.name
    shutil.copytree(OLD, folder)
    return folder


def output_event(events):
    return next(e for e in events if e["type"] == "output_artifact_created")


def test_rechain_alone_keeps_a_run_valid(tmp_path):
    folder = copy_old(tmp_path)
    rechain(folder, lambda events: None)
    assert verify_run(folder).ok


@pytest.mark.parametrize("name, change, problem", [
    ("bundle.json not JSON", lambda f: (f / "bundle.json").write_text("{not json"), "bundle.json is not JSON"),
    ("bundle.json a list", lambda f: (f / "bundle.json").write_text("[]"), "not a JSON object"),
    ("pinned a number", lambda f: (f / "bundle.json").write_text('{"pinned": 5}'), 'string "pinned"'),
    ("pinned not a URI", lambda f: (f / "bundle.json").write_text('{"pinned": "garbage"}'), "not a catalog URI"),
    ("manifest a list", lambda f: (f / "evidence" / "manifest.json").write_text("[]"), "not a JSON object"),
    ("manifest problems not text", lambda f: (f / "evidence" / "manifest.json").write_text(
        (f / "evidence" / "manifest.json").read_text().replace('"complete": true', '"complete": false')
        .replace('"problems": []', '"problems": [1]')), "the recorder lost events: 1"),
    ("events not UTF-8", lambda f: (f / "evidence" / "events.jsonl").write_bytes(b"\xff\n"), "cannot read"),
    ("events a folder", lambda f: ((f / "evidence" / "events.jsonl").unlink(),
                                   (f / "evidence" / "events.jsonl").mkdir()), "cannot read"),
    ("output data empty", lambda f: rechain(f, lambda es: output_event(es).update(data={})), "has no sha256"),
    ("output data text", lambda f: rechain(f, lambda es: output_event(es).update(data="x")), "not a JSON object"),
    ("a line that is a number", lambda f: rechain(f, lambda es: es.__setitem__(2, 1)), "not a JSON object"),
    ("a line that is a list", lambda f: rechain(f, lambda es: es.__setitem__(2, [])), "not a JSON object"),
    ("refs not text", lambda f: rechain(f, lambda es: es[2].update(refs=[1, 2])), "refs is not a list of strings"),
    ("type null", lambda f: rechain(f, lambda es: es[2].update(type=None)), "type is not a string"),
    ("deep events", lambda f: (f / "evidence" / "events.jsonl").write_text("[" * 100000 + "]" * 100000 + "\n"),
     "nested too deeply"),
    ("deep manifest", lambda f: (f / "evidence" / "manifest.json").write_text("[" * 100000 + "]" * 100000),
     "manifest.json is not JSON"),
    ("payload a number", lambda f: rechain(f, lambda es: es[2].update(payload=5)), "payload is not a string"),
    ("payload ..", lambda f: rechain(f, lambda es: es[2].update(payload="..")), "is not a SHA-256"),
])
def test_a_malformed_run_gives_problems_not_an_exception(tmp_path, name, change, problem):
    folder = copy_old(tmp_path)
    change(folder)
    check = verify_run(folder)
    assert not check.ok and any(problem in p for p in check.problems), check.problems


def test_a_given_bundle_is_used_without_reading_bundle_json(tmp_path):
    folder = copy_old(tmp_path)
    (folder / "bundle.json").write_text("{not json")
    assert provevi.bundle_pin(folder, "run://a/b/dev/x?at=commit:1") == "run://a/b/dev/x?at=commit:1"


@pytest.mark.parametrize("bundle, problem", [
    ("bogus", "not a catalog URI"),
    ("run://a/b/dev/x?at=commit:1", "cannot open the catalog"),
])
def test_a_bad_bundle_or_config_is_a_problem(tmp_path, capsys, bundle, problem):
    folder = copy_old(tmp_path)
    args = ["verify", str(folder), "--bundle", bundle, "--config", str(tmp_path / "none.yaml")]
    assert main(args) == 1
    assert problem in capsys.readouterr().err


def test_show_without_events_exits_2(tmp_path, capsys):
    folder = copy_old(tmp_path)
    (folder / "evidence" / "events.jsonl").unlink()
    assert main(["show", str(folder)]) == 2
    assert "is missing" in capsys.readouterr().err


def test_a_special_file_in_the_outputs_fails(tmp_path):
    import os

    folder = copy_old(tmp_path)
    os.mkfifo(folder / "output" / "pipe")
    assert "output pipe is not a regular file" in verify_run(folder).problems


def test_a_payload_the_manifest_does_not_list_fails(tmp_path):
    folder = copy_old(tmp_path)
    extra = folder / "evidence" / "payloads" / sha256_bytes(b"x")
    extra.write_bytes(b"x")
    assert f"payload {extra.name} is not in the manifest" in verify_run(folder).problems
    extra.unlink()
    [listed] = [p for p in (folder / "evidence" / "payloads").iterdir()][:1]
    listed.unlink()
    assert any("missing" in p for p in verify(folder / "evidence"))


def test_an_output_outside_output_or_recorded_twice_fails(tmp_path):
    folder = copy_old(tmp_path)
    rechain(folder, lambda es: output_event(es).update(refs=["run://acme/demo/dev/other-run/output/response.txt"]))
    assert any("names no one output of the run" in p for p in verify_run(folder).problems)
    folder = copy_old(tmp_path / "2")
    rechain(folder, lambda es: es.insert(-1, dict(output_event(es))))
    assert "output response.txt is recorded more than once" in verify_run(folder).problems


def test_a_link_under_the_runs_root_is_not_a_run(tmp_path, capsys):
    copy_old(tmp_path)
    (tmp_path / "run-20990101T000000Z-deadbeef").symlink_to(OLD.name)
    assert list_runs(tmp_path) == [OLD.name]
    for ids_only in (False, True):
        with pytest.raises(RunNotFound, match="is a link"):
            find_run("run-20990101T000000Z-deadbeef", tmp_path, ids_only=ids_only)
    assert main(["--runs", str(tmp_path), "verify", "run-20990101T000000Z-deadbeef"]) == 1


# Each test below is one finding of the second review of Phase 1.

def test_show_refuses_events_that_are_not_events(tmp_path, capsys):
    folder = copy_old(tmp_path)
    rechain(folder, lambda es: es[2].update(component=None))
    assert main(["show", str(folder)]) == 2
    assert "component is not a string" in capsys.readouterr().err


def test_an_unreadable_payloads_folder_is_a_problem(tmp_path):
    folder = copy_old(tmp_path)
    payloads = folder / "evidence" / "payloads"
    payloads.chmod(0)
    try:
        problems = verify_run(folder).problems
    finally:
        payloads.chmod(0o755)
    assert any("cannot" in p for p in problems), problems


def test_hidden_files_in_payloads_are_left_out(tmp_path):
    folder = copy_old(tmp_path)
    (folder / "evidence" / "payloads" / ".DS_Store").write_bytes(b"x")
    (folder / "evidence" / "payloads" / ".tmp-123").write_bytes(b"x")
    assert verify_run(folder).ok


def test_an_unreadable_output_folder_is_a_problem(tmp_path):
    folder = copy_old(tmp_path)
    sub = folder / "output" / "sub"
    sub.mkdir()
    (sub / "x").write_text("hidden")
    sub.chmod(0)
    try:
        problems = verify_run(folder).problems
    finally:
        sub.chmod(0o755)
    assert "output sub cannot be read" in problems


def test_an_output_folder_elsewhere_in_the_sandbox_verifies(tmp_path):
    """A stager may put the agent's output folder anywhere; the event's
    reference names the file in the run."""
    folder = copy_old(tmp_path)
    rechain(folder, lambda es: output_event(es)["data"].update(path="/workspace/out/response.txt"))
    assert verify_run(folder).ok
