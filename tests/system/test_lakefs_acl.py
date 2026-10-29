"""Tenant isolation on one lakeFS, with the reference access control.

scripts/lakefs_acl_local.py runs lakeFS 1.86.0 with the access-list server,
and gives each org a policy for the repositories that start with the org. This
test checks that each org's keys work on its own repositories and nowhere
else, through the REST API, the S3 gateway and provcat.

- PROVCAT_SYSTEM_LAKEFS_ACL=<url>: that lakeFS, for example http://127.0.0.1:8002.
- PROVCAT_SYSTEM_LAKEFS_ACL_CREDENTIALS=<file>: the credentials file the script
  writes, with the profiles lakefs-acme and lakefs-globex.
- PROVCAT_SYSTEM_LAKEFS_ACL_DATA=<folder>: the blockstore path, the data folder
  the script makes, so cleanup deletes the storage folders too.
"""
import os
import urllib.error
import uuid

import botocore.exceptions
import pytest

from object_store_suite import LakefsBackend, cleanup_all
from provcat import Catalog, NotFound
from provcat.errors import StoreError


def _need(var):
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"set {var} to run this against lakeFS with access control")
    return value


@pytest.fixture
def orgs(monkeypatch):
    url = _need("PROVCAT_SYSTEM_LAKEFS_ACL")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", _need("PROVCAT_SYSTEM_LAKEFS_ACL_CREDENTIALS"))
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    data = os.environ.get("PROVCAT_SYSTEM_LAKEFS_ACL_DATA")
    made = {}
    try:
        for org in ("acme", "globex"):
            made[org] = LakefsBackend(url, f"lakefs-{org}", data, prefix=f"{org}-provcat-test-")
        yield made
    finally:
        cleanup_all(*made.values())


def _refused(call):
    with pytest.raises(urllib.error.HTTPError) as e:
        call()
    assert e.value.code in (401, 403)
    return e.value.read().decode()


def test_one_lakefs_keeps_each_org_to_its_own_repositories(orgs, tmp_path):
    acme, globex = orgs["acme"], orgs["globex"]
    tag = uuid.uuid4().hex[:12]
    acme_repo, globex_repo = f"acme-provcat-test-{tag}", f"globex-provcat-test-{tag}"
    acme.make_bucket(acme_repo)
    globex.make_bucket(globex_repo)
    acme.put(acme_repo, "main/a.csv", b"acme data")
    globex.put(globex_repo, "main/b.csv", b"globex data")
    acme.commit(acme_repo, "main", "acme")
    globex.commit(globex_repo, "main", "globex")

    # An org may not make a repository under the other org's name, nor on the
    # other org's storage.
    stolen = f"globex-provcat-test-{tag}x"
    globex.created.append(stolen)  # globex may delete it, should the refusal ever fail
    assert "fs:CreateRepository" in _refused(lambda: acme._api(
        "POST", "/repositories", {"name": stolen, "storage_namespace": f"local://{stolen}",
                                  "default_branch": "main"}))
    acme.created.append(f"acme-provcat-test-{tag}x")
    assert "fs:AttachStorageNamespace" in _refused(lambda: acme._api(
        "POST", "/repositories", {"name": f"acme-provcat-test-{tag}x",
                                  "storage_namespace": f"local://{globex_repo}", "default_branch": "main"}))

    # Each org lists only its own repositories, through the API and the gateway.
    for org, mine, theirs in ((acme, acme_repo, globex_repo), (globex, globex_repo, acme_repo)):
        listed = [r["id"] for r in org._api("GET", "/repositories?amount=1000")["results"]]
        assert mine in listed and theirs not in listed
        assert all(name.startswith(mine.split("-")[0] + "-") for name in listed)
        buckets = [b["Name"] for b in org.client.list_buckets()["Buckets"]]
        assert mine in buckets and theirs not in buckets
        assert "fs:ReadRepository" in _refused(lambda: org._api("GET", f"/repositories/{theirs}"))
        for call in (lambda: org.client.get_object(Bucket=theirs, Key="main/a.csv"),
                     lambda: org.client.list_objects_v2(Bucket=theirs, Prefix="main/"),
                     lambda: org.client.put_object(Bucket=theirs, Key="main/x", Body=b"x")):
            with pytest.raises(botocore.exceptions.ClientError) as e:
                call()
            assert e.value.response["Error"]["Code"] in ("AccessDenied", "403")

    # Through provcat: each org's store reads, pins and lists its own data, and
    # is refused the other's, with a message that names the refused action.
    def store(profile):
        return {"driver": "lakefs", "endpoint_url": acme.endpoint, "profile": profile}

    c = Catalog({
        "stores": {"lakefs-acme": store("lakefs-acme"), "lakefs-globex": store("lakefs-globex")},
        "schemes": {"t": {"parts": ["org", "repo", "env"], "rest": "path",
                          "target": "lakefs-{org}://{repo}/{ref}/{path}", "ref": "{env}",
                          "match": {"org": "[a-z0-9]{1,20}"}}},
    }, cache_root=tmp_path / "cache")
    assert c.read(f"t://acme/{acme_repo}/main/a.csv") == b"acme data"
    assert c.read(f"t://globex/{globex_repo}/main/b.csv") == b"globex data"
    assert c.read(str(c.pin(f"t://acme/{acme_repo}/main/a.csv"))) == b"acme data"
    assert [str(e.uri) for e in c.list(f"lakefs-acme://{acme_repo}")] == [f"lakefs-acme://{acme_repo}/main"]
    with pytest.raises(StoreError, match="S3 error 403"):
        c.read(f"t://acme/{globex_repo}/main/b.csv")
    with pytest.raises(StoreError, match="does not allow these keys .*fs:Read"):
        c.pin(f"t://acme/{globex_repo}/main/b.csv")
    with pytest.raises(StoreError, match="does not allow these keys"):
        c.list(f"lakefs-globex://{acme_repo}")
    with pytest.raises(NotFound):
        c.read(f"t://acme/acme-provcat-test-{tag}none/main/a.csv")


