"""scripts/lakefs_acl_local.py, run against fake acl-server and lakefs programs.

These start real processes and talk to them over HTTP on this machine, so
they are integration tests."""
import configparser
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "lakefs_acl_local.py"

# One fake for both programs. It reads listen_address from its --config file,
# keeps its users, groups and keys in state.json beside that file, and logs
# each call. FAKE_EMPTY_KEYS makes it hand out empty keys, and FAKE_DIE makes
# the named program stop by itself after start.
FAKE = textwrap.dedent('''\
    #!{python}
    import base64, json, os, re, signal, sys, threading, time, uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import unquote
    from pathlib import Path
    name = Path(sys.argv[0]).name
    config = Path(sys.argv[sys.argv.index("--config") + 1])
    host, port = re.search(r"listen_address: (\\S+):(\\d+)", config.read_text()).groups()
    state_file = config.parent / f"state-{{name}}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {{"users": {{}}, "groups": {{}}, "policies": {{}}, "keys": {{}}, "attached": {{}}}}
    if name == "acl-server" and not state_file.exists():
        # As the real server's first start: the Admins group with its policy.
        state["groups"]["Admins"] = []
        state["policies"]["ACL(_-_)Admins"] = []
        state["attached"]["groups:Admins"] = ["ACL(_-_)Admins"]
    log = open(config.parent / "calls.log", "a")
    log.write(f"{{name}} cache {{os.environ.get('LAKEFS_COMMITTED_LOCAL_CACHE_DIR')}}\\n")
    token = os.environ.get("ACLSERVER_API_TOKEN") or os.environ.get("LAKEFS_AUTH_API_TOKEN") or ""
    log.write(f"{{name}} token {{token}}\\n"); log.flush()
    def save():
        state_file.write_text(json.dumps(state))
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def reply(self, code, body=None):
            data = json.dumps(body or {{}}).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers()
            self.wfile.write(data)
        def handle_any(self, method):
            path = self.path.split("?")[0]
            authed = "Authorization" in self.headers
            log.write(f"{{name}} {{method}} {{path}} {{'auth' if authed else 'anon'}}\\n"); log.flush()
            if path in ("/_health", "/api/v1/healthcheck"):
                return self.reply(204)
            if name == "acl-server" and not os.environ.get("FAKE_STOCK_ACL") and \\
                    self.headers.get("Authorization") != f"Bearer {{token}}":
                return self.reply(401, {{"message": "missing or wrong api token"}})
            if path == "/api/v1/user":
                given = base64.b64decode(self.headers.get("Authorization", "Basic ")[6:]).decode().split(":")[0]
                return self.reply(200 if given in state["keys"] else 401)
            m = re.fullmatch(r"/api/v1/auth/users/([^/]+)/credentials", path)
            if m and method == "GET":
                mine = [k for k, u in state["keys"].items() if u == m.group(1)]
                return self.reply(200, {{"results": [{{"access_key_id": k}} for k in mine]}})
            m = re.fullmatch(r"/api/v1/auth/users/([^/]+)/credentials/([^/]+)", path)
            if m and method == "DELETE":
                state["keys"].pop(m.group(2), None); save()
                return self.reply(204)
            if path == "/api/v1/setup_lakefs":
                return self.reply(200, {{"access_key_id": "", "secret_access_key": ""}})
            m = re.fullmatch(r"/api/v1/auth/(users|groups|policies)/([^/]+)", path)
            if m and method == "GET":
                return self.reply(200 if unquote(m.group(2)) in state[m.group(1)] else 404)
            m = re.fullmatch(r"/api/v1/auth/(users|groups|policies)", path)
            if m and method == "POST":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state[m.group(1)][body.get("id") or body.get("username") or body.get("name")] = []
                save(); return self.reply(201)
            m = re.fullmatch(r"/api/v1/auth/policies/([^/]+)", path)
            if m and method == "PUT":
                return self.reply(200)
            if path == "/api/v1/repositories" and method == "GET":
                return self.reply(200, {{"results": []}})
            m = re.fullmatch(r"/api/v1/auth/policies/([^/]+)", path)
            if m and method == "PUT":
                return self.reply(200)
            if path == "/api/v1/auth/policies" and method == "GET":
                if os.environ.get("FAKE_ADMIN_REFUSED"):
                    return self.reply(401, {{"message": "insufficient permissions"}})
                return self.reply(200, {{"results": []}})
            m = re.fullmatch(r"/api/v1/auth/(groups|users)/([^/]+)/policies", path)
            if m and method == "GET":
                names = state["attached"].get(m.group(1) + ":" + m.group(2), [])
                return self.reply(200, {{"results": [{{"name": n}} for n in names]}})
            m = re.fullmatch(r"/api/v1/auth/(groups|users)/([^/]+)/policies/([^/]+)", path)
            if m and method in ("PUT", "DELETE"):
                names = state["attached"].setdefault(m.group(1) + ":" + m.group(2), [])
                p = unquote(m.group(3))
                if method == "DELETE":
                    if p in names:
                        names.remove(p)
                    save(); return self.reply(204)
                if p in names:
                    return self.reply(409)
                names.append(p); save(); return self.reply(201)
            m = re.fullmatch(r"/api/v1/auth/users/([^/]+)/groups", path)
            if m and method == "GET":
                mine = [g for g, members in state["groups"].items() if m.group(1) in members]
                return self.reply(200, {{"results": [{{"name": g}} for g in mine]}})
            m = re.fullmatch(r"/api/v1/auth/groups/([^/]+)/members/([^/]+)", path)
            if m and method == "DELETE":
                members = state["groups"].get(m.group(1), [])
                if m.group(2) in members:
                    members.remove(m.group(2))
                save(); return self.reply(204)
            if m and method == "PUT":
                members = state["groups"].setdefault(m.group(1), [])
                if m.group(2) in members:
                    return self.reply(409)
                members.append(m.group(2)); save()
                return self.reply(201)
            m = re.fullmatch(r"/api/v1/auth/users/([^/]+)/credentials", path)
            if m and method == "POST":
                if os.environ.get("FAKE_EMPTY_KEYS"):
                    return self.reply(201, {{"access_key_id": "", "secret_access_key": ""}})
                key = "AK" + uuid.uuid4().hex[:8]
                state["keys"][key] = m.group(1); save()
                return self.reply(201, {{"access_key_id": key, "secret_access_key": "SK" + uuid.uuid4().hex}})
            return self.reply(404, {{"message": "not found"}})
        def do_GET(self): self.handle_any("GET")
        def do_POST(self): self.handle_any("POST")
        def do_PUT(self): self.handle_any("PUT")
        def do_DELETE(self): self.handle_any("DELETE")
    server = ThreadingHTTPServer((host, int(port)), H)
    def stop(signum, frame):
        log.write(f"{{name}} stopped\\n"); log.close(); os._exit(0)
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if os.environ.get("FAKE_DIE") == name:
        time.sleep(1); log.write(f"{{name}} died\\n"); log.close(); os._exit(3)
    while True:
        time.sleep(1)
    ''')


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    for name in ("lakefs", "acl-server"):
        program = build / name
        program.write_text(FAKE.format(python=sys.executable))
        program.chmod(0o755)
    return tmp_path, build, tmp_path / "home", free_port(), free_port()


