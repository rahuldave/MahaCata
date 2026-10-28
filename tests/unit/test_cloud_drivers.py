"""Driver logic that the emulators cannot show: missing extras, and the
folder marker blobs of an ADLS Gen2 account."""
import builtins
from pathlib import Path
from types import SimpleNamespace

import pytest

from provcat import Catalog
from provcat.errors import StoreError


@pytest.mark.parametrize("uri, extra, blocked", [
    ("gs://bucket/key", "gcs", "google"),
    ("az://account/container/key", "azure", "azure"),
])
def test_missing_extra_names_the_install(uri, extra, blocked, monkeypatch, tmp_path):
    real = builtins.__import__

    def no_sdk(name, *args, **kw):
        if name.startswith(blocked):
            raise ImportError(name)
        return real(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    with pytest.raises(StoreError, match=rf"provcat\[{extra}\]"):
        Catalog({}, cache_root=tmp_path).stat(uri)


class Blob(SimpleNamespace):
    pass


class FakeContainer:
    """A container on an ADLS Gen2 account: every folder is also a blob
    with the metadata hdi_isfolder."""

    def __init__(self):
        folder = {"hdi_isfolder": "true"}
        self.blobs = [Blob(name="d", size=0, etag='"e0"', metadata=folder),
                      Blob(name="d/a.txt", size=2, etag='"e1"', metadata={}),
                      Blob(name="d/sub", size=0, etag='"e2"', metadata=folder),
                      Blob(name="d/sub/b.txt", size=1, etag='"e3"', metadata={}),
                      Blob(name="d/empty", size=0, etag='"e4"', metadata=folder)]

    @staticmethod
    def _paged(items, size):
        size = size or len(items) or 1
        return SimpleNamespace(by_page=lambda: iter([items[i:i + size] for i in range(0, len(items), size)]
                                                    or [[]]))

    def list_blobs(self, name_starts_with=None, results_per_page=None, **kw):
        return self._paged([b for b in self.blobs if b.name.startswith(name_starts_with or "")],
                           results_per_page)

    def walk_blobs(self, name_starts_with=None, delimiter="/", results_per_page=None, **kw):
        prefix = name_starts_with or ""
        out, seen = [], set()
        for b in self.blobs:
            if not b.name.startswith(prefix):
                continue
            rest = b.name[len(prefix):]
            if "/" in rest:
                p = prefix + rest.split("/")[0] + "/"
                if p not in seen:
                    seen.add(p)
                    out.append(SimpleNamespace(name=p))
            else:
                out.append(b)
        return self._paged(out, results_per_page)

    def get_blob_client(self, key):
        from azure.core.exceptions import ResourceNotFoundError

        def props():
            for b in self.blobs:
                if b.name == key:
                    return b
            raise ResourceNotFoundError("missing")

        return SimpleNamespace(get_blob_properties=props)


def test_adls_folder_markers_are_folders(monkeypatch, tmp_path):
    c = Catalog({}, cache_root=tmp_path)
    store = c.stores["az"]
    fake = FakeContainer()
    monkeypatch.setattr(store, "_service", lambda account: SimpleNamespace(get_container_client=lambda n: fake))
    listed = {(str(e.uri), e.kind) for e in c.list("az://acct/ctr/d")}
    assert listed == {("az://acct/ctr/d/a.txt", "file"), ("az://acct/ctr/d/sub", "folder"),
                      ("az://acct/ctr/d/empty", "folder")}
    assert c.stat("az://acct/ctr/d/empty").kind == "folder"
    assert c.stat("az://acct/ctr/d/sub/b.txt").kind == "file"
    # An empty ADLS folder lists as empty, not as missing.
    assert c.list("az://acct/ctr/d/empty") == []
    from provcat import NotFound

    with pytest.raises(NotFound, match="no object and no folder"):
        c.list("az://acct/ctr/d/nosuch")
    walked = sorted(str(e.uri) for e in store.walk(c.resolve("az://acct/ctr/d").physical))
    assert walked == ["az://acct/ctr/d/a.txt", "az://acct/ctr/d/sub/b.txt"]


AZ_KEY = "a2V5"  # base64 of "key"; never sent anywhere


def _conn(account):
    return (f"DefaultEndpointsProtocol=https;AccountName={account};AccountKey={AZ_KEY};"
            "EndpointSuffix=core.windows.net")


def test_azure_connection_string_serves_only_its_account(monkeypatch, tmp_path):
    # Review of the cloud drivers, finding 1: never read another account's data.
    from provcat.errors import ConfigError

    monkeypatch.setenv("PROVCAT_AZ", _conn("mine"))
    c = Catalog({"stores": {"lake": {"driver": "azure", "connection_string_env": "PROVCAT_AZ"}}},
                cache_root=tmp_path)
    with pytest.raises(ConfigError, match="for account 'mine', not 'someoneelse'"):
        c.stat("lake://someoneelse/ctr/key")
    with pytest.raises(ConfigError, match=r"must contain \{account\}"):
        Catalog({"stores": {"lake": {"driver": "azure", "account_url": "https://one.blob.core.windows.net"}}})


def test_azure_bad_connection_string_is_a_config_error(monkeypatch, tmp_path):
    # Finding 5: a malformed string is a ConfigError, and the error does not show it.
    from provcat.errors import ConfigError

    monkeypatch.setenv("PROVCAT_AZ", "AccountKey=SECRETVALUE;garbage")
    c = Catalog({"stores": {"lake": {"driver": "azure", "connection_string_env": "PROVCAT_AZ"}}},
                cache_root=tmp_path)
    with pytest.raises(ConfigError) as e:
        c.stat("lake://mine/ctr/key")
    assert "SECRETVALUE" not in str(e.value)


def test_azure_without_credentials_says_so(monkeypatch, tmp_path):
    # Finding 4: no login gives "no Azure credentials", not "refused".
    from azure.identity import CredentialUnavailableError

    c = Catalog({}, cache_root=tmp_path)

    def no_login(*a, **kw):
        raise CredentialUnavailableError("DefaultAzureCredential failed to retrieve a token")

    container = SimpleNamespace(get_blob_client=lambda k: SimpleNamespace(get_blob_properties=no_login))
    monkeypatch.setattr(c.stores["az"], "_service",
                        lambda account: SimpleNamespace(get_container_client=lambda n: container))
    with pytest.raises(StoreError, match="no Azure credentials; run az login"):
        c.stat("az://acct/ctr/key")


def test_gcs_download_takes_its_version_from_the_response(tmp_path, monkeypatch):
    # Finding 2: one request, and the version is the one the bytes came with.
    c = Catalog({}, cache_root=tmp_path)
    store = c.stores["gs"]
    calls = []

    class Blob:
        generation = None

        def download_to_file(self, out):
            calls.append("download")
            out.write(b"new bytes")
            self.generation = 42  # the SDK fills this from x-goog-generation

    client = SimpleNamespace(bucket=lambda b: SimpleNamespace(blob=lambda k: Blob()))
    monkeypatch.setattr(store, "_client", client)
    path = tmp_path / "out"
    with path.open("wb") as f:
        assert store._get(c.resolve("gs://b/k").physical, "b", "k", f) == "42"
    assert calls == ["download"] and path.read_bytes() == b"new bytes"


def test_gcs_checksum_failure_is_a_store_error(tmp_path):
    # Finding 6.
    from google.cloud.storage.exceptions import DataCorruption

    store = Catalog({}, cache_root=tmp_path).stores["gs"]
    store._client = object()

    def bad(client):
        raise DataCorruption(None, "crc32c mismatch")

    with pytest.raises(StoreError, match="checksum"):
        store._call(Catalog({}).resolve("gs://b/k").physical, bad)


def test_markers_come_from_the_folder_under_tests_only(tmp_path):
    # Finding 3: a checkout under a folder named "system" must not mark every test.
    import conftest

    here = Path(conftest.__file__).parent
    items = [SimpleNamespace(path=here / "unit" / "test_x.py", marks=[]),
             SimpleNamespace(path=Path("/somewhere/system/repo/tests/unit/test_y.py"), marks=[])]
    for item in items:
        item.add_marker = item.marks.append
    conftest.pytest_collection_modifyitems(None, items)
    assert items[0].marks == ["unit"] and items[1].marks == []


def test_a_file_listed_for_its_own_prefix_is_still_a_file(monkeypatch, tmp_path):
    # Real ADLS Gen2 lists "d/a.txt" for the prefix "d/a.txt/".
    from provcat import NotFound

    c = Catalog({}, cache_root=tmp_path)
    fake = FakeContainer()
    real_walk = fake.walk_blobs

    def walk(name_starts_with=None, **kw):
        if name_starts_with == "d/a.txt/":
            return SimpleNamespace(by_page=lambda: iter([[fake.blobs[1]]]))
        return real_walk(name_starts_with=name_starts_with, **kw)

    fake.walk_blobs = walk
    monkeypatch.setattr(c.stores["az"], "_service", lambda account: SimpleNamespace(get_container_client=lambda n: fake))
    with pytest.raises(NotFound, match="not a folder"):
        c.list("az://acct/ctr/d/a.txt")


def _azure(monkeypatch, tmp_path, fake):
    c = Catalog({}, cache_root=tmp_path)
    monkeypatch.setattr(c.stores["az"], "_service",
                        lambda account: SimpleNamespace(get_container_client=lambda n: fake))
    return c


@pytest.mark.parametrize("page", [1, 2, 3, None])
def test_adls_folder_split_across_pages_is_listed_once(page, monkeypatch, tmp_path):
    # Second review of the cloud drivers, finding 1.
    fake = FakeContainer()
    c = _azure(monkeypatch, tmp_path, fake)
    c.stores["az"].page_size = page
    names = [str(e.uri).rsplit("/", 1)[1] for e in c.list("az://acct/ctr/d")]
    assert sorted(names) == ["a.txt", "empty", "sub"]


def test_flat_account_folder_found_through_a_marker(monkeypatch, tmp_path):
    # Finding 2: no marker for the folder itself, and its first key is a marker.
    fake = FakeContainer()
    fake.blobs = [Blob(name="d/sub", size=0, etag='"e2"', metadata={"hdi_isfolder": "true"}),
                  Blob(name="d/sub/b.txt", size=1, etag='"e3"', metadata={})]
    c = _azure(monkeypatch, tmp_path, fake)
    assert c.stat("az://acct/ctr/d").kind == "folder"


def test_head_errors_other_than_not_found_are_not_hidden(monkeypatch, tmp_path):
    # Finding 7: a refused head on an empty listing is an error, not "missing".
    from azure.core.exceptions import HttpResponseError

    fake = FakeContainer()
    fake.blobs = []

    def forbidden(key):
        def props():
            e = HttpResponseError(message="This request is not authorized")
            e.status_code = 403
            raise e
        return SimpleNamespace(get_blob_properties=props)

    fake.get_blob_client = forbidden
    c = _azure(monkeypatch, tmp_path, fake)
    with pytest.raises(StoreError, match="403"):
        c.list("az://acct/ctr/x")


def test_azure_failed_sign_in_shows_its_cause(monkeypatch, tmp_path):
    # Finding 3: a login that fails is not "no credentials", and says why.
    from azure.core.exceptions import ClientAuthenticationError

    def expired(*a, **kw):
        raise ClientAuthenticationError(message="AADSTS700082: The refresh token has expired")

    container = SimpleNamespace(get_blob_client=lambda k: SimpleNamespace(get_blob_properties=expired))
    c = _azure(monkeypatch, tmp_path, container)
    with pytest.raises(StoreError, match="could not sign in .AADSTS700082"):
        c.stat("az://acct/ctr/key")
