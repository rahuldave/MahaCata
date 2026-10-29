"""Run the reference access control for lakeFS: one lakeFS for many orgs.

    uv run python scripts/lakefs_acl_local.py

It runs two programs from the patched lakeFS 1.86.0 source build, the branch
provcat of github.com/rahuldave/lakeFS: the access-list server, which keeps
users, groups and policies, and a lakeFS that asks it who may do what. Each org gets a group, a user and a policy. The policy allows
everything on the repositories whose names start with the org and a "-", and
nothing else. So acme's keys work on acme-data and are refused on globex-data,
and acme lists only its own repositories.

lakeFS runs with auth.ui_config.rbac set to external. In this mode lakeFS
makes no users itself, so the first run makes the admin in the access-list
server. After that, the admin manages groups, policies and users through the
lakeFS API, with the admin keys. The access-list server takes only calls that
carry the token in <dir>/api-token, which lakeFS sends, and it listens on
127.0.0.1 only.

The first run makes the folder given by --dir and writes into it:

- acl.yaml and lakefs.yaml, each with a new random secret key;
- api-token, the random token between lakeFS and the access-list server;
- the databases of both programs, the lakeFS data and its commit cache, under
  that folder;
- credentials, an AWS credentials file with the profile lakefs for the admin
  and lakefs-<org> for each org.

Later runs reuse the folder, and add any org that is new. No message shows a
key or a line of a file with a secret.

Use it with provcat:

    export AWS_SHARED_CREDENTIALS_FILE=<dir>/credentials
    export LAKEFS_S3_ENDPOINT=http://127.0.0.1:8002

If the admin locks itself out through lakeFS, run with --repair-admin. With
the setup running, it repairs it in place and exits; otherwise it starts the
setup and repairs it first.

Ctrl+C, kill or closing the terminal stops lakeFS and then the access-list
server. The exit code is 0 after a requested stop, and 1 when a program fails.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GRACE = 30  # seconds each program gets to stop before it is killed
ORG = re.compile(r"[a-z0-9]{1,20}")  # the org rule of the example configuration


def program(path: str) -> Path:
    found = Path(path).expanduser().resolve()
    if not found.is_file():
        sys.exit(f"no program at {found}; build lakeFS 1.86.0 from source, as docs/setup.md shows")
    if not os.access(found, os.X_OK):
        sys.exit(f"{found} is not executable")
    return found


def write_secret_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def write_configs(home: Path, port: int, acl_port: int) -> None:
    """The two configuration files, made once. An existing file is kept."""
    if not (home / "acl.yaml").exists():
        write_secret_file(home / "acl.yaml", f"""\
listen_address: 127.0.0.1:{acl_port}
database:
  type: local
  local:
    path: {json.dumps(str(home / "acl-db"))}
encrypt:
  secret_key: {secrets.token_hex(32)}
""")
    if not (home / "lakefs.yaml").exists():
        write_secret_file(home / "lakefs.yaml", f"""\
listen_address: 127.0.0.1:{port}
database:
  type: local
  local:
    path: {json.dumps(str(home / "lakefs-db"))}
blockstore:
  type: local
  local:
    path: {json.dumps(str(home / "data"))}
auth:
  encrypt:
    secret_key: {secrets.token_hex(32)}
  api:
    endpoint: http://127.0.0.1:{acl_port}/api/v1
  ui_config:
    rbac: external
stats:
  enabled: false