def start(world, *extra, **env):
    tmp, build, home, port, acl_port = world
    args = [sys.executable, str(SCRIPT), "--dir", str(home), "--lakefs", str(build / "lakefs"),
            "--acl-server", str(build / "acl-server"), "--port", str(port),
            "--acl-port", str(acl_port), "--wait", "10",
            # The fakes have no policy cache, so a repair need not wait for one.
            "--cache-seconds", "0", "--admin-wait", "6", *extra]
    return subprocess.Popen(args, env=dict(os.environ, **env), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


def wait_ready(proc, seconds=30):
    """Read the script's output until it says it is ready. On a timeout, ask
    it to stop, so it stops its servers too, which hold its output open."""
    import queue
    import threading

    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout] + [lines.put(None)],
                     daemon=True).start()
    seen = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        seen.append(line)
        if line.startswith("export AWS_SHARED_CREDENTIALS_FILE="):
            proc._lines = lines  # stop() reads the rest
            return "".join(seen)
    proc.terminate()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
    raise AssertionError("not ready: " + "".join(seen) + (proc.stderr.read() or ""))


def stop(proc):
    proc.send_signal(signal.SIGINT)
    code = proc.wait(timeout=60)
    out = []
    lines = getattr(proc, "_lines", None)
    while lines is not None:
        line = lines.get(timeout=10)
        if line is None:
            break
        out.append(line)
    return code, "".join(out), proc.stderr.read()


