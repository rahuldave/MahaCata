"""Shared fixtures, and the three kinds of test.

- tests/unit: no servers. Only the code, temporary files and monkeypatching.
- tests/integration: servers on this machine. Fakes for GitHub and etcd run in
  the test process, moto runs S3, and emulators in Docker run GCS and Azure.
- tests/system: real services on the network, such as github.com, AWS, GCS,
  Azure, lakeFS and etcd. Each test skips unless its environment variable is
  set, and "pytest" leaves the folder out unless you ask for it with -m system.

Each test gets the marker of its folder, so "pytest -m unit" runs one kind.
"""
import base64
import io
import json
import tarfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from pathlib import Path

import pytest

KINDS = ("unit", "integration", "system")


def pytest_collection_modifyitems(config, items):
    here = Path(__file__).parent
    for item in items:
        try:
            kind = item.path.relative_to(here).parts[0]
        except ValueError:
            continue
        if kind in KINDS:
            item.add_marker(kind)

SHA_A = "a" * 40
SHA_B = "b" * 40
TAG_OBJ = "c" * 40


class FakeGithub:
    """A tiny GitHub API with one repository, owner/repo, and two commits."""

    def __init__(self):
        self.commits = {
            SHA_A: {"README.md": b"hello A\n", "src/app.py": b"print('A')\n", "src/lib/util.py": b"u = 1\n"},
            SHA_B: {"README.md": b"hello B\n", "src/app.py": b"print('B')\n"},
        }
        self.heads = {"main": SHA_B, "feat/x": SHA_A, "release": SHA_A}
        # v1 is a plain tag, v2 an annotated tag object, main a tag that shares a branch name
        self.tags = {"v1": ("commit", SHA_A), "v2": ("tag", TAG_OBJ), "main": ("commit", SHA_A)}
        self.tag_objects = {TAG_OBJ: SHA_A}
        self.hits = Counter()
        self.headers_seen = []
        self.extra_tar_members = []  # (name, bytes, type) added to every tarball
        self.tarball_redirect = None  # a URL to send tarball requests to

    def files(self, sha):
        return self.commits[sha]

    def handle(self, h: BaseHTTPRequestHandler):
        url = urlsplit(h.path)
        path = unquote(url.path)
        q = parse_qs(url.query)
        accept = h.headers.get("Accept", "")
        self.headers_seen.append(dict(h.headers))
        prefix = "/repos/owner/repo"
        if not path.startswith(prefix):
            return h.send_error(404)
        rest = path[len(prefix):]
        kind = rest.split("/")[1] if rest.count("/") >= 1 else ""
        self.hits[kind or "repo"] += 1
        if rest == "":
            return self.send_json(h, {"default_branch": "main"})
        if kind == "git":
            sub = rest[len("/git/"):]
            if sub.startswith("ref/heads/") and sub[len("ref/heads/"):] in self.heads:
                return self.send_json(h, {"object": {"type": "commit", "sha": self.heads[sub[len("ref/heads/"):]]}})
            if sub.startswith("ref/tags/") and sub[len("ref/tags/"):] in self.tags:
                typ, sha = self.tags[sub[len("ref/tags/"):]]
                return self.send_json(h, {"object": {"type": typ, "sha": sha}})
            if sub.startswith("tags/") and sub[len("tags/"):] in self.tag_objects:
                return self.send_json(h, {"object": {"type": "commit", "sha": self.tag_objects[sub[len("tags/"):]]}})
            return h.send_error(404)
        if kind == "commits":
            ref = rest[len("/commits/"):]
            sha = next((s for s in self.commits if s.startswith(ref)), None) if len(ref) >= 7 else None
            if sha is None:
                return h.send_error(404)
            return self.send(h, sha.encode(), "text/plain")
        if kind == "contents":
            sha = q["ref"][0]
            target = rest[len("/contents/"):].strip("/")
            files = self.files(sha)
            if target in files:
                if "raw" in accept:
                    return self.send(h, files[target], "application/octet-stream")
                return self.send_json(h, {"type": "file", "name": target.rsplit("/", 1)[-1], "size": len(files[target])})
            pre = target + "/" if target else ""
            names = {}
            for f in files:
                if f.startswith(pre):
                    head = f[len(pre):].split("/")
                    names[head[0]] = "file" if len(head) == 1 else "dir"
            if not names:
                return h.send_error(404)
            return self.send_json(h, [{"name": n, "type": t, "size": len(files.get(pre + n, b""))}
                                      for n, t in sorted(names.items())])
        if kind == "tarball":
            sha = rest[len("/tarball/"):]
            if self.tarball_redirect and "redirected" not in q:
                h.send_response(302)
                h.send_header("Location", f"{self.tarball_redirect}{path}?redirected=1")
                h.end_headers()
                return
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                top = f"owner-repo-{sha[:7]}"
                for name, data in self.files(sha).items():
                    info = tarfile.TarInfo(f"{top}/{name}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                for name, data, typ in self.extra_tar_members:
                    info = tarfile.TarInfo(f"{top}/{name}")
                    info.type = typ
                    if typ == tarfile.SYMTYPE:
                        info.linkname = data.decode()
                        tar.addfile(info)
                    else:
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            return self.send(h, buf.getvalue(), "application/gzip")
        h.send_error(404)

    @staticmethod
    def send(h, body: bytes, ctype: str):
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def send_json(self, h, obj):
        self.send(h, json.dumps(obj).encode(), "application/json")


class FakeEtcd:
    def __init__(self):
        self.kv = {}

    def handle(self, h: BaseHTTPRequestHandler):
        body = json.loads(h.rfile.read(int(h.headers["Content-Length"])))
        dec = lambda v: base64.b64decode(v).decode()  # noqa: E731
        key = dec(body["key"]) if "key" in body else None
        if h.path == "/v3/kv/txn":
            for op in body.get("success", []):
                if "request_put" in op:
                    self.kv[dec(op["request_put"]["key"])] = dec(op["request_put"]["value"])
                else:
                    self.kv.pop(dec(op["request_delete_range"]["key"]), None)
            out = {"succeeded": True}
        elif h.path == "/v3/kv/put":
            self.kv[key] = dec(body["value"])
            out = {}
        elif h.path == "/v3/kv/range":
            out = {"kvs": [{"value": base64.b64encode(self.kv[key].encode()).decode()}]} if key in self.kv else {}
        else:
            return h.send_error(404)
        FakeGithub.send(h, json.dumps(out).encode(), "application/json")


def serve(app):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            app.handle(self)

        do_POST = do_GET

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 64  # the parallel tests open more than the default 5

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def github():
    app = FakeGithub()
    server = serve(app)
    app.api_url = f"http://127.0.0.1:{server.server_port}"
    yield app
    server.shutdown()


@pytest.fixture
def github2():
    """A second fake GitHub on another port, standing in for another host."""
    app = FakeGithub()
    server = serve(app)
    app.api_url = f"http://localhost:{server.server_port}"
    yield app
    server.shutdown()


@pytest.fixture
def etcd():
    app = FakeEtcd()
    server = serve(app)
    app.url = f"etcd://127.0.0.1:{server.server_port}"
    yield app
    server.shutdown()


@pytest.fixture
def s3_endpoint(monkeypatch):
    from moto.server import ThreadedMotoServer

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


# Cloud emulators in Docker, one container per test session. A test that needs
# one skips when Docker or the image is missing.

GCS_IMAGE = "fsouza/fake-gcs-server:1.52.2"
AZURITE_IMAGE = "mcr.microsoft.com/azure-storage/azurite:3.35.0"
# The fixed, public key of the Azurite development account.
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _container(image: str, port: int, inner: int, *args: str):
    import shutil
    import subprocess
    import time
    import urllib.request

    if not shutil.which("docker"):
        pytest.skip("Docker is not installed")
    have = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if have.returncode:
        pytest.skip(f"pull the emulator first: docker pull {image}")
    cid = subprocess.run(["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:{inner}", image, *args],
                         capture_output=True, text=True, check=True).stdout.strip()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except urllib.error.HTTPError:
            break  # the server answered
        except OSError:
            time.sleep(0.1)
    return cid


def _stop(cid: str) -> None:
    import subprocess

    subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


@pytest.fixture(scope="session")
def gcs_endpoint():
    port = _free_port()
    cid = _container(GCS_IMAGE, port, 4443, "-scheme", "http", "-port", "4443",
                     "-public-host", f"127.0.0.1:{port}", "-external-url", f"http://127.0.0.1:{port}")
    yield f"http://127.0.0.1:{port}"
    _stop(cid)


@pytest.fixture(scope="session")
def azurite_connection():
    port = _free_port()
    cid = _container(AZURITE_IMAGE, port, 10000, "azurite-blob", "--blobHost", "0.0.0.0",
                     "--skipApiVersionCheck", "--loose")
    yield ("DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
           f"AccountKey={AZURITE_KEY};BlobEndpoint=http://127.0.0.1:{port}/devstoreaccount1;")
    _stop(cid)
