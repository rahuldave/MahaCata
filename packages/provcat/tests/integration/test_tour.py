"""The runnable tour: tours/provcat/run.sh runs, and docs/tour.md shows its output.

The script starts its own moto server on a free port, in a scratch folder, and
deletes the folder when it ends. Step 11 needs the network, so these tests run
the script without --online and compare the steps before it with the page.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
TOURS = ROOT / "tours" / "provcat"
SCRIPT = TOURS / "run.sh"
PAGE = ROOT / "docs" / "tour.md"
ONLINE = "Step 11"
PAGE_DIR = "/tmp/provcat-tour"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("curl") is None,
                                reason="the tour needs bash and curl")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(tour_dir: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PROVCAT_", "AWS_", "TOUR_"))}
    env.update(TOUR_DIR=str(tour_dir), TOUR_S3_PORT=str(_free_port()))
    env.update(extra)
    return env


def _normal(text: str, tour_dir: str) -> str:
    """The text with the parts that change on each run replaced."""
    for d in sorted({tour_dir, os.path.realpath(tour_dir)}, key=len, reverse=True):
        text = text.replace(d, "<TOUR>")
    text = text.replace("/private<TOUR>", "<TOUR>")
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>", text)
    text = re.sub(r"\b[0-9a-f]{64}\b", "<SHA256>", text)
    return re.sub(r"\b[0-9a-f]{32}\b", "<ETAG>", text)


def _script_steps(out: str) -> dict[str, str]:
    parts = re.split(r"^==== (.*)$", out, flags=re.M)
    return {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts), 2)}


def _page_steps(page: str) -> dict[str, str]:
    steps = {}
    for title, body in re.findall(r"^## (Step \d+:[^\n]*)\n(.*?)(?=^## |\Z)", page, flags=re.M | re.S):
        blocks = re.findall(r"^```\n(.*?)^```$", body, flags=re.M | re.S)
        steps[title] = "\n".join(b.rstrip("\n") for b in blocks)
    return steps


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tour_dir = tmp_path_factory.mktemp("tour") / "provcat-tour"
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=_env(tour_dir),
                          capture_output=True, text=True, timeout=600)
    return done, tour_dir


def test_the_tour_runs_and_cleans_up(run):
    done, tour_dir = run
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert not tour_dir.exists()


def test_the_page_shows_the_output_of_the_script(run):
    done, tour_dir = run
    script = _script_steps(done.stdout)
    page = {t: b for t, b in _page_steps(PAGE.read_text()).items() if not t.startswith(ONLINE)}
    assert list(page) == list(script)
    for title in script:
        shown = _normal(page[title], PAGE_DIR)
        ran = _normal(script[title], str(tour_dir))
        assert shown == ran, title


def test_the_page_shows_the_online_commands_of_the_script():
    online = next(b for t, b in _page_steps(PAGE.read_text()).items() if t.startswith(ONLINE))
    shown = [line[2:] for line in online.splitlines() if line.startswith("$ ")]
    body = SCRIPT.read_text().split('if [ "${1:-}" = "--online" ]', 1)[1]
    ran = [line.split("show ", 1)[1].replace('"', "") for line in body.splitlines() if "show " in line]
    assert shown == ran


def _shell(script: str, tour_dir: Path, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=ROOT, env=_env(tour_dir, **extra),
                          capture_output=True, text=True, timeout=300)


ROUND_TRIP = (
    'env | sort > before; set | grep "^PROVCAT_TENANT=" > before_local; '
    'source tours/provcat/start.sh > /dev/null; '
    'uv run provc put files://sales/a.csv tours/provcat/provcat.yaml > /dev/null; '
    'moto=$(pgrep -P "$(cat "$TOUR_DIR/moto.pid")"); source tours/provcat/stop.sh; '
    'env | sort > after; set | grep "^PROVCAT_TENANT=" > after_local; '
    'diff before after | grep -v "^[<>] _=" | grep "^[<>]"; diff before_local after_local; '
    'printenv PROVCAT_TENANT && echo "PROVCAT_TENANT was exported"; '
    'kill -0 $moto 2> /dev/null && echo "moto still runs"; rm before after before_local after_local; true'
)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_start_and_stop_give_back_the_shell(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"no {shell}")
    tour_dir = tmp_path / "t"
    script = "PROVCAT_TENANT='a b $x'; " + ROUND_TRIP
    done = subprocess.run([shell, "-c", script], cwd=ROOT,
                          env=_env(tour_dir, AWS_PROFILE="keep it", PROVCAT_CONFIG="/my.yaml"),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert not tour_dir.exists()


def test_a_failed_start_cleans_up(tmp_path):
    tour_dir = tmp_path / "t"
    done = subprocess.run(["bash", "-c", 'source tours/provcat/start.sh; rc=$?; env | grep -c "^PROVCAT_CONFIG=" ; exit $rc'],
                          cwd=ROOT, env=_env(tour_dir, TOUR_S3_PORT="abc"), capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "must be a number" in done.stderr
    assert done.stdout.strip() == "0"
    assert not tour_dir.exists()


def test_run_cleans_up_when_moto_does_not_start(tmp_path):
    # A broken uv on the PATH makes moto fail to start after start.sh saved the shell.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_uv = shutil.which("uv")
    (bin_dir / "uv").write_text(
        '#!/bin/sh\n[ "$2" = moto_server ] && exit 3\nexec ' + real_uv + ' "$@"\n')
    (bin_dir / "uv").chmod(0o755)
    tour_dir = tmp_path / "t"
    env = _env(tour_dir)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0
    assert "moto did not start" in done.stderr
    assert not tour_dir.exists()


def test_start_refuses_a_folder_that_it_did_not_make(tmp_path):
    tour_dir = tmp_path / "mine"
    tour_dir.mkdir()
    (tour_dir / "keep.txt").write_text("keep")
    done = _shell("source tours/provcat/start.sh", tour_dir)
    assert done.returncode == 1
    assert "is not a tour folder" in done.stderr
    assert (tour_dir / "keep.txt").read_text() == "keep"
    done = _shell("source tours/provcat/stop.sh", tour_dir)
    assert (tour_dir / "keep.txt").exists()


def test_start_refuses_a_port_in_use(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        env = {**_env(tmp_path / "t"), "TOUR_S3_PORT": str(port)}
        done = subprocess.run(["bash", "-c", "source tours/provcat/start.sh"], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert f"port {port} is in use" in done.stderr
    assert not (tmp_path / "t").exists()
