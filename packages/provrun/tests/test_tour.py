"""The runnable tour: tours/provrun/run.sh runs, and docs/provrun-tour.md shows its output.

The script starts its own moto server and collector on free ports, in a
scratch folder, and deletes the folder when it ends. Steps 14 and 15 call a
real model, so these tests run the script without --online, and compare the
steps before them with the page.
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
OVERVIEW = ROOT / "docs" / "overview.md"  # the overview of all three packages copies some steps
ONLINE = ("Step 14", "Step 15")  # the steps that call a real model
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
        text = re.sub(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", "<TIME>", text)  # MLflow's log lines
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
            fence, block = line.rstrip(), []
        elif fence is not None and line.rstrip() == "```":
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


@pytest.mark.parametrize("page_file", [FEATURES, OVERVIEW], ids=lambda p: p.name)
def test_the_pages_that_copy_the_tour_show_output_of_the_script(run, page_file):
    # A plain fenced block on these pages is the tour's real output; a block
    # that is not, such as a picture, has a language, as ```text.
    done, tour_dir, port = run
    ran = _Normal(str(tour_dir), port, numbered=False)(done.stdout)
    shown = _Normal(PAGE_DIR, numbered=False)
    page = page_file.read_text()
    blocks = _blocks(page)
    assert blocks and all(b.strip() for b in blocks)  # an empty block would match anything
    for block in blocks:
        assert shown(block) in ran, block[:200]


def _heading_ids(page: str) -> set[str]:
    """The IDs of a page's headings, from smalldocs' own parser, as the site makes them."""
    from smalldocs.markdown import render, split_front_matter

    meta, body = split_front_matter(page)
    env = {"title": meta.get("title")}
    render(body, env)
    return {h["id"] for h in env.get("headings", [])}


def test_the_links_into_the_tour_land_on_a_heading():
    # smalldocs check does not follow a link's #part into another page.
    ids = _heading_ids(PAGE.read_text())
    linked = {p.name: set(re.findall(r"provrun-tour(?:\.md|\.html|/)?#([^)\s\"'>]+)", p.read_text()))
              for p in sorted((ROOT / "docs").rglob("*.md")) if "_site" not in p.parts}
    assert linked[OVERVIEW.name]  # the overview links into the tour
    assert {n: a - ids for n, a in linked.items() if a - ids} == {}


def test_heading_ids_follow_the_site():
    page = (
        "## Step 14: Call it (needs ANTHROPIC_API_KEY)\n## Notes\n## Notes\n## Plain {#custom}\n"
        "## Has [a link](x.html) and _emph_\nSetext One\n----------\n"
        "````\n```python\n# not a heading\n```\n````\n"
    )
    assert _heading_ids(page) == {
        "step-14-call-it-needs-anthropic_api_key", "notes", "notes-1", "custom",
        "has-a-link-and-emph", "setext-one",
    }


def test_the_page_shows_the_online_steps_of_the_script():
    # Each online step on the page has the script's title and the script's commands, in order.
    body = SCRIPT.read_text().split('if [ "${1:-}" = "--online" ]; then', 1)[1]
    steps, title = {}, None
    for line in body.splitlines():
        if m := re.match(r'\s*step "(.*)"$', line):
            title = m.group(1)
            steps[title] = []
        elif "show " in line and title is not None:
            steps[title].append(line.split("show ", 1)[1][1:-1])
    page = {t: [line[2:] for line in b.splitlines() if line.startswith("$ ")]
            for t, b in _page_steps(PAGE.read_text()).items() if t.startswith(ONLINE)}
    assert list(steps) and all(t.startswith(ONLINE) for t in steps)
    assert page == steps


def test_the_streamed_script_records_each_request_and_keeps_to_a_new_folder(tmp_path):
    # PydanticAI's test model streams, and needs no key.
    env = {**os.environ, "PROVRUN_TOUR_MODEL": "test"}
    env.pop("ANTHROPIC_API_KEY", None)
    go = ["uv", "run", "python", str(TOURS / "streamed.py"), str(tmp_path / "s")]
    done = subprocess.run(go, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    events = [line.split()[0] for line in done.stdout.splitlines() if line.startswith(("model_called", "tool_called"))]
    assert events == ["model_called", "tool_called", "model_called"]
    assert all(" streamed " in line for line in done.stdout.splitlines() if line.startswith("model_called"))
    again = subprocess.run(go, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert again.returncode != 0 and "is not empty" in again.stderr


ROUND_TRIP = (
    'env | sort > before; set | grep "^PROVRUN_RUNS=" > before_local; '
    'source tours/provrun/start.sh > /dev/null; '
    'servers="$(pgrep -P "$(cat "$TOUR_DIR/moto.pid")") $(pgrep -P "$(cat "$TOUR_DIR/collector.pid")")"; '
    'source tours/provrun/stop.sh; '
    'env | sort > after; set | grep "^PROVRUN_RUNS=" > after_local; '
    'diff before after | grep -v "^[<>] _=" | grep "^[<>]"; diff before_local after_local; '
    'printenv PROVRUN_RUNS && echo "PROVRUN_RUNS was exported"; '
    'for p in $servers; do kill -0 $p 2> /dev/null && echo "a server still runs"; done; rm before after before_local after_local; '
    'for f in _tour_start _tour_stop; do typeset -f $f > /dev/null 2>&1 && echo "$f is still defined"; done; true'
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


def test_a_failed_start_cleans_up_under_set_e(tmp_path):
    tour_dir = tmp_path / "t"
    # run.sh sources start.sh with set -e, which must not exit before the clean-up.
    done = subprocess.run(["bash", "-c", 'set -e; source tours/provrun/start.sh || echo "rc=$?"; '
                           'typeset -f _tour_start _tour_stop || echo "no functions"'],
                          cwd=ROOT, env=_env(tour_dir, TOUR_OTLP_PORT="abc"), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["rc=1", "no", "functions"]
    assert not tour_dir.exists()


def test_a_start_that_fails_halfway_cleans_up_under_set_e(tmp_path):
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)  # the tour's folder cannot be made, after the checks
    try:
        done = subprocess.run(["bash", "-c", 'set -euo pipefail; PROVRUN_RUNS=orig; '
                               'source tours/provrun/start.sh || echo "rc=$?"; '
                               'echo "runs=$PROVRUN_RUNS saved=${_PROVRUN_TOUR_SAVED:-}"'],
                              cwd=ROOT, env=_env(parent / "t"), capture_output=True, text=True, timeout=60)
    finally:
        parent.chmod(0o700)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["rc=1", "runs=orig", "saved="]


def test_start_run_as_a_script_gives_its_status_quietly(tmp_path):
    done = subprocess.run(["bash", "tours/provrun/start.sh"], cwd=ROOT,
                          env=_env(tmp_path / "t", TOUR_OTLP_PORT="abc"), capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 1
    assert "return" not in done.stderr


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


def test_online_without_a_key_stops_before_the_tour_starts(tmp_path):
    done = subprocess.run(["bash", str(SCRIPT), "--online"], cwd=ROOT, env=_env(tmp_path / "t"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 1
    assert "needs ANTHROPIC_API_KEY" in done.stderr
    assert not (tmp_path / "t").exists()
