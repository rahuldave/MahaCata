"""The runnable tour: tours/provrun/run.sh runs, and docs/provrun-tour.md shows its output.

The script starts its own moto server and collector on free ports, in a
scratch folder, and deletes the folder when it ends. Step 13 calls a real
model, so these tests run the script without --online, and compare the steps
before it with the page.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOURS = ROOT / "tours" / "provrun"
SCRIPT = TOURS / "run.sh"
PAGE = ROOT / "docs" / "provrun-tour.md"
FEATURES = ROOT / "docs" / "provrun-features.md"
ONLINE = "Step 13"
PAGE_DIR = "/tmp/provrun-tour"
PAGE_OTLP_PORT = "4318"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("curl") is None,
                                reason="the tour needs bash and curl")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(tour_dir: Path, **extra) -> dict:
    # Leave out the variables that would change what a run records, and the
    # keys, so that the script's output does not depend on this shell.
    drop = ("PROVCAT_", "PROVRUN_", "AWS_", "TOUR_", "OTEL_", "LOGFIRE_", "MLFLOW_", "LITELLM_", "PG")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(drop) and not k.endswith("_API_KEY") and "PROXY" not in k.upper()}
    env.update(TOUR_DIR=str(tour_dir), TOUR_S3_PORT=str(_free_port()), TOUR_OTLP_PORT=str(_free_port()))
    env.update(extra)
    return env


class _Normal:
    """Replaces the parts that change on each run. Each distinct run ID, hex
    string and UUID gets its own number, in the order it first appears, so
    that the page must show the same value where the script did: R1's run ID,
    and not R2's, or one parent span for every step."""

    PATTERNS = [("RUN", r"run-\d{8}T\d{6}Z-[0-9a-f]{8}"),
                ("UUID", r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
                ("HEX", r"\b[0-9a-f]{12,64}\b")]

    def __init__(self, tour_dir: str, otlp_port: str = PAGE_OTLP_PORT, numbered: bool = True):
        self.tour_dir, self.otlp_port, self.numbered = tour_dir, otlp_port, numbered
        self.seen: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        for d in sorted({self.tour_dir, os.path.realpath(self.tour_dir)}, key=len, reverse=True):
            text = text.replace(d, "<TOUR>")
        text = text.replace("/private<TOUR>", "<TOUR>")
        text = text.replace(f"127.0.0.1:{self.otlp_port}", "127.0.0.1:<OTLP>")
        text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)", "<TIME>", text)
        for kind, pattern in self.PATTERNS:
            text = re.sub(pattern, lambda m, kind=kind: self._name(kind, m.group(0)), text)
        return text

    def _name(self, kind: str, value: str) -> str:
        if not self.numbered:
            return f"<{kind}>"
        if value not in self.seen:
            self.seen[value] = f"<{kind}{sum(v.startswith('<' + kind) for v in self.seen.values()) + 1}>"
        return self.seen[value]


def _script_steps(out: str) -> dict[str, str]:
    parts = re.split(r"^==== (.*)$", out, flags=re.M)
    return {parts[i]: parts[i + 1].strip("\n") for i in range(1, len(parts), 2)}


def _blocks(text: str) -> list[str]:
    """The plain fenced blocks of text, those with no language."""
    blocks, block, fence = [], [], None
    for line in text.splitlines():
        if fence is None and line.startswith("```"):
            fence, block = line, []
        elif fence is not None and line == "```":
            if fence == "```":
                blocks.append("\n".join(block).strip("\n"))
            fence = None
        elif fence is not None:
            block.append(line)
    return blocks


