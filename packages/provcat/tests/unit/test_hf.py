import contextlib
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from provcat import Catalog, ConfigError, NotFound, OfflineMiss, StoreError, UriError
from provcat.stores import hf as hfmod
from provcat.stores.hf import Item

pytestmark = pytest.mark.unit

S1, S2, S3, TAG = "1" * 40, "2" * 40, "3" * 40, "4" * 40
REPO = "hf://datasets/acme/wiki"


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class FakeHub:
    """One dataset, acme/wiki. main has S1 (oldest), S2, S3 (newest); the
    branch dev is at S2 and the tag v1 at TAG. Each commit has its own files."""

    def __init__(self):
        self.branches = {"main": S3, "dev": S2}
        self.tags = {"v1": TAG}
        self.history = {"main": [(S3, datetime(2026, 3, 1, tzinfo=UTC)), (S2, datetime(2026, 2, 1, tzinfo=UTC)),
                                 (S1, datetime(2026, 1, 1, tzinfo=UTC))],
                        "dev": [(S2, datetime(2026, 2, 1, tzinfo=UTC)), (S1, datetime(2026, 1, 1, tzinfo=UTC))]}
        self.files = {sha: {"README.md": b"readme " + sha.encode(), "data/a.txt": b"a",
                            "data/sub/b.bin": b"big " * 100} for sha in (S1, S2, S3, TAG)}
        self.lfs = {"data/sub/b.bin"}  # paths that Xet or Git LFS keeps
        self.page_size = hfmod._PAGE
        self.calls: list[tuple] = []
        self.corrupt: set[str] = set()  # paths whose download gives other bytes

    @contextlib.contextmanager
    def errors(self, uri):
        yield

    def refs(self, kind, repo):
        self.calls.append(("refs", kind, repo))
        if (kind, repo) != ("dataset", "acme/wiki"):
            raise NotFound("no repo")
        return dict(self.branches), dict(self.tags)

    def commit_of(self, kind, repo, revision):
        self.calls.append(("commit_of", revision))
        for sha in self.files:
            if sha.startswith(revision):
                return sha
        if revision in self.branches:
            return self.branches[revision]
        raise NotFound("no revision")

    def commits(self, kind, repo, branch, page):
        self.calls.append(("commits", branch, page))
        h = self.history[branch]
        return h[page * self.page_size:(page + 1) * self.page_size]

    def _item(self, sha, path):
        data = self.files[sha][path]
        return Item(path, "file", len(data), hashlib.sha256(data).hexdigest() if path in self.lfs else None,
                    _blob(data))

    def info(self, kind, repo, commit, path):
        self.calls.append(("info", commit, path))
        files = self.files[commit]
        if path in files:
            return self._item(commit, path)
        if any(f.startswith(path + "/") for f in files):
            return Item(path, "folder")
        return None

    def tree(self, kind, repo, commit, path, recursive):
        self.calls.append(("tree", commit, path, recursive))
        prefix = path + "/" if path else ""
        out, folders = [], set()
        for f in sorted(self.files[commit]):
            if not f.startswith(prefix):
                continue
            rest = f[len(prefix):]
            if recursive or "/" not in rest:
                out.append(self._item(commit, f))
            else:
                folders.add(prefix + rest.split("/")[0])
        return out + [Item(p, "folder") for p in sorted(folders)]

    def download(self, kind, repo, commit, item, dest):
        path = item.path
        self.calls.append(("download", commit, path))
        data = self.files[commit][path]
        dest.write_bytes(data + b"!" if path in self.corrupt else data)


@pytest.fixture
def hub():
    return FakeHub()


def cat(tmp_path, hub, offline=False):
    c = Catalog({}, cache_root=tmp_path / "cache", offline=offline)
    c.stores["hf"].hub = hub
    return c


def sha_of(uri):
    return uri.selector.value


# pins

def test_pin_default_branch_tag_and_commit(tmp_path, hub):
    c = cat(tmp_path, hub)
    assert sha_of(c.pin(REPO)) == S3
    assert sha_of(c.pin(REPO + "/README.md?at=branch:dev")) == S2
    assert sha_of(c.pin(REPO + "?at=tag:v1")) == TAG
    hub.calls.clear()
    assert sha_of(c.pin(REPO + f"?at=commit:{S1}")) == S1
    assert hub.calls == []  # a full commit needs no call
    assert sha_of(c.pin(REPO + "?at=commit:2222222")) == S2


def test_branch_and_tag_are_not_mixed_up(tmp_path, hub):
    c = cat(tmp_path, hub)
    with pytest.raises(NotFound, match="no tag named main"):
        c.pin(REPO + "?at=tag:main")
    with pytest.raises(NotFound, match="no branch named v1"):
        c.pin(REPO + "?at=branch:v1")