def test_an_org_cannot_link_the_storage_of_another(orgs):
    """Review of the reference setup, findings 1 and 2. Stock lakeFS 1.86.0
    lets a user who may write to its own repository stage any address on the
    same storage, so acme could read globex's objects by address. The patched
    build refuses an address outside the repository's storage without
    fs:ImportFromStorage, and refuses "." and ".." segments."""
    acme, globex = orgs["acme"], orgs["globex"]
    tag = uuid.uuid4().hex[:12]
    acme_repo, globex_repo = f"acme-provcat-test-{tag}", f"globex-provcat-test-{tag}"
    acme.make_bucket(acme_repo)
    globex.make_bucket(globex_repo)
    globex.put(globex_repo, "main/secret", b"globex secret")
    # The address stands in for one that acme guessed; the addresses are sequential.
    target = globex._api("GET", f"/repositories/{globex_repo}/refs/main/objects/stat?path=secret")["physical_address"]
    relative = target[target.index("/data/") + 1:]
    sneaky = {
        "full address": target,
        "dots from its own data": f"local://{acme_repo}/data/../../{globex_repo}/{relative}",
        "encoded dots": f"local://{acme_repo}/data/%2e%2e/%2e%2e/{globex_repo}/{relative}",
    }
    branch = f"/repositories/{acme_repo}/branches/main"
    for name, address in sneaky.items():
        key = name.replace(" ", "-")
        calls = {  # LinkPhysicalAddress, and the older StageObject
            "link": (f"{branch}/staging/backing?path={key}",
                     {"staging": {"physical_address": address}, "checksum": "x", "size_bytes": 13}),
            "stage": (f"{branch}/objects?path={key}",
                      {"physical_address": address, "checksum": "x", "size_bytes": 13}),
        }
        for call, (path, body) in calls.items():
            with pytest.raises(urllib.error.HTTPError) as e:
                acme._api("PUT", path, body)
            assert e.value.code in (400, 401), (name, call, e.value.code)
    # Nothing was staged, so acme has nothing to read.
    listed = acme._api("GET", f"/repositories/{acme_repo}/refs/main/objects/ls")["results"]
    assert listed == []

    # Storage namespaces that match namespace/local://acme-* but name globex's
    # storage: through "..", or through "@", which the commit cache reads as
    # the host (review 4, finding F1).
    for n, namespace in enumerate((f"local://acme-x/../{globex_repo}", f"local://acme-x@{globex_repo}/repo")):
        for bare in ("false", "true"):
            name = f"acme-provcat-test-{tag}{bare[0]}{n}"
            acme.created.append(name)
            with pytest.raises(urllib.error.HTTPError) as e:
                acme._api("POST", f"/repositories?bare={bare}",
                          {"name": name, "storage_namespace": namespace, "default_branch": "main"})
            assert e.value.code == 400 and "segments" in e.value.read().decode(), namespace


def test_an_org_cannot_commit_a_metarange_outside_its_storage(orgs):
    """Review of the patch, finding A1. A commit may name its metarange, and
    stock lakeFS 1.86.0 opened that name in its cache without a check, so
    "../" reached any file on the server, another org's metaranges among them.
    The patched build refuses "." and ".." in range and metarange names."""
    acme, globex = orgs["acme"], orgs["globex"]
    tag = uuid.uuid4().hex[:12]
    acme_repo, globex_repo = f"acme-provcat-test-{tag}", f"globex-provcat-test-{tag}"
    acme.make_bucket(acme_repo)
    globex.make_bucket(globex_repo)
    globex.put(globex_repo, "main/secret", b"globex secret")
    globex.commit(globex_repo, "main", "globex")
    theirs = globex._api("GET", f"/repositories/{globex_repo}/refs/main/commits?amount=1")["results"][0]["meta_range_id"]
    acme.put(acme_repo, "main/a.csv", b"acme data")
    acme.commit(acme_repo, "main", "acme")
    for sneaky in ("../" * 12 + "etc/hosts", f"../{globex_repo}/{theirs}", f"..%2F{globex_repo}%2F{theirs}"):
        with pytest.raises(urllib.error.HTTPError) as e:
            acme._api("POST", f"/repositories/{acme_repo}/branches/main/commits?source_metarange={sneaky}",
                      {"message": "sneaky"})
        assert e.value.code == 400, sneaky  # a refusal, not a server error (review 4, finding F3)
    # A real ID of globex's, which acme's storage does not hold (review 4, finding F2).
    with pytest.raises(urllib.error.HTTPError) as e:
        acme._api("POST", f"/repositories/{acme_repo}/branches/main/commits?source_metarange={theirs}",
                  {"message": "sneaky"})
    assert e.value.code == 404
    # The branch still works, and still holds only acme's data.
    listed = acme._api("GET", f"/repositories/{acme_repo}/refs/main/objects/ls")["results"]
    assert [o["path"] for o in listed] == ["a.csv"]
