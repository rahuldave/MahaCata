"""scripts/lakefs_local.py, run against fake docker and lakefs programs."""
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "lakefs_local.py"
SECRET = "SECRETPW"


def load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("lakefs_local", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def conn(tmp_path, url):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'database:\n  postgres:\n    connection_string: "{url}"\n')
    return load().connection_string(cfg, 55440)


@pytest.mark.parametrize("url, want", [
    ("postgresql://postgres:pw@some-postgres.orb.local:5432/", "postgresql://postgres:pw@127.0.0.1:55440/"),
    ("postgres://u:p%40ss%3Aw@db/x?sslmode=disable", "postgres://u:p%40ss%3Aw@127.0.0.1:55440/x?sslmode=disable"),
    ("postgresql://u:pw@[::1]:5432/db", "postgresql://u:pw@127.0.0.1:55440/db"),
    ("postgresql://u@db/db", "postgresql://u@127.0.0.1:55440/db"),
])
def test_connection_string_keeps_all_but_host_and_port(tmp_path, url, want):
    assert conn(tmp_path, url) == want


@pytest.mark.parametrize("url", [
    f"host=localhost port=5432 user=postgres password={SECRET} dbname=postgres",  # key/value form
    f"postgresql://postgres:pa?{SECRET}@db:5432/db",  # an unencoded "?" in the password
    f"postgresql://u:{SECRET}@h1:5432,h2:5433/db",  # more than one host
])
def test_connection_strings_it_cannot_rewrite_are_refused_without_the_password(tmp_path, capsys, url):
    # Review of the script, finding 10.
    with pytest.raises(SystemExit) as e:
        conn(tmp_path, url)
    assert SECRET not in str(e.value)


def test_a_yaml_error_does_not_show_the_password(tmp_path):
    # Finding 3: a YAML error quotes the file, so it is not shown.
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'database:\n  postgres:\n    connection_string: "postgresql://postgres:{SECRET}@x\n  a: [\n')
    with pytest.raises(SystemExit) as e:
        load().connection_string(cfg, 55440)
    assert "not valid YAML" in str(e.value) and SECRET not in str(e.value)


@pytest.fixture
def world(tmp_path):
    """A lakeFS folder with a fake lakefs, a fake docker on the PATH, and a
    port that answers like Postgres."""
    home = tmp_path / "lakefs"
    home.mkdir()
    (home / "config.yaml").write_text(
        f'database:\n  postgres:\n    connection_string: "postgresql://postgres:{SECRET}@pg:5432/"\n')
    log = tmp_path / "log"
    lakefs = home / "lakefs"
    lakefs.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, signal, sys, time
        log = open({str(log)!r}, "a")
        log.write("lakefs " + os.environ["LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING"].split("@")[1] + "\\n")
        log.flush()
        def stop(signum, frame):
            log.write("lakefs stopped gracefully\\n"); log.close(); sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        code = int(os.environ.get("FAKE_LAKEFS_EXIT", "-1"))
        if code >= 0:
            sys.exit(code)
        time.sleep(60)
        """))
    lakefs.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        args = sys.argv[1:]
        open({str(log)!r}, "a").write("docker " + " ".join(args) + "\\n")
        if args[0] == "inspect":
            if os.environ.get("FAKE_NO_CONTAINER"):
                print("Error: No such object: lakefs-postgres", file=sys.stderr); sys.exit(1)
            print(os.environ.get("FAKE_RUNNING", "false"))
        if args[0] == "exec" and os.environ.get("FAKE_PG_DOWN"):
            sys.exit(1)
        """))
    docker.chmod(0o755)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield home, port, env, log
    server.close()


def start(world, **extra):
    home, port, env, log = world
    env = dict(env, **extra)
    return subprocess.Popen([sys.executable, str(SCRIPT), "--lakefs-dir", str(home), "--port", str(port),
                             "--wait", "2"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for_line(log, text, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if log.exists() and text in log.read_text():
            return
        time.sleep(0.1)
    raise AssertionError(f"{text!r} not in {log.read_text() if log.exists() else ''}")


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_a_signal_stops_lakefs_gently_and_then_the_container(world, signum):
    # Findings 4 and 5: kill, a closed terminal or Ctrl+C all clean up.
    proc = start(world)
    log = world[3]
    wait_for_line(log, "lakefs 127.0.0.1:")
    proc.send_signal(signum)
    out, err = proc.communicate(timeout=20)
    text = log.read_text()
    assert "lakefs stopped gracefully" in text
    assert "docker start lakefs-postgres" in text and "docker stop lakefs-postgres" in text
    assert proc.returncode == 0
    assert SECRET not in out + err + text


def test_a_running_container_is_left_running(world):
    # Finding 7: it stops only a container that it started.
    proc = start(world, FAKE_RUNNING="true", FAKE_LAKEFS_EXIT="0")
    proc.communicate(timeout=20)
    text = world[3].read_text()
    assert "docker start" not in text and "docker stop" not in text


def test_the_exit_code_of_lakefs_is_kept(world):
    # Finding 9.
    proc = start(world, FAKE_LAKEFS_EXIT="3")
    proc.communicate(timeout=20)
    assert proc.returncode == 3
    assert "docker stop lakefs-postgres" in world[3].read_text()


def test_postgres_that_never_answers_stops_before_lakefs(world):
    # Finding 6: no "ready" message, no lakeFS, and the container is stopped.
    proc = start(world, FAKE_PG_DOWN="1")
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 1 and "did not answer" in err and "ready" not in out
    text = world[3].read_text()
    assert "lakefs " not in text and "docker stop lakefs-postgres" in text


def test_a_missing_container_shows_dockers_message(world):
    # Finding 8.
    proc = start(world, FAKE_NO_CONTAINER="1")
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 1 and "No such object: lakefs-postgres" in err
