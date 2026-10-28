"""The same tests for every object store driver.

tests/integration runs them against local servers, and tests/system against
the real clouds and lakeFS. Each of those modules imports everything here and
gives its own ``backend`` fixture. A backend makes buckets, writes and deletes
objects with the cloud's Python SDK, and names URIs for the store "lake".
"""
import uuid

import pytest

from provcat import Catalog, NotFound, OfflineMiss
from provcat.errors import StoreError


class S3Backend:
    def __init__(self, endpoint=None, profile=None):
        import boto3

        session = boto3.session.Session(profile_name=profile)
        self.client = session.client("s3", endpoint_url=endpoint, region_name="us-east-1")
        self.store = {"lake": {"driver": "s3"}}
        if endpoint:
            self.store["lake"]["endpoint_url"] = endpoint
        if profile:
            self.store["lake"]["profile"] = profile
        self.created = []

    def uri(self, bucket, key=""):
        return f"lake://{bucket}/{key}".rstrip("/")

    def make_bucket(self, name):
        self.client.create_bucket(Bucket=name)
        self.created.append(name)

    def cleanup(self):
        for name in self.created:
            pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=name)
            for page in pages:
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self.client.delete_objects(Bucket=name, Delete={"Objects": keys})
            self.client.delete_bucket(Bucket=name)

    def put(self, bucket, key, data):
        self.client.put_object(Bucket=bucket, Key=key, Body=data)

    def delete(self, bucket, key):
        self.client.delete_object(Bucket=bucket, Key=key)


class GcsBackend:
    def __init__(self, endpoint=None, project=None):
        from google.cloud import storage

        self.emulated = bool(endpoint)
        if endpoint:
            from google.auth.credentials import AnonymousCredentials

            self.client = storage.Client(project="test", credentials=AnonymousCredentials(),
                                         client_options={"api_endpoint": endpoint})
            self.store = {"lake": {"driver": "gcs", "endpoint_url": endpoint, "anonymous": True}}
        else:
            self.client = storage.Client(project=project)
            self.store = {"lake": {"driver": "gcs", "project": project}}
        self.created = []

    def uri(self, bucket, key=""):
        return f"lake://{bucket}/{key}".rstrip("/")

    def make_bucket(self, name):
        self.client.create_bucket(name, location="us-east1")
        self.created.append(name)

    def cleanup(self):
        for name in self.created:
            self.client.bucket(name).delete(force=True)

    def put(self, bucket, key, data):
        self.client.bucket(bucket).blob(key).upload_from_string(data)

    def delete(self, bucket, key):
        self.client.bucket(bucket).blob(key).delete()


class AzureBackend:
    def __init__(self, connection, monkeypatch):
        from azure.storage.blob import BlobServiceClient

        monkeypatch.setenv("PROVCAT_TEST_AZURE", connection)
        self.client = BlobServiceClient.from_connection_string(connection)
        self.account = self.client.account_name
        self.store = {"lake": {"driver": "azure", "connection_string_env": "PROVCAT_TEST_AZURE"}}
        self.created = []
        self.hns = False  # true for an ADLS Gen2 account, which keeps folders as marker blobs

    def uri(self, bucket, key=""):
        return f"lake://{self.account}/{bucket}/{key}".rstrip("/")

    def make_bucket(self, name):
        self.client.create_container(name)
        self.created.append(name)

    def cleanup(self):
        for name in self.created:
            self.client.delete_container(name)

    def put(self, bucket, key, data):
        if self.hns and key.endswith("/"):
            # ADLS Gen2 refuses a name that ends in "/". Its folders are blobs
            # with the metadata hdi_isfolder.
            self.client.get_container_client(bucket).upload_blob(
                key.rstrip("/"), b"", overwrite=True, metadata={"hdi_isfolder": "true"})
            return
        self.client.get_container_client(bucket).upload_blob(key, data, overwrite=True)

    def delete(self, bucket, key):
        self.client.get_container_client(bucket).delete_blob(key)


