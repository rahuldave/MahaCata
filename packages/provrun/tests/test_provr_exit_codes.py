"""provr's exit codes, as docs/provrun.md's table of the command `provr` gives them."""
import json
from pathlib import Path

import pytest

import provevi.store
from provrun.cli import main as provr

GOOD = """
run: {org: acme, domain: benefits, env: dev, actor: tester}
agent:
  name: q
  backend: litellm
  model: openai/gpt-x
  prompt: What was revenue?
  inputs: [INPUT]
  settings: {mock_response: Revenue was 42 million.}
"""


def write(tmp_path, uri):
    f = tmp_path / "run.yaml"
    f.write_text(GOOD.replace("INPUT", uri))
    return f


def test_a_completed_run_and_its_show_and_verify_exit_0(tmp_path, runs, docs, capsys):
    assert provr(["--runs", str(runs), "run", str(write(tmp_path, f"file://{docs}/report.txt"))]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    assert provr(["--runs", str(runs), "show", run_id]) == 0
    assert provr(["--runs", str(runs), "verify", run_id]) == 0


def test_a_failed_run_exits_1(tmp_path, runs, capsys):
    assert provr(["--runs", str(runs), "run", str(write(tmp_path, f"file://{tmp_path}/missing.txt"))]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_a_run_whose_evidence_cannot_be_sealed_exits_2(tmp_path, runs, docs, monkeypatch, capsys):
    def full(self, *a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(provevi.store.FileRunWriter, "seal", full)
    assert provr(["--runs", str(runs), "run", str(write(tmp_path, f"file://{docs}/report.txt"))]) == 2
    assert "provr:" in capsys.readouterr().err


def test_show_exits_2_on_a_line_that_is_not_an_event(tmp_path, runs, docs, capsys):
    assert provr(["--runs", str(runs), "run", str(write(tmp_path, f"file://{docs}/report.txt"))]) == 0
    folder = Path(json.loads(capsys.readouterr().out)["folder"])
    events = folder / "evidence" / "events.jsonl"
    events.chmod(0o644)
    with events.open("a") as f:
        f.write("not an event\n")
    assert provr(["--runs", str(runs), "show", str(folder)]) == 2


def test_a_command_line_that_argparse_rejects_exits_2(runs):
    with pytest.raises(SystemExit) as e:
        provr(["--runs", str(runs), "run", "x.yaml", "--mode", "loose"])
    assert e.value.code == 2