def profiles(home):
    cp = configparser.ConfigParser()
    cp.read(home / "credentials")
    return {s: (cp[s]["aws_access_key_id"], cp[s]["aws_secret_access_key"]) for s in cp.sections()}


def test_first_run_makes_admin_and_orgs_and_stops_both(world):
    home = world[2]
    proc = start(world)
    out = wait_ready(proc)
    assert "(new: acme, globex)" in out
    code, _, err = stop(proc)
    assert code == 0, err
    keys = profiles(home)
    assert set(keys) == {"lakefs", "lakefs-acme", "lakefs-globex"}
    assert all(a and s for a, s in keys.values())
    for f in ("credentials", "acl.yaml", "lakefs.yaml"):
        assert (home / f).stat().st_mode & 0o777 == 0o600
    for key in [k for pair in keys.values() for k in pair]:
        assert key not in out + err  # no key is shown
    calls = (home / "calls.log").read_text()
    # The admin is made in the access-list server, with the token; the orgs
    # through lakeFS, with the admin keys.
    assert "acl-server POST /api/v1/auth/users auth" in calls
    assert "acl-server PUT /api/v1/auth/groups/Admins/members/admin auth" in calls
    assert "lakefs POST /api/v1/auth/policies auth" in calls
    assert "lakefs PUT /api/v1/auth/groups/acme/members/acme-user auth" in calls
    # lakeFS stops first, then the server it asks.
    assert calls.index("lakefs stopped") < calls.index("acl-server stopped")


def test_configs_keep_to_127_0_0_1_with_external_access_control(world):
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    lakefs, acl = (home / "lakefs.yaml").read_text(), (home / "acl.yaml").read_text()
    assert "rbac: external" in lakefs and "endpoint: http://127.0.0.1:" in lakefs
    assert "listen_address: 127.0.0.1:" in lakefs and "listen_address: 127.0.0.1:" in acl
    secrets = [line.split(":", 1)[1].strip() for line in (lakefs + acl).splitlines() if "secret_key" in line]
    assert len(secrets) == 2 and secrets[0] != secrets[1] and all(len(s) == 64 for s in secrets)


def test_a_later_run_keeps_keys_and_adds_only_new_orgs(world):
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    first = profiles(home)
    configs = (home / "lakefs.yaml").read_text(), (home / "acl.yaml").read_text()
    proc = start(world, "--org", "acme", "--org", "globex", "--org", "initech")
    out = wait_ready(proc)
    stop(proc)
    assert "(new: initech)" in out
    second = profiles(home)
    assert {k: second[k] for k in first} == first and "lakefs-initech" in second
    assert ((home / "lakefs.yaml").read_text(), (home / "acl.yaml").read_text()) == configs


