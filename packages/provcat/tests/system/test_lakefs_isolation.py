"""Tenant isolation with one lakeFS server for each org.

Open-source lakeFS has no access control, so one server cannot keep two orgs
apart. Two servers can: each org's store points at its own lakeFS, with its own
keys, and each server refuses the other server's keys.

- PROVCAT_SYSTEM_LAKEFS=<url> and PROVCAT_SYSTEM_LAKEFS_B=<url>: the two servers.
- PROVCAT_SYSTEM_LAKEFS_PROFILE (default lakefs) and PROVCAT_SYSTEM_LAKEFS_B_PROFILE
  (default lakefs-b): the AWS profiles with each server's keys.
- PROVCAT_SYSTEM_LAKEFS_DATA and PROVCAT_SYSTEM_LAKEFS_B_DATA: the blockstore
  paths, so cleanup deletes the storage folders too.
"""
import os
import uuid

import pytest

from object_store_suite import LakefsBackend, cleanup_all
from provcat import Catalog, NotFound
from provcat.errors import StoreError


def _need(var):
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"set {var} to run this against a real lakeFS")
    return value


@pytest.fixture
def servers():
    a_url, b_url = _need("PROVCAT_SYSTEM_LAKEFS"), _need("PROVCAT_SYSTEM_LAKEFS_B")
    a = LakefsBackend(a_url, os.environ.get("PROVCAT_SYSTEM_LAKEFS_PROFILE", "lakefs"),
                      os.environ.get("PROVCAT_SYSTEM_LAKEFS_DATA"))
    b = LakefsBackend(b_url, os.environ.get("PROVCAT_SYSTEM_LAKEFS_B_PROFILE", "lakefs-b"),
                      os.environ.get("PROVCAT_SYSTEM_LAKEFS_B_DATA"))
    yield a, b
    cleanup_all(a, b)  # server B is cleaned up even when server A fails


def test_one_lakefs_for_each_org_keeps_them_apart(servers, tmp_path):
    a, b = servers
    acme_repo, globex_repo = ("provcat-test-" + uuid.uuid4().hex[:12] for _ in range(2))
    a.make_bucket(acme_repo)
    b.make_bucket(globex_repo)
    a.put(acme_repo, "main/a.csv", b"acme data")
    b.put(globex_repo, "main/b.csv", b"globex data")
    a.commit(acme_repo, "main", "acme")
    b.commit(globex_repo, "main", "globex")

    def store(server, profile):
        return {"driver": "lakefs", "endpoint_url": server.endpoint, "profile": profile}

    profile_a, profile_b = a.store["lake"]["profile"], b.store["lake"]["profile"]
    cfg = {
        "stores": {
            "lakefs-acme": store(a, profile_a),
            "lakefs-globex": store(b, profile_b),
            # Server A with server B's keys, and the other way round.
            "lakefs-mixab": store(a, profile_b),
            "lakefs-mixba": store(b, profile_a),
        },
        "schemes": {"t": {"parts": ["org", "repo", "env"], "rest": "path",
                          "target": "lakefs-{org}://{repo}/{ref}/{path}", "ref": "{env}",
                          "match": {"org": "[a-z0-9]{1,20}"}}},
    }
    c = Catalog(cfg, cache_root=tmp_path / "cache")

    # Each org reads its own data, at its head and pinned.
    assert c.read(f"t://acme/{acme_repo}/main/a.csv") == b"acme data"
    assert c.read(f"t://globex/{globex_repo}/main/b.csv") == b"globex data"
    assert c.read(str(c.pin(f"t://acme/{acme_repo}/main/a.csv"))) == b"acme data"

    # Neither org's store can see the other org's repository: the API says
    # the repository does not exist on that server.
    with pytest.raises(NotFound):
        c.read(f"t://acme/{globex_repo}/main/b.csv")
    with pytest.raises(NotFound):
        c.read(f"t://globex/{acme_repo}/main/a.csv")
    with pytest.raises(NotFound, match=f"has no repository '{globex_repo}'"):
        c.list(f"lakefs-acme://{globex_repo}")
    with pytest.raises(NotFound, match=f"has no repository '{acme_repo}'"):
        c.list(f"lakefs-globex://{acme_repo}")

    # Each server refuses the other server's keys, at the gateway and at the API.
    for mix, repo, key in (("mixab", acme_repo, "a.csv"), ("mixba", globex_repo, "b.csv")):
        with pytest.raises(StoreError, match=r"S3 error (403|InvalidAccessKeyId|AccessDenied|SignatureDoesNotMatch)"):
            c.read(f"lakefs-{mix}://{repo}/main/{key}")
        with pytest.raises(StoreError, match="refused the keys"):
            c.pin(f"lakefs-{mix}://{repo}/main/{key}")
