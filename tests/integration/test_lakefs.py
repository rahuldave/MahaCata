"""The lakefs driver against moto, for the data, and a fake lakeFS REST API,
for the commit logs."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
import pytest

from provcat import Catalog, NotFound, OfflineMiss, UriError
from provcat.errors import ProvcatError, StoreError

C1, C2, C0 = "c1" * 32, "c2" * 32, "c0" * 32
T1, T2 = 1_700_000_000, 1_700_000_100  # the creation times of C1 and C2
# Branch merged: M at 5, a feature commit F at 10 on another branch, merged at 20.
M, F, MERGE = "aa" * 32, "ff" * 32, "ee" * 32


class FakeLakefs:
    """The commit-log endpoint of the lakeFS API for one repository, "repo".
    Branch main has C2 then C1; branch empty has only C0."""

    LOGS = {"main": [(C2, T2), (C1, T1)], "empty": [(C0, T1 - 50)], C1: [(C1, T1)], C2: [(C2, T2), (C1, T1)],
            "merged": [(MERGE, T1 + 20), (F, T1 + 10), (M, T1 + 5)]}
    FIRST_PARENT = {"merged": [(MERGE, T1 + 20), (M, T1 + 5)]}

    def __init__(self):
        self.auth = []
        self.queries = []
        self.redirect_to = None
        self.fail_with = None
        self.fail_message = "broken"
        self.branches = {"main", "empty", "merged"}
        self.posts = []  # (path, body) of each POST
        self.commit_reply = (201, {"id": "NEWCOMMIT"})
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.auth.append(self.headers.get("Authorization"))
                if fake.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect_to)
                    self.end_headers()
                    return
                if fake.fail_with:
                    return self.reply(fake.fail_with, {"message": fake.fail_message})
                url = urlsplit(self.path)
                parts = [unquote(p) for p in url.path.split("/")]
                q = parse_qs(url.query)
                fake.queries.append(q)
                if len(parts) == 5 and parts[3] == "repositories":
                    return self.reply(200, {"id": "repo"}) if parts[4] == "repo" else self.reply(404, {})
                if len(parts) == 7 and parts[5] == "branches" and parts[4] == "repo":
                    return self.reply(200, {"id": parts[6]}) if parts[6] in fake.branches else self.reply(404, {})
                if len(parts) == 6 and parts[5] == "branches" and parts[4] == "repo":
                    return self.reply(200, {"results": [{"id": "empty"}, {"id": "main"}],
                                            "pagination": {"has_more": False}})
                # /api/v1/repositories/<repo>/refs/<ref>/commits
                if len(parts) != 8 or parts[3] != "repositories" or parts[5] != "refs" or parts[7] != "commits":
                    return self.reply(404, {"message": "not found"})
                repo, ref = parts[4], parts[6]
                if repo != "repo" or ref not in fake.LOGS:
                    return self.reply(404, {"message": f"{ref}: not found"})
                amount, start = int(q.get("amount", ["100"])[0]), int(q.get("after", ["0"])[0] or 0)
                log = fake.LOGS[ref]
                if q.get("first_parent") == ["true"]:
                    log = fake.FIRST_PARENT.get(ref, log)
                page = log[start:start + amount]
                more = start + amount < len(log)
                self.reply(200, {"results": [{"id": c, "creation_date": t} for c, t in page],
                                 "pagination": {"has_more": more, "next_offset": str(start + amount) if more else ""}})

            def do_POST(self):
                fake.auth.append(self.headers.get("Authorization"))
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                parts = [unquote(p) for p in urlsplit(self.path).path.split("/")]
                fake.posts.append(("/".join(parts), body))
                if parts[3:5] != ["repositories", "repo"]:
                    return self.reply(404, {"message": "repository not found"})
                # /api/v1/repositories/repo/branches/<b>/commits
                if len(parts) == 8 and parts[5] == "branches" and parts[7] == "commits":
                    if parts[6] not in fake.branches:
                        return self.reply(404, {"message": "branch not found"})
                    return self.reply(*fake.commit_reply)
                # /api/v1/repositories/repo/branches
                if len(parts) == 6 and parts[5] == "branches":
                    if body["name"] in fake.branches:
                        return self.reply(409, {"message": "branch already exists"})
                    if body["source"] not in fake.LOGS:
                        return self.reply(404, {"message": "source not found"})
                    fake.branches.add(body["name"])
                    data = body["source"].encode()  # lakeFS answers with the ref, as text
                    self.send_response(201)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self.reply(404, {"message": "not found"})

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}/api/v1"


@pytest.fixture
def api():
    fake = FakeLakefs()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def data(s3_endpoint):
    s3 = boto3.client("s3", endpoint_url=s3_endpoint, region_name="us-east-1")
    s3.create_bucket(Bucket="repo")
    for ref, body in (("main", b"head"), (C1, b"first"), (C2, b"second")):
        s3.put_object(Bucket="repo", Key=f"{ref}/t/a.csv", Body=body)
    return s3


def cat(s3_endpoint, api, tmp_path, offline=False, schemes=None):
    cfg = {"stores": {"lakefs": {"driver": "lakefs", "endpoint_url": s3_endpoint, "api_url": api.api_url}}}
    if schemes:
        cfg["schemes"] = schemes
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


def test_pin_a_branch_to_its_commit(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path)
    assert str(c.pin("lakefs://repo/main/t/a.csv")) == f"lakefs://repo/{C2}/t/a.csv"
    assert c.read(str(c.pin("lakefs://repo/main/t/a.csv"))) == b"second"
    # The API gets the same keys as the gateway, as basic auth.
    assert api.auth[0] == "Basic " + base64.b64encode(b"test:test").decode()


@pytest.mark.parametrize("ts, body", [
    (str(T1), b"first"),                 # exactly at C1
    (str(T1 + 99), b"first"),            # between C1 and C2
    (str(T2), b"second"),                # exactly at C2
    ("2025-01-01", b"second"),           # after both
])
def test_ts_reads_the_commit_of_that_time(s3_endpoint, api, data, tmp_path, ts, body):
    c = cat(s3_endpoint, api, tmp_path)
    uri = f"lakefs://repo/main/t/a.csv?at=ts:{ts}"
    assert c.read(uri) == body
    assert c.stat(uri).kind == "file"


def test_ts_pages_through_a_long_log(s3_endpoint, api, data, tmp_path, monkeypatch):
    import provcat.stores.lakefs as lk

    monkeypatch.setattr(lk, "_PAGE", 1)  # one commit per page
    assert cat(s3_endpoint, api, tmp_path).read(f"lakefs://repo/main/t/a.csv?at=ts:{T1 + 1}") == b"first"


def test_ts_before_the_first_commit_or_in_the_future(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path)
    with pytest.raises(NotFound, match="no commit at or before"):
        c.read(f"lakefs://repo/main/t/a.csv?at=ts:{T1 - 1}")
    with pytest.raises(UriError, match="in the future"):
        c.read("lakefs://repo/main/t/a.csv?at=ts:2999-01-01")


def test_other_selectors_are_refused(s3_endpoint, api, data, tmp_path):
    with pytest.raises(UriError, match="only selector is"):
        cat(s3_endpoint, api, tmp_path).read("lakefs://repo/main/t/a.csv?at=branch:dev")


def test_empty_and_missing_branches(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path)
    assert c.list("lakefs://repo/empty") == []
    assert c.stat("lakefs://repo/empty").kind == "folder"
    with pytest.raises(NotFound, match="has no branch, tag or commit 'nosuch'"):
        c.list("lakefs://repo/nosuch")
    with pytest.raises(NotFound, match="has no branch, tag or commit 'nosuch'"):
        c.stat("lakefs://repo/nosuch")
    with pytest.raises(NotFound, match="has no branch, tag or commit 'nosuch'"):
        c.pin("lakefs://repo/nosuch/t/a.csv")


def test_offline_uses_the_last_pin(s3_endpoint, api, data, tmp_path):
    online = cat(s3_endpoint, api, tmp_path)
    online.fetch(f"lakefs://repo/main/t/a.csv?at=ts:{T1 + 1}")
    online.pin("lakefs://repo/main/t/a.csv")
    off = cat(s3_endpoint, api, tmp_path, offline=True)
    assert off.read(f"lakefs://repo/main/t/a.csv?at=ts:{T1 + 1}") == b"first"
    assert str(off.pin("lakefs://repo/main/t/a.csv")) == f"lakefs://repo/{C2}/t/a.csv"
    with pytest.raises(OfflineMiss, match="never pinned"):
        off.read(f"lakefs://repo/main/t/a.csv?at=ts:{T2}")


def test_a_logical_uri_with_ts(s3_endpoint, api, data, tmp_path):
    schemes = {"data+table": {"parts": ["org", "env", "name"], "target": "lakefs://{org}/{ref}/t/{name}",
                              "ref": "{env}"}}
    c = cat(s3_endpoint, api, tmp_path, schemes=schemes)
    assert c.read(f"data+table://repo/main/a.csv?at=ts:{T1 + 1}") == b"first"
    assert c.read("data+table://repo/main/a.csv") == b"head"  # the branch, with what is not committed



def test_ts_follows_the_branch_not_merged_commits(s3_endpoint, api, data, tmp_path):
    # Review of the lakefs driver, finding 1: at T1+15 the branch held M, not
    # the feature commit F, which came in by the merge at T1+20.
    c = cat(s3_endpoint, api, tmp_path)
    assert str(c.pin(f"lakefs://repo/merged/x?at=ts:{T1 + 15}")) == f"lakefs://repo/{M}/x"
    assert all(q.get("first_parent") == ["true"] for q in api.queries if "amount" in q)


def test_errors_on_a_ref_root_are_not_hidden(s3_endpoint, api, data, tmp_path, monkeypatch):
    # Finding 2: only "nothing there" makes an empty branch.
    from provcat.errors import StoreError
    from provcat.stores.s3 import S3Store

    c = cat(s3_endpoint, api, tmp_path)
    real = S3Store._call

    def broken(self, uri, method, **kw):
        if method == "list_objects_v2":
            raise StoreError(f"{uri}: S3 error 503: SlowDown")
        return real(self, uri, method, **kw)

    monkeypatch.setattr(S3Store, "_call", broken)
    with pytest.raises(StoreError, match="503"):
        c.list("lakefs://repo/main")


def test_the_keys_never_follow_a_redirect(s3_endpoint, api, data, tmp_path):
    # Finding 3: a redirect is an error, and the second host gets nothing.
    from provcat.errors import StoreError

    other = FakeLakefs()
    try:
        api.redirect_to = other.api_url + "/repositories/repo/refs/main/commits"
        with pytest.raises(StoreError, match="answered with a redirect"):
            cat(s3_endpoint, api, tmp_path).pin("lakefs://repo/main/t/a.csv")
        assert other.auth == []
    finally:
        other.server.shutdown()


def test_a_recent_time_is_not_remembered(s3_endpoint, api, data, tmp_path, monkeypatch):
    # Finding 4: a time close to now can still gain a commit, so --offline
    # must not keep its answer.
    import provcat.stores.lakefs as lk

    monkeypatch.setattr(lk, "_SETTLE", 10 ** 10)  # every time counts as recent
    cat(s3_endpoint, api, tmp_path).pin(f"lakefs://repo/main/t/a.csv?at=ts:{T2}")
    with pytest.raises(OfflineMiss, match="never pinned"):
        cat(s3_endpoint, api, tmp_path, offline=True).pin(f"lakefs://repo/main/t/a.csv?at=ts:{T2}")


def test_a_missing_repository_is_named(s3_endpoint, api, data, tmp_path):
    # Finding 5a.
    with pytest.raises(NotFound, match="has no repository 'other'"):
        cat(s3_endpoint, api, tmp_path).pin("lakefs://other/main/x")


def test_a_commit_without_a_date_is_an_error(s3_endpoint, api, data, tmp_path, monkeypatch):
    # Finding 5b: a missing date is not the epoch.
    from provcat.errors import StoreError

    monkeypatch.setitem(FakeLakefs.LOGS, "nodate", [(C2, None)])
    with pytest.raises(StoreError, match="no creation date"):
        cat(s3_endpoint, api, tmp_path).pin(f"lakefs://repo/nodate/x?at=ts:{T2}")


def test_the_search_limit_says_so(s3_endpoint, api, data, tmp_path, monkeypatch):
    # Finding 5c.
    import provcat.stores.lakefs as lk
    from provcat.errors import StoreError

    monkeypatch.setattr(lk, "_MAX_COMMITS", 1)
    with pytest.raises(StoreError, match="searched 1 commits"):
        cat(s3_endpoint, api, tmp_path).pin(f"lakefs://repo/main/x?at=ts:{T1}")


def test_the_repository_root_lists_branches(s3_endpoint, api, data, tmp_path):
    # Finding 5e: lakefs://repo lists the branches, as the s3 driver did.
    c = cat(s3_endpoint, api, tmp_path)
    assert [str(e.uri) for e in c.list("lakefs://repo")] == ["lakefs://repo/empty", "lakefs://repo/main"]
    assert c.stat("lakefs://repo").kind == "folder"
    with pytest.raises(NotFound, match="has no repository 'other'"):
        c.list("lakefs://other")


def _gateway_403(monkeypatch, code="403"):
    """The S3 gateway of lakeFS with access control: 403 for every read."""
    from botocore.exceptions import ClientError

    from provcat.errors import StoreError
    from provcat.stores.s3 import S3Store

    def call(self, uri, method, **kw):
        error = ClientError({"Error": {"Code": code, "Message": "Forbidden"}}, method)
        raise StoreError(f"{uri}: S3 error 403: {error}") from error

    monkeypatch.setattr(S3Store, "_call", call)


@pytest.mark.parametrize("code", ["403", "AccessDenied"])
def test_a_403_for_a_missing_repository_is_not_found(s3_endpoint, api, tmp_path, monkeypatch, code):
    # With access control, the gateway answers a missing repository with 403;
    # the API tells that it is missing.
    _gateway_403(monkeypatch, code)
    with pytest.raises(NotFound, match="has no repository 'other'"):
        cat(s3_endpoint, api, tmp_path).read("lakefs://other/main/t/a.csv")


@pytest.mark.parametrize("api_code", [None, 401])
def test_a_403_for_a_repository_that_exists_stands(s3_endpoint, api, tmp_path, monkeypatch, api_code):
    # The repository exists, or the API refuses too: the keys may not read it.
    from provcat.errors import StoreError

    _gateway_403(monkeypatch)
    api.fail_with = api_code
    with pytest.raises(StoreError, match="S3 error 403") as e:
        cat(s3_endpoint, api, tmp_path).read("lakefs://repo/main/t/a.csv")
    assert not isinstance(e.value, NotFound)


def test_a_403_offline_asks_no_api(s3_endpoint, api, tmp_path, monkeypatch):
    # Offline, the 403 stands, and nothing goes to the API.
    from provcat.errors import StoreError
    from provcat.uri import parse

    _gateway_403(monkeypatch)
    store = cat(s3_endpoint, api, tmp_path, offline=True).stores["lakefs"]
    with pytest.raises(StoreError, match="S3 error 403") as e:
        store._call(parse("lakefs://other/main/t/a.csv"), "head_object", Bucket="other", Key="main/t/a.csv")
    assert not isinstance(e.value, NotFound)
    assert api.auth == []


def test_api_errors_name_the_api(s3_endpoint, api, data, tmp_path):
    from provcat.errors import StoreError

    api.fail_with = 500
    with pytest.raises(StoreError, match="lakeFS API error 500"):
        cat(s3_endpoint, api, tmp_path).pin("lakefs://repo/main/x")


@pytest.mark.parametrize("message, want", [
    # lakeFS with access control knows the keys, but a policy refuses the action.
    ("insufficient permissions: not allowed to fs:ReadRepository",
     r"does not allow these keys to do this \(401\): insufficient permissions: not allowed to fs:ReadRepository"),
    # Keys that lakeFS does not know.
    ("error authenticating request", r"refused the keys \(401\)"),
])
def test_a_refusal_says_whether_the_keys_or_the_policy_failed(s3_endpoint, api, data, tmp_path, message, want):
    from provcat.errors import StoreError

    api.fail_with, api.fail_message = 401, message
    with pytest.raises(StoreError, match=want):
        cat(s3_endpoint, api, tmp_path).pin("lakefs://repo/main/x")


def test_each_org_reads_with_its_own_keys(s3_endpoint, api, data, tmp_path, monkeypatch):
    # Keys for each tenant: lakefs-{org} picks a store, and each store has its
    # own profile, so the API and the gateway see that org's keys only.
    creds = tmp_path / "credentials"
    creds.write_text("[acme]\naws_access_key_id = ACMEKEY\naws_secret_access_key = acmesecret\n"
                     "[globex]\naws_access_key_id = GLOBEXKEY\naws_secret_access_key = globexsecret\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    store = {"driver": "lakefs", "endpoint_url": s3_endpoint, "api_url": api.api_url}
    cfg = {"stores": {"lakefs-acme": {**store, "profile": "acme"}, "lakefs-globex": {**store, "profile": "globex"}},
           "schemes": {"t": {"parts": ["org", "env", "name"], "target": "lakefs-{org}://repo/{ref}/t/{name}",
                             "ref": "{env}", "match": {"org": "[a-z0-9]{1,20}"}}}}
    c = Catalog(cfg, cache_root=tmp_path / "cache")

    def key(header):
        return base64.b64decode(header.split()[1]).decode().split(":")[0]

    api.auth.clear()
    assert c.read(f"t://acme/main/a.csv?at=ts:{T1 + 1}") == b"first"
    assert {key(h) for h in api.auth} == {"ACMEKEY"}
    api.auth.clear()
    assert c.read(f"t://globex/main/a.csv?at=ts:{T1 + 1}") == b"first"
    assert {key(h) for h in api.auth} == {"GLOBEXKEY"}
    api.auth.clear()
    with pytest.raises(Exception, match="no store named 'lakefs-initech'"):
        c.read("t://initech/main/a.csv")
    assert api.auth == []  # refused before any request


# Writes


def test_put_writes_to_a_branch_and_commit_pins_it(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path)
    [entry] = c.put("lakefs://repo/main/t/new.csv", b"N")
    assert str(entry.uri) == "lakefs://repo/main/t/new.csv"
    assert c.read("lakefs://repo/main/t/new.csv") == b"N"
    pinned = c.commit("lakefs://repo/main/t/new.csv", "add new.csv", {"run": "42"})
    assert str(pinned) == "lakefs://repo/NEWCOMMIT/t/new.csv"
    path, body = api.posts[-1]
    assert path.endswith("/repositories/repo/branches/main/commits")
    assert body == {"message": "add new.csv", "metadata": {"run": "42"}, "allow_empty": False}
    c.delete("lakefs://repo/main/t/new.csv")
    with pytest.raises(NotFound):
        c.stat("lakefs://repo/main/t/new.csv")


@pytest.mark.parametrize("uri, match", [
    (f"lakefs://repo/{C1}/t/x.csv", "is not a branch of repo"),
    ("lakefs://nosuch/main/t/x.csv", "has no repository 'nosuch'"),
    ("lakefs://repo/main/t/x.csv?at=ts:2026-01-01", "write to a branch"),
    ("lakefs://repo/main", "name an object"),
])
def test_writes_go_only_to_an_object_on_a_branch(s3_endpoint, api, data, tmp_path, uri, match):
    with pytest.raises(ProvcatError, match=match):
        cat(s3_endpoint, api, tmp_path).put(uri, b"x")


def test_commit_with_nothing_to_commit_says_so(s3_endpoint, api, data, tmp_path):
    api.commit_reply = (400, {"message": "commit: no changes"})
    with pytest.raises(StoreError, match="nothing to commit on main"):
        cat(s3_endpoint, api, tmp_path).commit("lakefs://repo/main", "nothing")


def test_other_commit_errors_are_not_called_empty(s3_endpoint, api, data, tmp_path):
    # Finding 7: "no changes" anywhere in an error read as an empty commit.
    api.commit_reply = (500, {"message": "storage failed; no changes were saved"})
    with pytest.raises(StoreError, match="API error 500"):
        cat(s3_endpoint, api, tmp_path).commit("lakefs://repo/main", "m")


def test_commit_needs_a_message(s3_endpoint, api, data, tmp_path):
    with pytest.raises(UriError, match="needs a message"):
        cat(s3_endpoint, api, tmp_path).commit("lakefs://repo/main", "")


def test_branch_makes_a_branch_from_a_ref(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path)
    assert str(c.branch("lakefs://repo/feature", "main")) == "lakefs://repo/feature"
    assert api.posts[-1][1] == {"name": "feature", "source": "main"}
    with pytest.raises(StoreError, match="conflicts with what is there"):
        c.branch("lakefs://repo/feature", "main")
    with pytest.raises(NotFound, match="no branch, tag or commit 'nosuch'"):
        c.branch("lakefs://repo/other", "nosuch")
    with pytest.raises(UriError):
        c.branch("lakefs://repo/feature/extra", "main")


SCHEMES = {"t": {"parts": ["env"], "rest": "path", "target": "lakefs://repo/{ref}/{path}", "ref": "{env}"}}


def test_logical_writes_go_to_the_default_or_a_named_branch(s3_endpoint, api, data, tmp_path):
    c = cat(s3_endpoint, api, tmp_path, schemes=SCHEMES)
    api.branches.add("dev")
    data.put_object(Bucket="repo", Key="dev/placeholder", Body=b"")
    [entry] = c.put("t://main/t/l.csv", b"L")
    assert str(entry.uri) == "lakefs://repo/main/t/l.csv"
    [entry] = c.put("t://main/t/l.csv?at=branch:dev", b"D")
    assert str(entry.uri) == "lakefs://repo/dev/t/l.csv"
    pinned = c.commit("t://main/t", "logical commit")
    assert str(pinned) == "lakefs://repo/NEWCOMMIT/t"
    assert api.posts[-1][1]["metadata"] == {"provcat.uri": "t://main/t"}


@pytest.mark.parametrize("selector", [f"commit:{C1}", "tag:v1", "ts:2026-01-01"])
def test_logical_writes_to_a_fixed_version_are_refused(s3_endpoint, api, data, tmp_path, selector):
    c = cat(s3_endpoint, api, tmp_path, schemes=SCHEMES)
    with pytest.raises(UriError, match="names a fixed version"):
        c.put(f"t://main/t/l.csv?at={selector}", b"x")


def test_stores_without_commits_say_so(tmp_path):
    from provcat import Catalog

    c = Catalog({}, cache_root=tmp_path / "cache")
    with pytest.raises(StoreError, match="has no commits"):
        c.commit(f"file://{tmp_path}/x", "m")
    with pytest.raises(StoreError, match="has no branches"):
        c.branch(f"file://{tmp_path}/x", "main")