class LakefsBackend(S3Backend):
    """lakeFS through its S3 gateway. A bucket is a repository whose default
    branch is main, so the suite's keys "main/..." are paths on branch main."""

    def __init__(self, endpoint, profile):
        super().__init__(endpoint, profile)
        self.endpoint = endpoint
        creds = self.client._request_signer._credentials.get_frozen_credentials()
        import base64

        self.auth = base64.b64encode(f"{creds.access_key}:{creds.secret_key}".encode()).decode()

    def _api(self, method, path, body=None):
        import json
        import urllib.request

        req = urllib.request.Request(f"{self.endpoint}/api/v1{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Basic " + self.auth,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return resp.status

    def make_bucket(self, name):
        self._api("POST", "/repositories", {"name": name, "storage_namespace": f"local://{name}",
                                             "default_branch": "main"})
        self.created.append(name)

    def cleanup(self):
        for name in self.created:
            self._api("DELETE", f"/repositories/{name}")


@pytest.fixture
def bucket(backend):
    # Bucket names are global on AWS and GCS, so make them unique and plain.
    name = "provcat-test-" + uuid.uuid4().hex[:12]
    backend.make_bucket(name)
    backend.put(name, "main/data/a.txt", b"A1")
    backend.put(name, "main/data/sub/b.txt", b"B1")
    backend.put(name, "main/top.txt", b"T")
    return name


def cat(backend, tmp_path, offline=False):
    return Catalog({"stores": backend.store}, cache_root=tmp_path / "cache", offline=offline)


def test_fetch_file_and_refetch_after_change(backend, bucket, tmp_path):
    c = cat(backend, tmp_path)
    uri = backend.uri(bucket, "main/data/a.txt")
    p = c.fetch(uri)
    assert p.read_bytes() == b"A1"
    assert p.parts[-3:] == ("main", "data", "a.txt")
    before = c.stat(uri).version
    backend.put(bucket, "main/data/a.txt", b"A2")
    assert c.stat(uri).version != before
    assert c.read(uri) == b"A2"


def test_folder_fetch_adds_and_prunes(backend, bucket, tmp_path):
    c = cat(backend, tmp_path)
    folder = c.fetch(backend.uri(bucket, "main/data"))
    assert (folder / "a.txt").read_bytes() == b"A1"
    assert (folder / "sub" / "b.txt").read_bytes() == b"B1"
    backend.delete(bucket, "main/data/sub/b.txt")
    backend.put(bucket, "main/data/c.txt", b"C1")
    folder = c.fetch(backend.uri(bucket, "main/data"))
    assert not (folder / "sub" / "b.txt").exists()
    assert (folder / "c.txt").read_bytes() == b"C1"


def test_stat_and_list(backend, bucket, tmp_path):
    c = cat(backend, tmp_path)
    assert c.stat(backend.uri(bucket, "main/top.txt")).kind == "file"
    assert c.stat(backend.uri(bucket, "main/top.txt")).size == 1
    assert c.stat(backend.uri(bucket, "main/data")).kind == "folder"
    listed = {(str(e.uri), e.kind) for e in c.list(backend.uri(bucket, "main"))}
    assert listed == {(backend.uri(bucket, "main/data"), "folder"), (backend.uri(bucket, "main/top.txt"), "file")}
    with pytest.raises(NotFound):
        c.stat(backend.uri(bucket, "main/nosuch"))
    with pytest.raises(NotFound, match="no object and no folder"):
        c.list(backend.uri(bucket, "main/nosuch"))
    with pytest.raises(NotFound, match="not a folder"):
        c.list(backend.uri(bucket, "main/top.txt"))


def test_missing_bucket(backend, tmp_path):
    c = cat(backend, tmp_path)
    with pytest.raises(NotFound):
        c.list(backend.uri("nosuchbucket" + uuid.uuid4().hex[:6], "x"))


def test_offline(backend, bucket, tmp_path):
    cat(backend, tmp_path).fetch(backend.uri(bucket, "main/data"))
    off = cat(backend, tmp_path, offline=True)
    assert off.read(backend.uri(bucket, "main/data/sub/b.txt")) == b"B1"
    assert (off.fetch(backend.uri(bucket, "main/data")) / "a.txt").read_bytes() == b"A1"
    with pytest.raises(OfflineMiss):
        off.fetch(backend.uri(bucket, "main/top.txt"))
    with pytest.raises(OfflineMiss):
        off.list(backend.uri(bucket, "main"))


def test_case_twins_are_refused(backend, bucket, tmp_path):
    backend.put(bucket, "main/twins/Dir/a", b"1")
    backend.put(bucket, "main/twins/dir/b", b"2")
    with pytest.raises(StoreError, match="one name"):
        cat(backend, tmp_path).fetch(backend.uri(bucket, "main/twins"))


def test_folder_markers_are_folders(backend, bucket, tmp_path):
    if isinstance(backend, GcsBackend) and backend.emulated:
        pytest.skip("fake-gcs-server drops objects whose names end in /; tests/system covers real GCS")
    backend.put(bucket, "main/empty/", b"")
    c = cat(backend, tmp_path)
    assert c.list(backend.uri(bucket, "main/empty")) == []
    assert c.stat(backend.uri(bucket, "main/empty")).kind == "folder"
    folder = c.fetch(backend.uri(bucket, "main/empty"))
    assert folder.is_dir() and list(folder.iterdir()) == []


def test_listings_follow_every_page(backend, bucket, tmp_path):
    # Review of the cloud drivers, finding 8: force pages of 2 entries.
    if isinstance(backend, GcsBackend) and backend.emulated:
        pytest.skip("fake-gcs-server sends no next-page token; tests/system checks paging on real GCS")
    # "a.txt" comes first, so pages of 2 do not line up with folder pairs.
    backend.put(bucket, "main/many/a.txt", b"a")
    for i in range(5):
        backend.put(bucket, f"main/many/f{i}.txt", str(i).encode())
    for i in range(3):
        backend.put(bucket, f"main/many/d{i}/x.txt", b"x")
    c = cat(backend, tmp_path)
    c.stores["lake"].page_size = 2
    names = sorted(str(e.uri).rsplit("/", 1)[1] for e in c.list(backend.uri(bucket, "main/many")))
    assert names == ["a.txt", "d0", "d1", "d2", "f0.txt", "f1.txt", "f2.txt", "f3.txt", "f4.txt"]
    folder = c.fetch(backend.uri(bucket, "main/many"))
    assert sorted(p.name for p in folder.rglob("*.txt")) == ["a.txt", "f0.txt", "f1.txt", "f2.txt", "f3.txt",
                                                             "f4.txt", "x.txt", "x.txt", "x.txt"]