""")


def api_token(home: Path) -> str:
    """The token between lakeFS and the access-list server, made once."""
    path = home / "api-token"
    if not path.exists():
        write_secret_file(path, secrets.token_hex(32))
    if path.stat().st_mode & 0o077:
        sys.exit(f"{path} can be read by other users; run chmod 600 on it, or delete it and run again")
    token = path.read_text().strip()
    if len(token) < 32:
        sys.exit(f"{path} holds a token shorter than 32 characters; delete it, and run again")
    return token


def check_ports(home: Path, port: int, acl_port: int) -> None:
    """The configs keep the ports of the first run; a different port now
    would start a server where the script does not look."""
    want = {"acl.yaml": [f"listen_address: 127.0.0.1:{acl_port}"],
            "lakefs.yaml": [f"listen_address: 127.0.0.1:{port}",
                            f"endpoint: http://127.0.0.1:{acl_port}/api/v1"]}
    for name, lines in want.items():
        text = (home / name).read_text().splitlines()
        for line in lines:
            if not any(have.strip() == line for have in text):
                sys.exit(f"{home / name} was made with other ports; run with the ports of the first run, "
                         f"or edit its listen_address and endpoint lines")


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Api:
    def __init__(self, port: int, keys: tuple[str, str] | None = None, token: str | None = None,
                 name: str = "lakeFS"):
        self.base = f"http://127.0.0.1:{port}/api/v1"
        self.keys = keys
        self.token = token
        self.name = name

    def __call__(self, method: str, path: str, body=None):
        """(status, reply). A reply that is not JSON comes back as text."""
        headers = {"Content-Type": "application/json"}
        if self.keys:
            headers["Authorization"] = "Basic " + base64.b64encode(":".join(self.keys).encode()).decode()
        elif self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, text = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read()
        try:
            return status, json.loads(text) if text else {}
        except ValueError:
            return status, text.decode(errors="replace")[:300]

    def must(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        """The reply. A 404 or 409 that ok allows comes back as None or
        "exists", and a 201 with an empty reply as "made"."""
        status, reply = self(method, path, body)
        if status not in ok:
            message = reply.get("message") if isinstance(reply, dict) else reply
            raise SystemExit(f"{self.name} {method} {path} answered {status}: {message}")
        if status == 404:
            return None
        if status == 409:
            return "exists"
        if status == 201 and not reply:
            return "made"
        return reply


def wait_for(url: str, proc: subprocess.Popen, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


CACHE_SECONDS = 25  # lakeFS caches a user's policies for 20 seconds, plus up to 3 of jitter
ADMINS_POLICY = "ACL(_-_)Admins"
ADMINS_STATEMENT = {"effect": "allow", "resource": "*",
                    "action": ["fs:*", "auth:*", "ci:*", "retention:*", "branches:*", "pr:*"]}


def repair_admin(home: Path, acl_port: int, token: str) -> list[str]:
    """Put the admin back, through the access-list server and its token.

    The admin can lock itself out through lakeFS: leave Admins, delete it,
    join a group with a deny policy, attach a deny policy to itself or to
    Admins, delete its keys, or delete its user. So this does the same steps
    every time, whatever happened, and ends with new admin keys. It returns
    what it changed."""
    acl = Api(acl_port, token=token, name="the access-list server")
    quoted = urllib.parse.quote(ADMINS_POLICY, safe="")
    done = []
    if acl.must("GET", "/auth/groups/Admins", ok=(200, 404)) is None:
        acl.must("POST", "/auth/groups", {"id": "Admins"})
        done.append("made the Admins group")
    # Written every time, so it holds exactly the Admin statements again. The
    # server takes no other statements for this policy.
    admins_policy = {"name": ADMINS_POLICY, "statement": [ADMINS_STATEMENT], "acl": "Admin"}
    if acl.must("GET", f"/auth/policies/{quoted}", ok=(200, 404)) is None:
        acl.must("POST", "/auth/policies", admins_policy)
        done.append(f"made the {ADMINS_POLICY} policy")
    else:
        acl.must("PUT", f"/auth/policies/{quoted}", admins_policy)
    if acl.must("PUT", f"/auth/groups/Admins/policies/{quoted}", ok=(201, 409)) == "made":
        done.append(f"attached {ADMINS_POLICY} to Admins")
    for p in acl.must("GET", "/auth/groups/Admins/policies?amount=1000").get("results") or []:
        if p["name"] != ADMINS_POLICY:
            acl.must("DELETE", f"/auth/groups/Admins/policies/{urllib.parse.quote(p['name'], safe='')}")
            done.append(f"detached {p['name']} from Admins")
    if acl.must("GET", "/auth/users/admin", ok=(200, 404)) is None:
        acl.must("POST", "/auth/users", {"username": "admin", "source": "internal"})
        done.append("made the admin user")
    if acl.must("PUT", "/auth/groups/Admins/members/admin", ok=(201, 409)) == "made":
        done.append("put the admin back in Admins")
    for g in acl.must("GET", "/auth/users/admin/groups?amount=1000").get("results") or []:
        if g["name"] != "Admins":
            acl.must("DELETE", f"/auth/groups/{urllib.parse.quote(g['name'], safe='')}/members/admin")
            done.append(f"took the admin out of {g['name']}")
    for p in acl.must("GET", "/auth/users/admin/policies?amount=1000").get("results") or []:
        acl.must("DELETE", f"/auth/users/admin/policies/{urllib.parse.quote(p['name'], safe='')}")
        done.append(f"detached {p['name']} from the admin")
    # New keys every time: the saved ones may be deleted, or may belong to
    # an admin user that was deleted and made again.
    delete_keys(acl, "admin")
    key = acl.must("POST", "/auth/users/admin/credentials")
    if not key.get("access_key_id") or not key.get("secret_access_key"):
        sys.exit("the access-list server made admin keys that are empty")
    creds = configparser.ConfigParser()
    creds.read(home / "credentials")
    creds["lakefs"] = {"aws_access_key_id": key["access_key_id"], "aws_secret_access_key": key["secret_access_key"]}
    write_credentials(home / "credentials", creds)
    done.append("made new admin keys")
    return done


def write_credentials(path: Path, creds: configparser.ConfigParser) -> None:
    """A new file, renamed over the old one, so a crash never leaves a file
    with some keys lost."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        creds.write(f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def delete_keys(api: Api, user: str) -> None:
    """Delete every key of a user, before new keys replace them."""
    listed = api.must("GET", f"/auth/users/{user}/credentials")
    for cred in listed.get("results") or []:
        api.must("DELETE", f"/auth/users/{user}/credentials/{cred['access_key_id']}", ok=(204, 404))


