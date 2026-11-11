"""The tutorials: each script runs, and each page shows its output and its files.

Each script runs in a scratch folder, and deletes it when it ends. The
scripts of provcat and of all three start their own moto server on a free
port. A step that calls a real model is left out, as in the tours. Each script
runs twice, so that a hash that is the same in both runs must be the same on
the page too.
"""
from __future__ import annotations

import http.server
import re
import shutil
import socketserver
import subprocess
import threading

import pytest

from .test_tour import ROOT, _blocks, _env, _Normal, _page_steps, _script_steps

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("curl") is None,
                                reason="the tutorials need bash and curl")

# Each tutorial: its page, its folder of files, the folder that its page shows,
# and the steps that call a real model.
TUTORIALS = {
    "tutorial": (ROOT / "docs" / "tutorial.md", ROOT / "tours" / "tutorial", "/tmp/mahacata-tutorial", ("Step 10",)),
    "provcat": (ROOT / "docs" / "provcat-tutorial.md", ROOT / "tours" / "provcat-tutorial", "/tmp/provcat-tutorial", ()),
    "provrun": (ROOT / "docs" / "provrun-tutorial.md", ROOT / "tours" / "provrun-tutorial", "/tmp/provrun-tutorial",
                ("Step 10",)),
    "provevi": (ROOT / "docs" / "provevi-tutorial.md", ROOT / "tours" / "provevi-tutorial", "/tmp/provevi-tutorial", ()),
}


@pytest.fixture(scope="module", params=list(TUTORIALS))
def ran(request, tmp_path_factory):
    page, folder, page_dir, online = TUTORIALS[request.param]
    runs = []
    for _ in range(2):
        tour_dir = tmp_path_factory.mktemp(request.param) / "scratch"
        # A reader's shell can hold any of these; the script must clear them.
        env = _env(tour_dir, **INHERITED)
        runs.append((subprocess.run(["bash", str(folder / "run.sh")], cwd=ROOT, env=env, capture_output=True,
                                    text=True, timeout=600), tour_dir))
    (done, tour_dir), (again, _) = runs
    return done, tour_dir, page, folder, page_dir, online, again


# Variables that a reader may have set, each of which would change the output.
INHERITED = {"PROVCAT_CONFIG": "/nonexistent/provcat.yaml", "PROVRUN_RUNS": "/nonexistent/runs",
             "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9", "LITELLM_LOG": "DEBUG",
             "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": "example.com", "PGHOST": "nowhere"}
HEX = re.compile(r"(?<![0-9a-f-])[0-9a-f]{12,64}(?![0-9a-f-])")  # not a part of a UUID


def test_the_script_runs_and_cleans_up(ran):
    done, tour_dir, *_ = ran
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert not tour_dir.exists()


def test_the_page_shows_the_output_of_the_script(ran):
    done, tour_dir, page, _, page_dir, online, _ = ran
    script = _script_steps(done.stdout)
    shown = {t: b for t, b in _page_steps(page.read_text()).items() if not t.startswith(online)}
    assert list(shown) == list(script)
    shown_normal, ran_normal = _Normal(page_dir), _Normal(str(tour_dir))
    for title in script:  # in order, so that each value keeps its number across the steps
        assert shown_normal(shown[title]) == ran_normal(script[title]), title


def test_a_hash_that_is_the_same_in_each_run_is_the_same_on_the_page(ran):
    done, _, page, *_, again = ran
    assert again.returncode == 0, again.stdout[-3000:] + again.stderr[-3000:]
    first, second = _script_steps(done.stdout), _script_steps(again.stdout)
    shown = _page_steps(page.read_text())
    for title in first:
        fixed = set(HEX.findall(first[title])) & set(HEX.findall(second[title]))
        assert fixed <= set(HEX.findall(shown[title])), (title, fixed - set(HEX.findall(shown[title])))


def test_the_page_shows_the_files_that_the_script_copies(ran):
    _, _, page, folder, *_ = ran
    text = page.read_text()
    # A file goes in by cp, or by a sed that puts the reader's folder in it.
    files = re.findall(rf"(?:cp|sed \"[^\"]*\") tours/{folder.name}/([a-z.-]+\.yaml) ", text)
    assert files
    yaml_blocks = re.findall(r"^```yaml\n(.*?)^```$", text, flags=re.M | re.S)
    for name in files:
        assert (folder / name).read_text() in yaml_blocks, name