@pytest.mark.parametrize("org", ["Acme", "acme-corp", "", "a" * 21, "../x"])
def test_a_bad_org_name_is_refused_before_anything_starts(world, org):
    proc = start(world, "--org", org)
    code, _, err = proc.wait(timeout=30), *proc.communicate()
    assert code and "must be 1 to 20 of a-z and 0-9" in err
    assert not world[2].exists()


def test_empty_keys_are_refused(world):
    proc = start(world, FAKE_EMPTY_KEYS="1")
    code, _, err = proc.wait(timeout=60), *proc.communicate()
    assert code == 1 and "admin keys that are empty" in err
    assert not (world[2] / "credentials").exists()
    calls = (world[2] / "calls.log").read_text()
    assert "lakefs stopped" in calls and "acl-server stopped" in calls


def test_an_admin_without_saved_keys_gets_new_keys(world):
    # Review finding 4: a first run that made the admin but lost its keys once
    # left a state that no rerun could fix.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    old = profiles(home)["lakefs"]
    (home / "credentials").unlink()
    proc = start(world)
    wait_ready(proc)
    code, _, err = stop(proc)
    assert code == 0, err
    keys = profiles(home)
    assert keys["lakefs"] != old and all(keys["lakefs"])
    calls = (home / "calls.log").read_text()
    assert calls.count("acl-server POST /api/v1/auth/users auth") == 1  # the admin is not made twice
    assert calls.count("acl-server POST /api/v1/auth/users/admin/credentials auth") == 2
    # Review 3, finding B1: the lost keys are deleted, not left working.
    assert f"acl-server DELETE /api/v1/auth/users/admin/credentials/{old[0]} auth" in calls


def test_a_saved_key_that_no_longer_works_is_replaced(world):
    # Review 3, finding B2: an org whose saved keys fail gets new keys.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    first = profiles(home)
    text = (home / "credentials").read_text().replace(first["lakefs-acme"][0], "AKBROKEN")
    (home / "credentials").write_text(text)
    proc = start(world)
    out = wait_ready(proc)
    stop(proc)
    assert "(new: acme)" in out
    second = profiles(home)
    assert second["lakefs-acme"][0] not in (first["lakefs-acme"][0], "AKBROKEN")
    assert second["lakefs-globex"] == first["lakefs-globex"] and second["lakefs"] == first["lakefs"]
    calls = (home / "calls.log").read_text()
    assert f"lakefs DELETE /api/v1/auth/users/acme-user/credentials/{first['lakefs-acme'][0]} auth" in calls


def test_lakefs_gets_its_own_commit_cache(world):
    # Review 3, finding A2: the default cache is shared by every lakeFS.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    calls = (home / "calls.log").read_text()
    assert f"lakefs cache {home.resolve() / 'cache'}" in calls


def test_policies_are_written_again_on_every_run(world):
    # Review finding 6: a changed policy now reaches an existing setup.
    home = world[2]
    for _ in range(2):
        proc = start(world)
        wait_ready(proc)
        stop(proc)
    calls = (home / "calls.log").read_text()
    assert calls.count("lakefs POST /api/v1/auth/policies auth") == 2  # acme and globex, once
    assert "lakefs PUT /api/v1/auth/policies/acme-repos auth" in calls
    assert "lakefs PUT /api/v1/auth/policies/globex-repos auth" in calls
    assert not (home / "credentials.tmp").exists()
    assert (home / "credentials").stat().st_mode & 0o777 == 0o600


def test_a_port_that_is_in_use_is_refused(world):
    # Review finding 7: an old server on the port once answered for the new one.
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", world[3]))
        busy.listen()
        proc = start(world)
        code, _, err = proc.wait(timeout=30), *proc.communicate()
    assert code and f"something already listens on 127.0.0.1:{world[3]}" in err
    assert not (world[2] / "calls.log").exists()