def policy(org: str) -> dict:
    """Everything on the org's repositories, and on storage named for the org."""
    return {"id": f"{org}-repos", "statement": [
        {"effect": "allow", "action": ["fs:*"], "resource": f"arn:lakefs:fs:::repository/{org}-*"},
        {"effect": "allow", "action": ["fs:AttachStorageNamespace"],
         "resource": f"arn:lakefs:fs:::namespace/local://{org}-*"},
    ]}


def set_up(home: Path, port: int, acl_port: int, orgs: list[str], token: str) -> list[str]:
    """Make the admin on the first run, and each org that is new. Returns the
    orgs it added."""
    creds_path = home / "credentials"
    creds = configparser.ConfigParser()
    creds.read(creds_path)

    def save():
        write_credentials(creds_path, creds)

    # With external access control, lakeFS leaves users to the access-list
    # server: its setup call makes no user, and returns empty keys. So the
    # admin is made in the access-list server, in its Admins group.
    Api(port).must("POST", "/setup_lakefs", {"username": "admin"}, ok=(200, 409))
    if "lakefs" not in creds:
        acl = Api(acl_port, token=token)
        # An earlier run may have made the admin and stopped before it saved
        # the keys; then the admin gets new keys.
        if acl("GET", "/auth/users/admin")[0] == 404:
            acl.must("POST", "/auth/users", {"username": "admin", "source": "internal"})
        else:
            delete_keys(acl, "admin")  # the lost keys must stop working
        acl.must("PUT", "/auth/groups/Admins/members/admin", ok=(201, 409))
        key = acl.must("POST", "/auth/users/admin/credentials")
        if not key.get("access_key_id") or not key.get("secret_access_key"):
            sys.exit("the access-list server made admin keys that are empty")
        creds["lakefs"] = {"aws_access_key_id": key["access_key_id"],
                           "aws_secret_access_key": key["secret_access_key"]}
        save()
    admin = Api(port, (creds["lakefs"]["aws_access_key_id"], creds["lakefs"]["aws_secret_access_key"]))
    added = []
    for org in orgs:
        # The policy is written on every run, so a change to it reaches an
        # existing setup.
        if admin("GET", f"/auth/policies/{org}-repos")[0] == 404:
            admin.must("POST", "/auth/policies", policy(org))
        else:
            admin.must("PUT", f"/auth/policies/{org}-repos", policy(org))
        status, _ = admin("GET", f"/auth/groups/{org}")
        profile = f"lakefs-{org}"
        if status == 200 and profile in creds:
            keys = (creds[profile]["aws_access_key_id"], creds[profile]["aws_secret_access_key"])
            if Api(port, keys)("GET", "/user")[0] == 200:
                continue  # its saved keys still work
        if status == 404:
            admin.must("POST", "/auth/groups", {"id": org})
        admin.must("PUT", f"/auth/groups/{org}/policies/{org}-repos", ok=(201, 409))
        user = f"{org}-user"
        if admin("GET", f"/auth/users/{user}")[0] == 404:
            admin.must("POST", "/auth/users", {"id": user})
        admin.must("PUT", f"/auth/groups/{org}/members/{user}", ok=(201, 409))
        delete_keys(admin, user)  # keys that were lost or no longer work
        key = admin.must("POST", f"/auth/users/{user}/credentials")
        if not key.get("access_key_id") or not key.get("secret_access_key"):
            sys.exit(f"lakeFS made keys for {user} that are empty")
        creds[f"lakefs-{org}"] = {"aws_access_key_id": key["access_key_id"],
                                  "aws_secret_access_key": key["secret_access_key"]}
        save()
        added.append(org)
    return added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default="~/Catalog/lakefs-acl", help="the folder for configs, databases and data")
    ap.add_argument("--lakefs", default="~/Projects/lakeFS/lakefs", help="the lakefs program")
    ap.add_argument("--acl-server", default="~/Projects/lakeFS/acl-server", help="the access-list server")
    ap.add_argument("--port", type=int, default=8002, help="the lakeFS port on 127.0.0.1")
    ap.add_argument("--acl-port", type=int, default=8011, help="the access-list server port on 127.0.0.1")
    ap.add_argument("--org", action="append", help="an org to set up; give it once per org (default: acme, globex)")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for each program")
    ap.add_argument("--cache-seconds", type=int, default=CACHE_SECONDS,
                    help="after a repair, how long lakeFS may still answer from its policy cache")
    ap.add_argument("--admin-wait", type=int, default=90,
                    help="after a repair, how long to wait for lakeFS to take the new admin keys")
    ap.add_argument("--repair-admin", action="store_true",
                    help="put the admin back, if it locked itself out through lakeFS")
    args = ap.parse_args()
    orgs = args.org or ["acme", "globex"]
    for org in orgs:
        if not ORG.fullmatch(org):
            sys.exit(f"org {org!r} must be 1 to 20 of a-z and 0-9, as provcat's org rule asks")
    lakefs, acl = program(args.lakefs), program(args.acl_server)
    home = Path(args.dir).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    write_configs(home, args.port, args.acl_port)
    check_ports(home, args.port, args.acl_port)
    if args.repair_admin and port_in_use(args.port) and port_in_use(args.acl_port):
        # The setup is running: repair it in place. Send the token only to
        # an access-list server, which refuses a call without it.
        if not (home / "api-token").exists():
            sys.exit(f"no api-token in {home}; is it the folder of the running setup?")
        if Api(args.acl_port)("GET", "/auth/users")[0] != 401:
            sys.exit(f"127.0.0.1:{args.acl_port} is not an access-list server that wants the token")
        done = repair_admin(home, args.acl_port, api_token(home))
        print("repaired the admin: " + "; ".join(done), flush=True)
        wait_for_admin(home, args.port, args.admin_wait, args.cache_seconds)
        sys.exit(0)
    for busy in (args.port, args.acl_port):
        if port_in_use(busy):
            sys.exit(f"something already listens on 127.0.0.1:{busy}; stop it, or an earlier run of this script")

    procs: list[subprocess.Popen] = []
    asked: list[float] = []

    def stop(signum, frame):
        asked.append(time.monotonic())

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
    code = 1
    try:
        code = serve(args, home, orgs, lakefs, acl, procs, asked)
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code, file=sys.stderr)
    finally:
        for proc in reversed(procs):  # lakeFS first, then the server it asks
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(GRACE)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
    sys.exit(code)