def test_commit_selector_must_name_a_commit(tmp_path, hub):
    c = cat(tmp_path, hub)
    with pytest.raises(UriError, match="7 to 40 hex"):
        c.pin(REPO + "?at=commit:main")  # the Hub would take a branch here
    hub.branches["abcdef0"] = S1  # a branch whose name looks like a short commit
    with pytest.raises(NotFound, match="no commit abcdef0"):
        c.pin(REPO + "?at=commit:abcdef0")


def test_hub_answer_that_is_not_a_commit_is_refused(tmp_path, hub):
    hub.branches["main"] = "not-a-sha"
    with pytest.raises(StoreError, match="not a commit"):
        cat(tmp_path, hub).pin(REPO)


def test_ts_takes_newest_commit_at_or_before(tmp_path, hub):
    c = cat(tmp_path, hub)
    assert sha_of(c.pin(REPO + "?at=ts:2026-02-15")) == S2
    assert sha_of(c.pin(REPO + "?at=ts:2026-02-01T00:00:00Z")) == S2  # at, not only before
    assert sha_of(c.pin(REPO + "?at=ts:2026-04-01")) == S3
    assert sha_of(c.pin(REPO + "?at=ts:dev@2026-04-01")) == S2
    with pytest.raises(NotFound, match="no commit at or before"):
        c.pin(REPO + "?at=ts:2025-12-31")
    with pytest.raises(NotFound, match="no branch named nope"):
        c.pin(REPO + "?at=ts:nope@2026-04-01")


def test_ts_reads_page_by_page_and_stops_early(tmp_path, hub):
    hub.page_size = 1
    c = cat(tmp_path, hub)
    assert sha_of(c.pin(REPO + "?at=ts:2026-01-15")) == S1
    assert [x[2] for x in hub.calls if x[0] == "commits"] == [0, 1, 2]
    hub.calls.clear()
    c.pin(REPO + "?at=ts:2026-03-15")
    assert [x[2] for x in hub.calls if x[0] == "commits"] == [0]


def test_ts_search_has_a_limit(tmp_path, hub, monkeypatch):
    monkeypatch.setattr(hfmod, "_MAX_COMMITS", 2)
    hub.page_size = 1
    with pytest.raises(StoreError, match="read 2 commits"):
        cat(tmp_path, hub).pin(REPO + "?at=ts:2026-01-15")


