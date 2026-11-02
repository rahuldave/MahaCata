"""The runnable tour: tours/provevi/run.sh runs, and docs/provevi-tour.md shows its output.

The script makes its own scratch folder, runs provrun with a mocked model,
and deletes the folder when it ends. It needs no server and no network.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tours" / "provevi" / "run.sh"
PAGE = ROOT / "docs" / "provevi-tour.md"
PAGE_DIR = "/tmp/provevi-tour"

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(shutil.which("bash") is None, reason="the tour needs bash")]


def _env(tour_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PROVRUN_", "PROVCAT_", "MLFLOW_", "OTEL_", "LOGFIRE_", "LITELLM_", "TOUR_"))}
    env["TOUR_DIR"] = str(tour_dir)
    return env


def _normal(text: str, tour_dir: str) -> str:
    """The text with the parts that change on each run replaced."""
    for d in sorted({tour_dir, os.path.realpath(tour_dir)}, key=len, reverse=True):
        text = text.replace(d, "<TOUR>")
    text = text.replace("/private<TOUR>", "<TOUR>")
    text = re.sub(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}", "<RUN>", text)
    text = re.sub(r"\b[0-9a-f]{32}\b", "<ID>", text)
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", "<TIME>", text)
    return re.sub(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", "<TIME>", text)


def _script_steps(out: str) -> dict[str, str]:
    parts = re.split(r"^==== (.*)$", out, flags=re.M)
    return {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts), 2)}


def _page_steps(page: str) -> dict[str, str]:
    steps = {}
    for title, body in re.findall(r"^## (Step \d+:[^\n]*)\n(.*?)(?=^## |\Z)", page, flags=re.M | re.S):
        # Only the plain blocks hold output; a python or yaml block shows a file.
        blocks = re.findall(r"^```(\w*)\n(.*?)^```$", body, flags=re.M | re.S)
        steps[title] = "\n".join(b.rstrip("\n") for lang, b in blocks if not lang)
    return steps


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tour_dir = tmp_path_factory.mktemp("tour") / "provevi-tour"
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=_env(tour_dir),
                          capture_output=True, text=True, timeout=900)
    return done, tour_dir


def test_the_tour_runs_and_cleans_up(run):
    done, tour_dir = run
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert not tour_dir.exists()


def test_the_page_shows_the_output_of_the_script(run):
    done, tour_dir = run
    script = _script_steps(done.stdout)
    page = _page_steps(PAGE.read_text())
    assert list(page) == list(script)
    for title in script:
        assert _normal(page[title], PAGE_DIR) == _normal(script[title], str(tour_dir)), title


def test_mlflow_writes_no_note_to_the_terminal(run):
    done, _ = run
    assert "mlflow.store.db.utils" not in done.stdout


ROUND_TRIP = (
    'env | sort > before; set | grep "^PROVRUN_RUNS=" > before_local; '
    'source tours/provevi/start.sh > /dev/null; '
    'test -f "$TOUR_DIR/run.yaml" && test -z "${MLFLOW_TRACKING_URI:-}" || echo "start did not set up"; '
    'source tours/provevi/stop.sh; '
    'env | sort > after; set | grep "^PROVRUN_RUNS=" > after_local; '
    'diff before after | grep -v "^[<>] _=" | grep "^[<>]"; diff before_local after_local; '
    'printenv PROVRUN_RUNS && echo "PROVRUN_RUNS was exported"; '
    'rm before after before_local after_local; true'
)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_start_and_stop_give_back_the_shell(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"no {shell}")
    tour_dir = tmp_path / "t"
    env = {**_env(tour_dir), "MLFLOW_TRACKING_URI": "https://mine.example", "OTEL_SERVICE_NAME": "keep me",
           "PROVCAT_CONFIG": "/my.yaml"}
    done = subprocess.run([shell, "-c", "PROVRUN_RUNS='a b $x'; " + ROUND_TRIP], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert not tour_dir.exists()


def test_start_refuses_a_folder_that_it_did_not_make(tmp_path):
    tour_dir = tmp_path / "mine"
    tour_dir.mkdir()
    (tour_dir / "keep.txt").write_text("keep")
    done = subprocess.run(["bash", "-c", "source tours/provevi/start.sh"], cwd=ROOT, env=_env(tour_dir),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "is not a tour folder" in done.stderr
    subprocess.run(["bash", "-c", "source tours/provevi/stop.sh"], cwd=ROOT, env=_env(tour_dir),
                   capture_output=True, text=True, timeout=60)
    assert (tour_dir / "keep.txt").read_text() == "keep"


def test_start_refuses_a_relative_folder(tmp_path):
    env = {**_env(tmp_path), "TOUR_DIR": "relative/tour"}
    done = subprocess.run(["bash", "-c", "source tours/provevi/start.sh"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "must be an absolute path" in done.stderr
    assert not (ROOT / "relative").exists()


@pytest.mark.parametrize("second", ["", 'TOUR_DIR="$TOUR_DIR-2"; '])
def test_a_second_start_leaves_the_running_tour_alone(tmp_path, second):
    # A second start in the same folder, or in another, once stopped the first.
    tour_dir = tmp_path / "t"
    script = ('source tours/provevi/start.sh; first="$TOUR_DIR"; '
              f'{second}source tours/provevi/start.sh; echo "rc=$?"; '
              'TOUR_DIR="$first"; test -f "$TOUR_DIR/run.yaml" && echo "folder kept"; echo "store=$STORE"; '
              'source tours/provevi/stop.sh; '
              'type _tour_start _tour_stop > /dev/null 2>&1 && echo "functions left"; true')
    done = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_env(tour_dir), capture_output=True, text=True,
                          timeout=120)
    assert "a tour already runs in this shell" in done.stderr
    assert done.stdout.splitlines() == ["rc=1", "folder kept", f"store=sqlite:///{tour_dir}/evidence.db"]
    assert not tour_dir.exists() and not (tmp_path / "t-2").exists()


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_a_failed_start_cleans_up_in_a_shell_with_set_e(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"no {shell}")
    # A sed that fails makes the start fail after it saved the shell.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sed").write_text("#!/bin/sh\nexit 4\n")
    (bin_dir / "sed").chmod(0o755)
    tour_dir = tmp_path / "t"
    env = {**_env(tour_dir), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    done = subprocess.run([shell, "-c", "set -e; source tours/provevi/start.sh"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode != 0
    assert not tour_dir.exists()


def test_start_refuses_to_run_from_another_folder(tmp_path):
    done = subprocess.run(["bash", "-c", f"source {ROOT}/tours/provevi/start.sh; echo rc=$?; echo dir=$TOUR_DIR"],
                          cwd=tmp_path, env=_env(tmp_path / "t"), capture_output=True, text=True, timeout=60)
    assert "run it from the top folder" in done.stderr
    assert done.stdout.splitlines() == ["rc=1", f"dir={tmp_path / 't'}"]
    assert not (tmp_path / "t").exists()


def test_stop_stops_the_tour_that_started_after_tour_dir_changes(tmp_path):
    tour_dir = tmp_path / "t"
    done = subprocess.run(["bash", "-c", 'source tours/provevi/start.sh; TOUR_DIR=/elsewhere; '
                                         'source tours/provevi/stop.sh; echo "dir=${TOUR_DIR}"'],
                          cwd=ROOT, env=_env(tour_dir), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert not tour_dir.exists()
    assert done.stdout.strip() == f"dir={tour_dir}"
