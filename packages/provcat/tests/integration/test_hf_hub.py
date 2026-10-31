"""The real Hub class of the hf driver, against a fake Hub API on this machine."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from provcat import Catalog, NotFound, StoreError

pytestmark = pytest.mark.integration

SHA, OLD = "c" * 40, "d" * 40
FILES = {"README.md": b"hello\n", "data/a.txt": b"a" * 10, "data/b.bin": b"b" * 50}
LFS = {"data/b.bin"}
API = "/api/datasets/acme/wiki/"


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _entry(path: str) -> dict:
    if path not in FILES:
        return {"type": "directory", "oid": "0" * 40, "path": path}
    data = FILES[path]
    out = {"type": "file", "oid": _blob(data), "size": len(data), "path": path}
    if path in LFS:
        out["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data), "pointerSize": 130}
    return out


class FakeHubServer:
    def __init__(self):
        self.seen: list[str] = []
        self.auth: list[str | None] = []
        self.commits = [{"id": SHA, "date": "2026-03-01T00:00:00.000Z"},
                        {"id": OLD, "date": "2026-01-01T00:00:00.000Z"}]
        self.page_limit = None  # set by the client
        self.tree_page = 1  # entries in one page of a tree listing
        self.fail = {}  # path -> number of 503 replies to give first
        self.xet = set()  # files whose HEAD names a Xet hash
        self.cdn = None  # the URL of another server that serves the files
        self.link_host = None  # the host that page links name

    def handle(self, h: BaseHTTPRequestHandler, method: str):
        u = urlsplit(h.path)
        path, q = unquote(u.path), parse_qs(u.query)
        self.seen.append(f"{method} {path}?{u.query}")
        self.auth.append(h.headers.get("Authorization"))
        if self.fail.get(path):
            self.fail[path] -= 1
            return self.error(h, 503, "Busy")
        if path == API + "refs":
            return self.json(h, {"branches": [{"name": "main", "ref": "refs/heads/main", "targetCommit": SHA}],
                                 "tags": [{"name": "v1", "ref": "refs/tags/v1", "targetCommit": OLD}]})
        if path.startswith(API + "revision/"):
            rev = path.rpartition("/")[2]
            if rev in (SHA, "main") or SHA.startswith(rev):
                return self.json(h, {"sha": SHA})
            return self.error(h, 404, "RevisionNotFound")
        if path.startswith(API + "commits/"):
            p, limit = int(q["p"][0]), int(q["limit"][0])
            self.page_limit = limit
            return self.json(h, self.commits[p * limit:(p + 1) * limit])
        if method == "POST" and path == API + f"paths-info/{SHA}":
            n = int(h.headers["Content-Length"])
            want = parse_qs(h.rfile.read(n).decode())["paths"]
            got = [_entry(p) for p in want if p in FILES or any(f.startswith(p + "/") for f in FILES)]
            return self.json(h, got)
        if path.startswith(API + f"tree/{SHA}"):
            sub = path[len(API + f"tree/{SHA}"):].strip("/")
            rec = q.get("recursive", ["False"])[0].lower() == "true"
            prefix = sub + "/" if sub else ""
            names = sorted({f[:len(prefix)] + f[len(prefix):].split("/")[0] if not rec else f
                            for f in FILES if f.startswith(prefix)})
            start = int(q.get("cursor", ["0"])[0])
            page = names[start:start + self.tree_page]
            more = start + self.tree_page < len(names)
            link = None
            if more:
                host = self.link_host or h.headers["Host"]
                link = f'<http://{host}{u.path}?recursive={rec}&cursor={start + self.tree_page}>; rel="next"'
            return self.json(h, [_entry(n) for n in page], link)
        if path.startswith(f"/datasets/acme/wiki/resolve/{SHA}/"):
            name = path.split(f"/resolve/{SHA}/", 1)[1]
            data = FILES[name]
            h.send_response(302 if self.cdn else 200)
            if self.cdn:
                h.send_header("Location", f"{self.cdn}/{name}")
            h.send_header("Content-Length", "0" if self.cdn else str(len(data)))
            h.send_header("X-Linked-Size", str(len(data)))
            h.send_header("ETag", f'"{_blob(data)}"')
            h.send_header("X-Repo-Commit", SHA)
            if name in self.xet:
                h.send_header("X-Xet-Hash", "f" * 64)
                h.send_header("X-Xet-Refresh-Route", f"{self.url}/api/datasets/acme/wiki/xet-read-token/{SHA}")
            h.end_headers()
            if method == "GET" and not self.cdn:
                h.wfile.write(data)
            return
        return self.error(h, 404, "EntryNotFound")

    @staticmethod
    def json(h, obj, link=None):
        body = json.dumps(obj).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        if link:
            h.send_header("Link", link)
        h.end_headers()
        h.wfile.write(body)

    @staticmethod
    def error(h, code, kind):
        body = json.dumps({"error": kind}).encode()
        h.send_response(code)
        h.send_header("X-Error-Code", kind)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)


@pytest.fixture
def server():
    app = FakeHubServer()

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            app.handle(self, "GET")

        def do_HEAD(self):
            app.handle(self, "HEAD")

        def do_POST(self):
            app.handle(self, "POST")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.url = f"http://127.0.0.1:{srv.server_port}"
    yield app
    srv.shutdown()


def _serve(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def cat(server, tmp_path, offline=False):
    cfg = {"stores": {"hf": {"endpoint": server.url, "token_env": ["PROVCAT_TEST_HF_TOKEN"], "timeout": 5}}}
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


def test_real_hub_pins_stats_lists_and_fetches(server, tmp_path):
    c = cat(server, tmp_path)
    base = "hf://datasets/acme/wiki"
    assert c.pin(base).selector.value == SHA
    assert c.pin(base + "?at=tag:v1").selector.value == OLD
    assert c.pin(base + "?at=commit:ccccccc").selector.value == SHA
    assert c.pin(base + "?at=ts:2026-02-01").selector.value == OLD
    assert server.page_limit == 100
    e = c.stat(base + "/data/b.bin")
    assert (e.kind, e.size) == ("file", 50)
    assert c.stat(base + "/data").kind == "folder"
    with pytest.raises(NotFound):
        c.stat(base + "/nope")
    assert sorted(x.uri.segments[-1] for x in c.list(base)) == ["README.md", "data"]  # two pages
    assert any("cursor=1" in s for s in server.seen)
    assert any("/revision/ccccccc?expand=sha" in s for s in server.seen)
    assert c.read(base + "/README.md") == b"hello\n"
    d = c.fetch(base + "/data")
    assert (d / "a.txt").read_bytes() == b"a" * 10 and (d / "b.bin").read_bytes() == b"b" * 50
    # Every call names the commit; none reads main, which may differ from the pin.
    reads = [s for s in server.seen if "/tree/" in s or "/resolve/" in s or "paths-info" in s]
    assert reads and all(SHA in s for s in reads)
    with pytest.raises(NotFound):
        c.stat(base + "?at=commit:" + "e" * 40)


def test_real_hub_checks_the_bytes(server, tmp_path, monkeypatch):
    import provcat.stores.hf as hfmod
    real = hfmod.Hub.download

    def flip(self, kind, repo, commit, item, dest):
        real(self, kind, repo, commit, item, dest)
        dest.write_bytes(b"x" * dest.stat().st_size)

    monkeypatch.setattr(hfmod.Hub, "download", flip)
    with pytest.raises(StoreError, match="other content"):
        cat(server, tmp_path).fetch("hf://datasets/acme/wiki/data/b.bin")


@pytest.fixture
def no_backoff_wait(monkeypatch):
    import huggingface_hub.utils._http as h
    monkeypatch.setattr(h.time, "sleep", lambda s: None)


def test_real_hub_tries_again_after_a_503(server, tmp_path, no_backoff_wait):
    server.fail = {API + "refs": 2, API + f"paths-info/{SHA}": 1}
    c = cat(server, tmp_path)
    assert c.stat("hf://datasets/acme/wiki/README.md?at=branch:main").size == 6
    assert server.fail == {API + "refs": 0, API + f"paths-info/{SHA}": 0}


def test_real_hub_refuses_a_page_link_to_another_host(server, tmp_path):
    server.link_host = "127.0.0.2:1"
    with pytest.raises(StoreError, match="not on"):
        cat(server, tmp_path).list("hf://datasets/acme/wiki")


def test_xet_files_use_xet_and_fall_back_to_http(server, tmp_path, monkeypatch):
    from huggingface_hub import file_download as fd
    server.xet.add("data/b.bin")
    used = []

    def fake_xet_get(*, incomplete_path, xet_file_data, headers, expected_size, displayed_filename):
        used.append(xet_file_data.file_hash)
        incomplete_path.write_bytes(FILES["data/b.bin"])

    monkeypatch.setattr(fd, "xet_get", fake_xet_get)
    monkeypatch.setattr(fd, "is_xet_available", lambda: True)
    c = cat(server, tmp_path / "a")
    assert c.read("hf://datasets/acme/wiki/data/b.bin") == FILES["data/b.bin"]
    assert used == ["f" * 64]
    monkeypatch.setattr(fd, "is_xet_available", lambda: False)  # no hf_xet: plain HTTP
    c = cat(server, tmp_path / "b")
    assert c.read("hf://datasets/acme/wiki/data/b.bin") == FILES["data/b.bin"]
    assert used == ["f" * 64]


def test_the_token_does_not_go_to_the_download_host(server, tmp_path, monkeypatch):
    got = []

    class Cdn(BaseHTTPRequestHandler):
        def do_GET(self):
            got.append(self.headers.get("Authorization"))
            data = FILES[unquote(self.path).lstrip("/")]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    cdn = _serve(Cdn)
    try:
        server.cdn = f"http://localhost:{cdn.server_port}"  # another origin than 127.0.0.1
        monkeypatch.setenv("PROVCAT_TEST_HF_TOKEN", "hf_secret")
        assert cat(server, tmp_path).read("hf://datasets/acme/wiki/data/a.txt") == FILES["data/a.txt"]
        assert got == [None]
        assert "Bearer hf_secret" in server.auth  # the Hub itself did get it
    finally:
        cdn.shutdown()