def _page_steps(page: str) -> dict[str, str]:
    """Each step's plain fenced blocks. A block holds whole commands with
    their output, so the blocks are joined as the script parts commands."""
    return {title: "\n\n".join(_blocks(body))
            for title, body in re.findall(r"^## (Step \d+:[^\n]*)\n(.*?)(?=^## |\Z)", page, flags=re.M | re.S)}


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tour_dir = tmp_path_factory.mktemp("tour") / "provrun-tour"
    env = _env(tour_dir)
    done = subprocess.run(["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
    return done, tour_dir, env["TOUR_OTLP_PORT"]


def test_the_tour_runs_and_cleans_up(run):
    done, tour_dir, _ = run
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert not tour_dir.exists()


def test_the_page_shows_the_output_of_the_script(run):
    done, tour_dir, port = run
    script = _script_steps(done.stdout)
    page = {t: b for t, b in _page_steps(PAGE.read_text()).items() if not t.startswith(ONLINE)}
    assert list(page) == list(script)
    shown_normal, ran_normal = _Normal(PAGE_DIR), _Normal(str(tour_dir), port)
    for title in script:  # in order, so that each value keeps its number across the steps
        assert shown_normal(page[title]) == ran_normal(script[title]), title


def test_the_features_page_shows_output_of_the_script(run):
    done, tour_dir, port = run
    ran = _Normal(str(tour_dir), port, numbered=False)(done.stdout)
    shown = _Normal(PAGE_DIR, numbered=False)
    page = FEATURES.read_text()
    blocks = _blocks(page)
    assert blocks
    for block in blocks:
        assert shown(block) in ran, block[:200]


def test_the_page_shows_the_online_commands_of_the_script():
    online = next(b for t, b in _page_steps(PAGE.read_text()).items() if t.startswith(ONLINE))
    shown = [line[2:] for line in online.splitlines() if line.startswith("$ ")]
    body = SCRIPT.read_text().split('if [ "${1:-}" = "--online" ]', 1)[1]
    ran = [line.split("show ", 1)[1][1:-1] for line in body.splitlines() if "show " in line]
    assert shown == ran


ROUND_TRIP = (
    'env | sort > before; set | grep "^PROVRUN_RUNS=" > before_local; '
    'source tours/provrun/start.sh > /dev/null; '
    'servers="$(pgrep -P "$(cat "$TOUR_DIR/moto.pid")") $(pgrep -P "$(cat "$TOUR_DIR/collector.pid")")"; '
    'source tours/provrun/stop.sh; '
    'env | sort > after; set | grep "^PROVRUN_RUNS=" > after_local; '
    'diff before after | grep -v "^[<>] _=" | grep "^[<>]"; diff before_local after_local; '
    'printenv PROVRUN_RUNS && echo "PROVRUN_RUNS was exported"; '
    'for p in $servers; do kill -0 $p 2> /dev/null && echo "a server still runs"; done; rm before after before_local after_local; true'
)


@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_start_and_stop_give_back_the_shell(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"no {shell}")
    tour_dir = tmp_path / "t"
    env = _env(tour_dir, AWS_PROFILE="keep it", PROVCAT_CONFIG="/my.yaml",
               OTEL_EXPORTER_OTLP_ENDPOINT="http://mine:4318", HTTPS_PROXY="http://proxy.example:3128")
    # A shell-only variable that start.sh exports must come back shell-only.
    done = subprocess.run([shell, "-c", "PROVRUN_RUNS='a b $x'; " + ROUND_TRIP], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=300)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert not tour_dir.exists()


def test_a_failed_start_cleans_up(tmp_path):
    tour_dir = tmp_path / "t"
    done = subprocess.run(["bash", "-c", 'source tours/provrun/start.sh; rc=$?; env | grep -c "^PROVRUN_RUNS="; exit $rc'],
                          cwd=ROOT, env=_env(tour_dir, TOUR_OTLP_PORT="abc"), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 1
    assert "must be a number" in done.stderr
    assert done.stdout.strip() == "0"
    assert not tour_dir.exists()


def test_a_second_start_leaves_the_running_tour_alone(tmp_path):
    tour_dir = tmp_path / "t"
    script = ('source tours/provrun/start.sh > /dev/null; source tours/provrun/start.sh; echo "rc=$?"; '
              'test -f "$TOUR_DIR/.provrun-tour" && echo "folder kept"; '
              'curl -s -m 2 -o /dev/null "http://127.0.0.1:$TOUR_OTLP_PORT" && echo "collector runs"; '
              'echo "config=$PROVCAT_CONFIG pghost=${PGHOST:-}"; source tours/provrun/stop.sh; '
              'echo "back=$PGHOST"')
    # PGHOST would show in the runs' redirects, so the tour unsets it, and stop.sh puts it back.
    done = subprocess.run(["bash", "-c", script], cwd=ROOT, env=_env(tour_dir, PGHOST="db.example"),
                          capture_output=True, text=True, timeout=300)
    assert "a tour already runs in this shell" in done.stderr
    assert "rc=1" in done.stdout and "folder kept" in done.stdout and "collector runs" in done.stdout
    assert f"config={tour_dir}/provcat.yaml pghost=\n" in done.stdout
    assert "back=db.example" in done.stdout
    assert not tour_dir.exists()


def test_start_refuses_a_folder_that_it_did_not_make(tmp_path):
    tour_dir = tmp_path / "mine"
    tour_dir.mkdir()
    (tour_dir / "keep.txt").write_text("keep")
    done = subprocess.run(["bash", "-c", "source tours/provrun/start.sh"], cwd=ROOT, env=_env(tour_dir),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "is not a tour folder" in done.stderr
    subprocess.run(["bash", "-c", "source tours/provrun/stop.sh"], cwd=ROOT, env=_env(tour_dir),
                   capture_output=True, text=True, timeout=60)
    assert (tour_dir / "keep.txt").read_text() == "keep"


def test_start_refuses_a_port_in_use(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        done = subprocess.run(["bash", "-c", "source tours/provrun/start.sh"], cwd=ROOT,
                              env=_env(tmp_path / "t", TOUR_OTLP_PORT=str(port)),
                              capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert f"port {port} is in use" in done.stderr
    assert not (tmp_path / "t").exists()