def test_a_port_that_only_starts_like_the_first_is_refused(world):
    # Review finding 5: the check once matched 800 inside 8002.
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    shorter = int(str(world[3])[:-1])
    proc = start(world[:3] + (shorter, world[4]))
    code, _, err = proc.wait(timeout=30), *proc.communicate()
    assert code and "was made with other ports" in err


def test_paths_in_the_configs_survive_yaml(tmp_path):
    # Review finding 8: a folder name with ": " or " #" once broke the YAML.
    import importlib.util

    import yaml

    spec = importlib.util.spec_from_file_location("lakefs_acl_local", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / "a: b #c"
    home.mkdir()
    mod.write_configs(home, 8002, 8011)
    acl = yaml.safe_load((home / "acl.yaml").read_text())
    lakefs = yaml.safe_load((home / "lakefs.yaml").read_text())
    assert acl["database"]["local"]["path"] == str(home / "acl-db")
    assert lakefs["database"]["local"]["path"] == str(home / "lakefs-db")
    assert lakefs["blockstore"]["local"]["path"] == str(home / "data")


@pytest.mark.parametrize("dies", ["lakefs", "acl-server"])
def test_a_program_that_stops_by_itself_stops_the_other(world, dies):
    proc = start(world, FAKE_DIE=dies)
    code, _, err = proc.wait(timeout=60), *proc.communicate()
    assert code == 1
    calls = (world[2] / "calls.log").read_text()
    assert f"{dies} died" in calls
    other = "acl-server" if dies == "lakefs" else "lakefs"
    if f"{other} GET" in calls:  # it had started
        assert f"{other} stopped" in calls


def test_a_missing_program_is_named(world):
    tmp, build, home = world[:3]
    (build / "acl-server").unlink()
    proc = start(world)
    code, _, err = proc.wait(timeout=30), *proc.communicate()
    assert code and f"no program at {(build / 'acl-server').resolve()}" in err
    assert not home.exists()


def test_other_ports_than_the_first_run_are_refused(world):
    # Review of the script: the configs keep the first ports, so a new
    # --port once started lakeFS where the script did not look.
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    proc = start(world[:3] + (free_port(), world[4]))
    code, _, err = proc.wait(timeout=30), *proc.communicate()
    assert code and "was made with other ports" in err
    assert "lakefs stopped" in (world[2] / "calls.log").read_text()  # only from the first run
    assert (world[2] / "calls.log").read_text().count("lakefs stopped") == 1


def test_both_programs_share_one_token_that_is_kept(world):
    # The access-list server refuses calls without the token, and lakeFS sends it.
    home = world[2]
    for _ in range(2):
        proc = start(world)
        out = wait_ready(proc)
        code, _, err = stop(proc)
        assert code == 0, err
    token = (home / "api-token").read_text().strip()
    assert len(token) == 64 and (home / "api-token").stat().st_mode & 0o777 == 0o600
    calls = (home / "calls.log").read_text()
    assert calls.count(f"acl-server token {token}") == 2 and calls.count(f"lakefs token {token}") == 2
    assert token not in out + err


def test_a_server_that_takes_calls_without_the_token_is_refused(world):
    # Review 5, finding 4: a stock access-list server ignores the token.
    proc = start(world, FAKE_STOCK_ACL="1")
    code, _, err = proc.wait(timeout=60), *proc.communicate()
    assert code == 1 and "takes calls without the token" in err
    calls = (world[2] / "calls.log").read_text()
    assert "acl-server stopped" in calls and "lakefs token" not in calls  # lakeFS never started


@pytest.mark.parametrize("problem, text", [("short", "shorter than 32"), ("open", "can be read by other users")])
def test_a_weak_token_file_is_refused(world, problem, text):
    # Review 5, finding 5.
    home = world[2]
    home.mkdir()
    token = home / "api-token"
    token.write_text("abc" if problem == "short" else "a" * 64)
    token.chmod(0o600 if problem == "short" else 0o644)
    proc = start(world)
    code, _, err = proc.wait(timeout=60), *proc.communicate()
    assert code == 1 and text in err


def lock_out(home, **changes):
    """Edit the stopped fake access-list server's state, as a lockout would."""
    import json

    path = home / "state-acl-server.json"
    state = json.loads(path.read_text())
    for change, value in changes.items():
        if change == "drop_group":
            state["groups"].pop(value, None)
            state["attached"].pop(f"groups:{value}", None)
        elif change == "drop_user":
            state["users"].pop(value, None)
            for members in state["groups"].values():
                if value in members:
                    members.remove(value)
        elif change == "user_policy":
            state["attached"].setdefault("users:admin", []).append(value)
        elif change == "group_policy":
            state["attached"].setdefault("groups:Admins", []).append(value)
        elif change == "join_group":
            state["groups"].setdefault(value, []).append("admin")
        elif change == "drop_keys":
            state["keys"] = {k: u for k, u in state["keys"].items() if u != "admin"}
    path.write_text(json.dumps(state))


def test_repair_admin_undoes_each_lockout_as_it_starts(world):
    # Review 7: --repair-admin had no unit test.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    lock_out(home, drop_group="Admins", user_policy="deny-me", group_policy="deny-all")
    proc = start(world, "--repair-admin")
    out = wait_ready(proc)
    code, _, err = stop(proc)
    assert code == 0, err
    for done in ("made the Admins group", "attached ACL(_-_)Admins to Admins", "put the admin back in Admins",
                 "detached deny-me from the admin", "made new admin keys"):
        assert done in out, out
    calls = (home / "calls.log").read_text()
    assert "acl-server DELETE /api/v1/auth/users/admin/policies/deny-me auth" in calls


def test_repair_admin_makes_a_deleted_admin_new_keys(world):
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    old = profiles(home)["lakefs"]
    lock_out(home, drop_user="admin")
    proc = start(world, "--repair-admin")
    out = wait_ready(proc)
    stop(proc)
    assert "made the admin user" in out
    new = profiles(home)["lakefs"]
    assert new != old and all(new)


def test_repair_admin_in_place_on_a_running_setup(world):
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    try:
        repair = start(world, "--repair-admin")
        out, err = repair.communicate(timeout=90)
        assert repair.returncode == 0, err
        assert "repaired the admin: made new admin keys" in out  # a healthy setup only gets new keys
    finally:
        stop(proc)


def test_repair_admin_leaves_other_groups_and_drops_old_keys(world):
    # Review 7, findings 1 and 5: a deny policy on another group, and the
    # admin's keys deleted, were not undone.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    old = profiles(home)["lakefs"]
    lock_out(home, join_group="Evil", drop_keys=True)
    proc = start(world, "--repair-admin")
    out = wait_ready(proc)
    stop(proc)
    assert "took the admin out of Evil" in out and "made new admin keys" in out
    assert profiles(home)["lakefs"] != old


def test_repair_in_place_needs_the_token_file(world):
    # Review 7, finding 8: a missing token file was made anew, and wrong.
    home = world[2]
    proc = start(world)
    wait_ready(proc)
    try:
        token = (home / "api-token").read_text()
        (home / "api-token").unlink()
        repair = start(world, "--repair-admin")
        _, err = repair.communicate(timeout=60)
        assert repair.returncode == 1 and "no api-token in" in err
        assert not (home / "api-token").exists()
    finally:
        (home / "api-token").write_text(token)
        (home / "api-token").chmod(0o600)
        stop(proc)


def test_repair_that_lakefs_still_refuses_exits_1(world):
    # Review 7: a repair whose keys lakeFS refuses must fail, not report success.
    proc = start(world)
    wait_ready(proc)
    stop(proc)
    proc = start(world, "--repair-admin", FAKE_ADMIN_REFUSED="1")
    try:
        code = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read()
    assert code == 1 and "lakeFS still refuses the new admin keys" in err
