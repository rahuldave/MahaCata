"""Run a local lakeFS with its Postgres container, for the provcat system tests.

    uv run python scripts/lakefs_local.py

It starts the Postgres container, waits until Postgres takes connections, and
then runs lakeFS in the foreground. Ctrl+C, kill or closing the terminal asks
lakeFS to stop, waits for it, and then stops the container, but only if this
script started it. The exit code is the exit code of lakeFS.

lakeFS reads its own config.yaml. This script takes the Postgres connection
string from that file and changes only its host and port, to the port that the
container publishes on 127.0.0.1. It passes the result in the environment
variable LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING, so config.yaml is never
changed. No message of this script shows the connection string or a line of
config.yaml.

The folder given by --lakefs-dir holds config.yaml, and lakeFS runs in it.
--lakefs names the lakefs program: a path, or a bare name to look up on the
PATH. The defaults fit this machine: lakeFS 1.86.0
built from source in ~/Projects/lakeFS, its config in ~/Catalog/lakefs, and
the container lakefs-postgres published on 127.0.0.1:55440.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

GRACE = 30  # seconds lakeFS gets to stop before it is killed


def connection_string(config: Path, port: int) -> str:
    """The connection string of config.yaml with its host and port replaced."""
    try:
        cfg = yaml.safe_load(config.read_text())
    except OSError as e:
        sys.exit(f"cannot read {config}: {e.strerror}")
    except yaml.YAMLError:
        # The YAML error quotes lines of the file, which can hold the password.
        sys.exit(f"{config} is not valid YAML; fix it and run again")
    try:
        url = cfg["database"]["postgres"]["connection_string"]
    except (KeyError, TypeError):
        sys.exit(f"{config} has no database.postgres.connection_string")
    if not isinstance(url, str):
        sys.exit(f"database.postgres.connection_string in {config} is not a string")
    try:
        parts = urlsplit(url)
        user, password = parts.username, parts.password
        parts.port  # raises ValueError when the port is not a number
    except ValueError:
        sys.exit(f"database.postgres.connection_string in {config} is not a valid URL")
    if url.count("@") != parts.netloc.count("@") or parts.fragment:
        # An "@" outside the host part means a "/", "?" or "#" in the
        # password that is not percent-encoded.
        sys.exit(f"cannot rewrite the connection string in {config} safely; percent-encode its password")
    if parts.scheme not in ("postgres", "postgresql") or "," in parts.netloc:
        sys.exit(f"database.postgres.connection_string in {config} must be one postgres:// URL with one host")
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + f"127.0.0.1:{port}"
    new = urlunsplit(parts._replace(netloc=netloc))
    check = urlsplit(new)
    if (check.username, check.password, check.hostname, check.port) != (user, password, "127.0.0.1", port):
        # For example a "/", "?" or "#" in the password that is not percent-encoded.
        sys.exit(f"cannot rewrite the connection string in {config} safely; percent-encode its password")
    return new


def find_program(name: str) -> Path:
    """The lakefs program as an absolute path. A bare name is looked up on the
    PATH; any other path is taken from the current folder, not the folder
    lakeFS runs in."""
    if os.sep not in name and not name.startswith("~"):
        found = shutil.which(name)
        if not found:
            sys.exit(f"no lakefs program named {name} on the PATH")
        return Path(found).resolve()
    program = Path(name).expanduser().resolve()
    if not program.is_file():
        sys.exit(f"no lakefs program at {program}")
    if not os.access(program, os.X_OK):
        sys.exit(f"{program} is not executable")
    return program


def docker(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("docker is not installed or not on the PATH")


def wait_for(port: int, container: str, seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                if docker("exec", container, "pg_isready", "-q").returncode == 0:
                    return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def run_lakefs(program: Path, home: Path, env: dict) -> int:
    """Run lakeFS until it exits. A signal asks it to stop, and a second
    request or the grace time ends it."""
    proc = subprocess.Popen([str(program), "--config", str(home / "config.yaml"), "run"],
                            cwd=home, env=env)
    asked = []

    def stop(signum, frame):
        if proc.poll() is None:
            asked.append(time.monotonic())
            proc.send_signal(signal.SIGKILL if len(asked) > 2 else signal.SIGTERM)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    while proc.poll() is None:
        if asked and time.monotonic() - asked[0] > GRACE:
            proc.kill()
        time.sleep(0.2)
    return 0 if asked and proc.returncode in (0, -signal.SIGTERM, -signal.SIGINT) else proc.returncode


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lakefs", default="~/Projects/lakeFS/lakefs", help="the lakefs program, or its name on the PATH")
    ap.add_argument("--lakefs-dir", default="~/Catalog/lakefs", help="the folder with config.yaml")
    ap.add_argument("--container", default="lakefs-postgres", help="the Postgres container, which must exist")
    ap.add_argument("--port", type=int, default=55440, help="the port the container publishes on 127.0.0.1")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for Postgres")
    args = ap.parse_args()
    program, home = find_program(args.lakefs), Path(args.lakefs_dir).expanduser().resolve()
    env = dict(os.environ, LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING=connection_string(home / "config.yaml",
                                                                                         args.port))
    state = docker("inspect", "-f", "{{.State.Running}}", args.container)
    if state.returncode:
        sys.exit(f"docker: {state.stderr.strip() or 'cannot inspect ' + args.container}")
    started = state.stdout.strip() != "true"
    if started:
        out = docker("start", args.container)
        if out.returncode:
            sys.exit(f"docker: {out.stderr.strip()}")
    code = 1
    try:
        if not wait_for(args.port, args.container, args.wait):
            print(f"Postgres in {args.container} did not answer on 127.0.0.1:{args.port}", file=sys.stderr)
        else:
            print(f"Postgres is ready in {args.container}; starting lakeFS on http://localhost:8000", flush=True)
            code = run_lakefs(program, home, env)
    finally:
        if started:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(signum, signal.SIG_IGN)  # let the container stop
            docker("stop", args.container)
            print(f"stopped {args.container}")
    sys.exit(code)


if __name__ == "__main__":
    main()