def test_ts_in_the_future_is_refused_and_recent_is_not_kept(tmp_path, hub):
    c = cat(tmp_path, hub)
    with pytest.raises(UriError, match="future"):
        c.pin(REPO + f"?at=ts:{(datetime.now(UTC) + timedelta(days=1)).isoformat()}")
    recent = (datetime.now(UTC) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hub.history["main"].insert(0, (S3, datetime.now(UTC) - timedelta(minutes=1)))
    c.pin(REPO + f"?at=ts:{recent}")
    with pytest.raises(OfflineMiss):
        cat(tmp_path, hub, offline=True).pin(REPO + f"?at=ts:{recent}")


def test_offline_uses_the_pins_it_kept(tmp_path, hub):
    with pytest.raises(OfflineMiss, match="never pinned"):
        cat(tmp_path, hub, offline=True).pin(REPO)
    cat(tmp_path, hub).pin(REPO + "?at=branch:dev")
    hub.calls.clear()
    assert sha_of(cat(tmp_path, hub, offline=True).pin(REPO + "?at=branch:dev")) == S2
    assert hub.calls == []


def test_bad_uris(tmp_path, hub):
    c = cat(tmp_path, hub)
    for bad in ("hf://wiki/acme/x", "hf://datasets/acme", "hf://datasets", "hf://models/a/b?at=stage:x"):
        with pytest.raises(UriError):
            c.pin(bad)


# stat and list

def test_stat_file_folder_root_and_missing(tmp_path, hub):
    c = cat(tmp_path, hub)
    e = c.stat(REPO + "/data/sub/b.bin")
    assert (e.kind, e.size, e.version) == ("file", 400, S3)
    assert c.stat(REPO + "/data").kind == "folder"
    assert c.stat(REPO).kind == "folder"
    with pytest.raises(NotFound):
        c.stat(REPO + "/nope")


def test_list_gives_children(tmp_path, hub):
    c = cat(tmp_path, hub)
    got = {(str(e.uri), e.kind) for e in c.list(REPO + "/data?at=branch:dev")}
    assert got == {(f"{REPO}/data/a.txt?at=commit:{S2}", "file"), (f"{REPO}/data/sub?at=commit:{S2}", "folder")}
    with pytest.raises(NotFound, match="not a folder"):
        c.list(REPO + "/README.md")
    with pytest.raises(OfflineMiss):
        cat(tmp_path, hub, offline=True).list(REPO)


# fetch

def test_fetch_file_is_cached_by_commit(tmp_path, hub):
    c = cat(tmp_path, hub)
    p = c.fetch(REPO + "/README.md")
    assert p.read_bytes() == b"readme " + S3.encode() and S3 in p.parts
    hub.calls.clear()
    assert c.fetch(REPO + f"/README.md?at=commit:{S3}") == p
    assert hub.calls == []
    assert c.fetch(REPO + "/README.md?at=branch:dev").read_bytes() == b"readme " + S2.encode()


@pytest.mark.parametrize("path", ["README.md", "data/sub/b.bin"])  # checked by blob ID, and by SHA-256
def test_fetch_refuses_other_bytes_and_keeps_nothing(tmp_path, hub, path):
    hub.corrupt.add(path)
    c = cat(tmp_path, hub)
    with pytest.raises(StoreError, match="other content|bytes, not"):
        c.fetch(f"{REPO}/{path}")
    hub.corrupt.clear()
    assert c.fetch(f"{REPO}/{path}").read_bytes() == hub.files[S3][path]


def test_fetch_refuses_same_size_other_bytes(tmp_path, hub):
    hub.files[S3]["README.md"] = b"x" * 10
    real = hub.download

    def swap(kind, repo, commit, item, dest):
        real(kind, repo, commit, item, dest)
        dest.write_bytes(b"y" * 10)

    hub.download = swap
    with pytest.raises(StoreError, match="other content"):
        cat(tmp_path, hub).fetch(REPO + "/README.md")


def test_fetch_refuses_a_file_without_a_hash(tmp_path, hub):
    hub._item = lambda sha, path: Item(path, "file", len(hub.files[sha][path]))
    with pytest.raises(StoreError, match="no hash"):
        cat(tmp_path, hub).fetch(REPO + "/README.md")


def test_fetch_folder_whole_and_offline(tmp_path, hub):
    c = cat(tmp_path, hub)
    d = c.fetch(REPO + "/data")
    assert (d / "a.txt").read_bytes() == b"a" and (d / "sub" / "b.bin").read_bytes() == b"big " * 100
    root = c.fetch(REPO)
    assert (root / "README.md").is_file() and (root / "data" / "sub" / "b.bin").is_file()
    hub.calls.clear()
    off = cat(tmp_path, hub, offline=True)
    assert off.fetch(REPO + "/data/sub/b.bin").read_bytes() == b"big " * 100  # inside a fetched folder
    assert off.stat(REPO + "/data/sub").kind == "folder"
    assert off.stat(REPO + "/data/a.txt").size == 1
    assert hub.calls == []
    with pytest.raises(OfflineMiss):
        off.fetch(REPO + "/data?at=branch:dev")


def test_fetch_folder_with_a_bad_file_keeps_nothing(tmp_path, hub):
    hub.corrupt.add("data/sub/b.bin")
    c = cat(tmp_path, hub)
    with pytest.raises(StoreError):
        c.fetch(REPO + "/data")
    with pytest.raises(OfflineMiss):
        cat(tmp_path, hub, offline=True).fetch(REPO + "/data/a.txt")


def test_fetch_folder_refuses_twins_and_odd_paths(tmp_path, hub):
    hub.files[S3]["data/A.txt"] = b"A"
    with pytest.raises(StoreError, match="one name"):
        cat(tmp_path, hub).fetch(REPO + "/data")
    del hub.files[S3]["data/A.txt"]
    real = hub.tree
    hub.tree = lambda *a, **k: real(*a, **k) + [Item("data/../x", "file", 1, None, _blob(b"x"))]
    with pytest.raises(StoreError, match="not a path in the folder"):
        cat(tmp_path, hub).fetch(REPO + "/data")


def test_writes_are_refused(tmp_path, hub):
    src = tmp_path / "f"
    src.write_text("x")
    with pytest.raises(StoreError, match="cannot be written"):
        cat(tmp_path, hub).put(REPO + "/x", src)


# the real Hub class, without the network

def test_token_env(monkeypatch):
    made = []
    monkeypatch.setattr(hfmod, "Hub", lambda endpoint, token, timeout: made.append((endpoint, token)) or object())
    monkeypatch.delenv("MY_HF", raising=False)
    hfmod.HfStore("hf", {"token_env": ["MY_HF"]}).hub
    monkeypatch.setenv("MY_HF", "secret")
    hfmod.HfStore("hf", {"token_env": ["MY_HF"], "endpoint": "https://hub.example"}).hub
    hfmod.HfStore("hf", {}).hub
    assert made == [(None, False), ("https://hub.example", "secret"), (None, None)]


def _http_error(cls, code):
    import httpx2
    req = httpx2.Request("GET", "https://huggingface.co/api/x")
    return cls("boom", response=httpx2.Response(code, request=req))


def test_hub_errors_become_provcat_errors():
    from huggingface_hub import errors
    hub = hfmod.Hub(None, False)
    uri = "hf://datasets/a/b"
    cases = [(_http_error(errors.GatedRepoError, 403), StoreError, "gated"),
             (_http_error(errors.RepositoryNotFoundError, 404), NotFound, "not found"),
             (_http_error(errors.RevisionNotFoundError, 404), NotFound, "not found"),
             (_http_error(errors.HfHubHTTPError, 401), StoreError, "HF_TOKEN"),
             (_http_error(errors.HfHubHTTPError, 500), StoreError, "said 500"),
             (errors.HTTPError("connection refused"), StoreError, "cannot read"),
             (KeyError("id"), StoreError, "unexpected shape"),
             (RuntimeError("Task error: File reconstruction error"), StoreError, "cannot read"),  # hf_xet
             (errors.HFValidationError("bad repo id"), UriError, "not a Hugging Face repository name"),
             (errors.OfflineModeIsEnabled("offline"), StoreError, "HF_HUB_OFFLINE")]
    for exc, want, text in cases:
        with pytest.raises(want, match=text):
            with hub.errors(uri):
                raise exc


def test_missing_extra(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    with pytest.raises(StoreError, match=r"provcat\[hf\]"):
        hfmod.HfStore("hf", {"token_env": []}).hub


def test_logical_ts_keeps_the_branch(tmp_path, hub):
    cfg = {"schemes": {"data": {"parts": ["org", "repo", "env"], "rest": "path",
                                "target": "hf://datasets/{org}/{repo}/{path}", "ref": "{env}"}}}
    c = Catalog(cfg, cache_root=tmp_path / "cache")
    c.stores["hf"].hub = hub
    assert str(c.resolve("data://acme/wiki/dev/x?at=ts:2026-04-01").physical) == \
        "hf://datasets/acme/wiki/x?at=ts:dev@2026-04-01"
    assert sha_of(c.pin("data://acme/wiki/dev/README.md?at=ts:2026-04-01")) == S2


def test_stat_of_root_checks_a_full_commit(tmp_path, hub):
    c = cat(tmp_path, hub)
    with pytest.raises(NotFound):
        c.stat(REPO + "?at=commit:" + "a" * 40)  # pin asks nothing for a full commit
    assert c.stat(REPO + f"?at=commit:{S1}").kind == "folder"


def test_answer_for_another_path_is_refused(tmp_path, hub):
    real = hub.info
    hub.info = lambda kind, repo, commit, path: real(kind, repo, commit, "data/a.txt")
    with pytest.raises(StoreError, match="answered for 'data/a.txt'"):
        cat(tmp_path, hub).fetch(REPO + "/README.md")


def test_endpoint_must_use_https():
    with pytest.raises(ConfigError, match="HTTPS"):
        hfmod.Hub("http://hub.example", None)
    hfmod.Hub("http://127.0.0.1:9", None)  # the test servers
    hfmod.Hub("https://hub.example/", None)
    with pytest.raises(ConfigError, match="timeout"):
        hfmod.HfStore("hf", {"timeout": 0})


def test_bad_endpoints_timeouts_and_names_are_refused(tmp_path, hub):
    for bad in ("https://[::1", "https://huggingface.co:abc", "https://", "ftp://hub.example"):
        with pytest.raises(ConfigError):
            hfmod.Hub(bad, None)
    for bad in (float("inf"), float("nan"), -1):
        with pytest.raises(ConfigError, match="timeout"):
            hfmod.HfStore("hf", {"timeout": bad})
    for bad in ("hf://models/openai-community%23/gpt2", "hf://models/a/b%3Fx", "hf://models/a%20b/c"):
        with pytest.raises(UriError, match="letters, digits"):
            cat(tmp_path, hub).pin(bad)


def test_a_rust_panic_in_xet_becomes_a_store_error():
    PanicException = type("PanicException", (BaseException,), {})
    with pytest.raises(StoreError, match="Xet download failed"):
        with hfmod.Hub(None, False).errors("hf://models/a/b"):
            raise PanicException("thread panicked")
    with pytest.raises(KeyboardInterrupt):
        with hfmod.Hub(None, False).errors("hf://models/a/b"):
            raise KeyboardInterrupt