def test_the_online_steps_are_the_last_steps_of_the_script(ran):
    _, _, page, folder, _, online, _ = ran
    titles = re.findall(r'step "(Step \d+:[^"]*)"', (folder / "run.sh").read_text())
    page_titles = re.findall(r"^## (Step \d+:.*)$", page.read_text(), flags=re.M)
    assert page_titles == titles
    assert all(t.startswith(online) for t in titles[len(titles) - len(online):])


def _sections(text: str) -> dict[str, str]:
    """Each section of a page by its heading, with its body."""
    return dict(re.findall(r"^## ([^\n]*)\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S))


@pytest.mark.parametrize("name", list(TUTORIALS))
def test_each_file_that_the_page_shows_is_in_the_step_that_copies_it(name):
    page, folder, *_ = TUTORIALS[name]
    for title, body in _sections(page.read_text()).items():
        copied = re.findall(r"(?:cp|sed \"[^\"]*\") tours/[a-z-]+/([a-z.-]+\.yaml) ", body)
        assert all(f"tours/{folder.name}/" in c for c in re.findall(r"(?:cp|sed \"[^\"]*\") tours/[a-z-]+/", body)), title
        shown = re.findall(r"^```yaml\n(.*?)^```$", body, flags=re.M | re.S)
        assert sorted(shown) == sorted((folder / f).read_text() for f in copied), title


@pytest.mark.parametrize("name", list(TUTORIALS))
def test_the_python_that_the_page_shows_is_in_the_tutorials_files(name):
    page, folder, *_ = TUTORIALS[name]
    sources = [p.read_text() for p in folder.glob("*.py")]
    for block in re.findall(r"^```python\n(.*?)^```$", page.read_text(), flags=re.M | re.S):
        assert any(block in s for s in sources), block


@pytest.mark.parametrize("name", list(TUTORIALS))
def test_the_page_clears_the_variables_that_the_script_clears(name):
    page, folder, *_ = TUTORIALS[name]
    script = (folder / "run.sh").read_text()
    clears = re.compile(r"^ *(unset [A-Z_ ]+|for v in .*; do unset \$v; done)$", flags=re.M)
    setup = _sections(page.read_text())["Before you start"]
    assert clears.findall(setup) == clears.findall(script) and len(clears.findall(script)) == 2
    assert "proxy" in setup  # the reader learns that the line removes the shell's proxy


@pytest.mark.parametrize("name", list(TUTORIALS))
def test_the_page_uses_the_folder_and_port_that_the_script_uses(name):
    page, folder, page_dir, _ = TUTORIALS[name]
    script = (folder / "run.sh").read_text()
    setup = _sections(page.read_text())["Before you start"]
    assert re.search(rf"^   export TOUR_DIR={re.escape(page_dir)}\b", setup, flags=re.M)
    assert f'TOUR_DIR="${{TOUR_DIR:-{page_dir}}}"' in script
    ports = re.findall(r"TOUR_S3_PORT=(\d+)|moto_server -p (\d+)", page.read_text())
    if "moto_server" in script:
        [default] = re.findall(r'TOUR_S3_PORT="\$\{TOUR_S3_PORT:-(\d+)\}"', script)
        assert len(ports) == 2 and {p for pair in ports for p in pair if p} == {default}
    else:
        assert not ports


class _Closer(socketserver.BaseRequestHandler):
    """A server that does not speak HTTP: it closes each connection."""

    def handle(self):
        pass


@pytest.mark.parametrize("handler", [http.server.SimpleHTTPRequestHandler, _Closer], ids=["http", "not http"])
@pytest.mark.parametrize("name", [n for n in TUTORIALS if "moto_server" in (TUTORIALS[n][1] / "run.sh").read_text()])
def test_the_script_refuses_a_port_that_is_in_use(name, handler, tmp_path):
    _, folder, *_ = TUTORIALS[name]
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = _env(tmp_path / "scratch", TOUR_S3_PORT=str(server.server_address[1]))
        done = subprocess.run(["bash", str(folder / "run.sh")], cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=60)
    finally:
        server.shutdown()
        server.server_close()
    assert done.returncode == 1 and "is in use" in done.stderr
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize("name", [n for n, t in TUTORIALS.items() if t[3]])
def test_the_online_commands_on_the_page_are_the_scripts(name):
    page, folder, _, online = TUTORIALS[name]
    script = (folder / "run.sh").read_text()
    for title, body in _sections(page.read_text()).items():
        if title.startswith(online):
            part = script[script.index(f'step "{title}"'):]
            part = part.split('\n  step "', 1)[0]
            commands = [line[2:] for line in "\n".join(_blocks(body)).splitlines() if line.startswith("$ ")]
            assert commands and commands == re.findall(r"^  show '(.*)'$", part, flags=re.M), title
