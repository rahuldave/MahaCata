"""The object store suite against the real clouds and a real lakeFS.

Each backend runs only when its environment variable is set. Each test makes
its own bucket, or container, and the fixture deletes it afterwards, even when
the test fails. Everything is made and deleted with the Python SDKs.

- PROVCAT_SYSTEM_AWS=1: AWS S3 with the default credentials. PROVCAT_SYSTEM_AWS_PROFILE
  picks a profile.
- PROVCAT_SYSTEM_GCS=<project>: Google Cloud Storage in that project, with
  Application Default Credentials (gcloud auth application-default login).
- PROVCAT_SYSTEM_AZURE=<subscription id>: makes a resource group with one ADLS
  Gen2 storage account for the session, and deletes the group at the end.
  PROVCAT_SYSTEM_AZURE_LOCATION picks the region (default eastus). It uses
  DefaultAzureCredential, for example az login.
- PROVCAT_SYSTEM_LAKEFS=<url>: lakeFS, for example http://localhost:8000, with
  the keys of the AWS profile PROVCAT_SYSTEM_LAKEFS_PROFILE (default lakefs).
  PROVCAT_SYSTEM_LAKEFS_DATA names the local blockstore path of lakeFS. With
  it, cleanup also deletes the storage folder of each repository it made,
  because lakeFS keeps that folder when a repository is deleted.
"""
import os
import uuid

import pytest

from object_store_suite import *  # noqa: F401,F403  (the tests and backends)
from provcat import NotFound

from object_store_suite import AzureBackend, GcsBackend, LakefsBackend, S3Backend, cat


def _need(var):
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"set {var} to run this against the real service")
    return value


@pytest.fixture(scope="session")
def azure_connection():
    subscription = _need("PROVCAT_SYSTEM_AZURE")
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource.resources import ResourceManagementClient
    from azure.mgmt.storage import StorageManagementClient

    credential = DefaultAzureCredential()
    location = os.environ.get("PROVCAT_SYSTEM_AZURE_LOCATION", "eastus")
    suffix = uuid.uuid4().hex[:12]
    group, account = f"provcat-test-{suffix}", f"provcattest{suffix}"
    groups = ResourceManagementClient(credential, subscription).resource_groups
    accounts = StorageManagementClient(credential, subscription).storage_accounts
    try:
        groups.create_or_update(group, {"location": location, "tags": {"purpose": "provcat system test"}})
        from azure.mgmt.storage import models

        accounts.begin_create(group, account, models.StorageAccountCreateParameters(
            location=location, kind="StorageV2", sku=models.Sku(name="Standard_LRS"),
            properties=models.StorageAccountPropertiesCreateParameters(
                is_hns_enabled=True, minimum_tls_version="TLS1_2", allow_blob_public_access=False),
        )).result()
        key = accounts.list_keys(group, account).keys_property[0].value
        yield (f"DefaultEndpointsProtocol=https;AccountName={account};AccountKey={key};"
               "EndpointSuffix=core.windows.net")
    finally:
        # Delete the group only if it exists, so a failed setup shows its own error.
        if groups.check_existence(group):
            groups.begin_delete(group).result()


@pytest.fixture(params=["aws", "gcs", "azure", "lakefs"])
def backend(request, monkeypatch):
    if request.param == "aws":
        _need("PROVCAT_SYSTEM_AWS")
        b = S3Backend(profile=os.environ.get("PROVCAT_SYSTEM_AWS_PROFILE"))
    elif request.param == "gcs":
        b = GcsBackend(project=_need("PROVCAT_SYSTEM_GCS"))
    elif request.param == "azure":
        b = AzureBackend(request.getfixturevalue("azure_connection"), monkeypatch)
        b.hns = True
    else:
        b = LakefsBackend(_need("PROVCAT_SYSTEM_LAKEFS"),
                          os.environ.get("PROVCAT_SYSTEM_LAKEFS_PROFILE", "lakefs"),
                          os.environ.get("PROVCAT_SYSTEM_LAKEFS_DATA"))
    yield b
    b.cleanup()


@pytest.fixture
def lakefs(monkeypatch):
    b = LakefsBackend(_need("PROVCAT_SYSTEM_LAKEFS"), os.environ.get("PROVCAT_SYSTEM_LAKEFS_PROFILE", "lakefs"),
                      os.environ.get("PROVCAT_SYSTEM_LAKEFS_DATA"))
    yield b
    b.cleanup()


def test_lakefs_pins_and_times(lakefs, tmp_path):
    """Two real commits: ts: between them reads the first, pin reads the
    second, and the branch also shows what is not committed."""
    import time
    from datetime import UTC, datetime

    repo = "provcat-test-" + uuid.uuid4().hex[:12]
    lakefs.make_bucket(repo)
    first_commit_of_repo = lakefs.log(repo, "main")[-1]["id"]
    lakefs.put(repo, "main/t/a.csv", b"v1")
    c1 = lakefs.commit(repo, "main", "v1")
    time.sleep(2.1)
    between = datetime.now(UTC).isoformat(timespec="seconds")
    time.sleep(2.1)
    lakefs.put(repo, "main/t/a.csv", b"v2")
    c2 = lakefs.commit(repo, "main", "v2")
    lakefs.put(repo, "main/t/a.csv", b"v3, not committed")
    lakefs.branch(repo, "fresh", first_commit_of_repo)

    c = cat(lakefs, tmp_path)
    uri = lakefs.uri(repo, "main/t/a.csv")
    assert c.read(uri) == b"v3, not committed"
    assert str(c.pin(uri)) == lakefs.uri(repo, f"{c2}/t/a.csv")
    assert c.read(str(c.pin(uri))) == b"v2"
    assert c.read(f"{uri}?at=ts:{between.replace('+', '%2B')}") == b"v1"
    assert str(c.pin(f"{uri}?at=ts:{between.replace('+', '%2B')}")) == lakefs.uri(repo, f"{c1}/t/a.csv")
    assert c.list(lakefs.uri(repo, "fresh")) == []
    with pytest.raises(NotFound, match="no branch, tag or commit 'nosuch'"):
        c.list(lakefs.uri(repo, "nosuch"))


def test_lakefs_ts_ignores_merged_commits(lakefs, tmp_path):
    """A feature commit made before a merge is not on main until the merge."""
    import time
    from datetime import UTC, datetime

    repo = "provcat-test-" + uuid.uuid4().hex[:12]
    lakefs.make_bucket(repo)
    lakefs.put(repo, "main/a.csv", b"main v1")
    m = lakefs.commit(repo, "main", "main v1")
    lakefs.branch(repo, "feature", "main")
    time.sleep(2.1)
    lakefs.put(repo, "feature/a.csv", b"feature v1")
    lakefs.commit(repo, "feature", "feature v1")
    time.sleep(2.1)
    between = datetime.now(UTC).isoformat(timespec="seconds").replace("+", "%2B")
    time.sleep(2.1)
    lakefs.merge(repo, "feature", "main")

    c = cat(lakefs, tmp_path)
    uri = lakefs.uri(repo, "main/a.csv")
    assert c.read(uri) == b"feature v1"  # after the merge
    assert str(c.pin(f"{uri}?at=ts:{between}")) == lakefs.uri(repo, f"{m}/a.csv")
    assert c.read(f"{uri}?at=ts:{between}") == b"main v1"
    assert sorted(str(e.uri) for e in c.list(lakefs.uri(repo))) == [lakefs.uri(repo, "feature"),
                                                                    lakefs.uri(repo, "main")]