def wait_for_admin(home: Path, port: int, seconds: int, settle: int = CACHE_SECONDS) -> None:
    """Wait until lakeFS lets the saved admin keys manage policies."""
    creds = configparser.ConfigParser()
    creds.read(home / "credentials")
    admin = Api(port, (creds["lakefs"]["aws_access_key_id"], creds["lakefs"]["aws_secret_access_key"]))
    start = time.monotonic()
    # lakeFS caches each user's policies for up to 23 seconds, so an answer
    # before then may come from the policies the repair replaced. The admin
    # must manage policies and read repositories, after that time.
    while True:
        works = all(admin("GET", path)[0] == 200 for path in ("/auth/policies?amount=1", "/repositories?amount=1"))
        if works and time.monotonic() - start >= settle:
            return
        if time.monotonic() - start > seconds:
            sys.exit(f"lakeFS still refuses the new admin keys after {seconds} seconds; see its log")
        time.sleep(2)


def serve(args, home, orgs, lakefs, acl, procs, asked) -> int:
    """Start both programs, set up the orgs, and wait for a stop. The caller
    stops what is in procs."""
    # The access-list server refuses calls without this token, and lakeFS
    # sends it. Both take it from the environment, so older configs get it too.
    token = api_token(home)
    # Its cache made a deleted user or key look present for 20 seconds.
    acl_env = dict(os.environ, ACLSERVER_API_TOKEN=token, ACLSERVER_CACHE_ENABLED="false")
    acl_proc = subprocess.Popen([str(acl), "--config", str(home / "acl.yaml"), "run"], cwd=home,
                                env=acl_env, stdin=subprocess.DEVNULL, start_new_session=True)
    procs.append(acl_proc)
    if not wait_for(f"http://127.0.0.1:{args.acl_port}/_health", acl_proc, args.wait):
        print(f"the access-list server did not answer on 127.0.0.1:{args.acl_port}", file=sys.stderr)
        return 1
    if Api(args.acl_port)("GET", "/auth/users")[0] != 401:
        # A stock build ignores the token and takes every caller.
        print(f"{acl} takes calls without the token; build the access-list server from the fork",
              file=sys.stderr)
        return 1
    # Its own commit cache: the default ~/lakefs/data/cache is shared by every
    # lakeFS of this user, and names its files by storage namespace only.
    env = dict(os.environ, LAKEFS_COMMITTED_LOCAL_CACHE_DIR=str(home / "cache"), LAKEFS_AUTH_API_TOKEN=token)
    lakefs_proc = subprocess.Popen([str(lakefs), "--config", str(home / "lakefs.yaml"), "run"], cwd=home,
                                   env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    procs.append(lakefs_proc)
    if not wait_for(f"http://127.0.0.1:{args.port}/_health", lakefs_proc, args.wait):
        print(f"lakeFS did not answer on 127.0.0.1:{args.port}", file=sys.stderr)
        return 1
    if args.repair_admin:
        done = repair_admin(home, args.acl_port, token)
        print("repaired the admin: " + "; ".join(done), flush=True)
        wait_for_admin(home, args.port, args.admin_wait, args.cache_seconds)
    added = set_up(home, args.port, args.acl_port, orgs, token)
    print(f"lakeFS on http://127.0.0.1:{args.port}, with access control for {', '.join(orgs)}"
          + (f" (new: {', '.join(added)})" if added else ""), flush=True)
    print(f"export AWS_SHARED_CREDENTIALS_FILE={home / 'credentials'}", flush=True)
    while not asked and all(p.poll() is None for p in procs):
        time.sleep(0.2)
    if not asked:
        print("a program stopped by itself; stopping the other", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    main()
