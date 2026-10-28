import boto3
import pytest

from provcat import Catalog, NotFound, OfflineMiss, UriError


@pytest.fixture
def bucket(s3_endpoint):
    s3 = boto3.client("s3", endpoint_url=s3_endpoint, region_name="us-east-1")
    s3.create_bucket(Bucket="repo")
    s3.put_object(Bucket="repo", Key="main/data/a.txt", Body=b"A1")
    s3.put_object(Bucket="repo", Key="main/data/sub/b.txt", Body=b"B1")
    s3.put_object(Bucket="repo", Key="main/top.txt", Body=b"T")
    return s3


def cat(endpoint, tmp_path, offline=False):
    # A named store "lakefs" that uses the S3 driver, the way lakeFS is used.
    cfg = {"stores": {"lakefs": {"driver": "s3", "endpoint_url": endpoint}}}
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


def test_fetch_file_and_refetch_after_change(s3_endpoint, bucket, tmp_path):
    c = cat(s3_endpoint, tmp_path)
    p = c.fetch("lakefs://repo/main/data/a.txt")
    assert p == tmp_path / "cache" / "lakefs" / "repo" / "main" / "data" / "a.txt"
    assert p.read_bytes() == b"A1"
    bucket.put_object(Bucket="repo", Key="main/data/a.txt", Body=b"A2")
    assert c.read("lakefs://repo/main/data/a.txt") == b"A2"


def test_unchanged_file_is_not_downloaded_again(s3_endpoint, bucket, tmp_path):
    c = cat(s3_endpoint, tmp_path)
    p = c.fetch("lakefs://repo/main/top.txt")
    mtime = p.stat().st_mtime_ns
    assert c.fetch("lakefs://repo/main/top.txt").stat().st_mtime_ns == mtime


def test_fetch_folder_prunes_deleted_objects(s3_endpoint, bucket, tmp_path):
    c = cat(s3_endpoint, tmp_path)
    p = c.fetch("lakefs://repo/main/data")
    assert sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()) == ["a.txt", "sub/b.txt"]
    bucket.delete_object(Bucket="repo", Key="main/data/sub/b.txt")
    c.fetch("lakefs://repo/main/data")
    assert not (p / "sub").exists()


def test_stat_and_list(s3_endpoint, bucket, tmp_path):
    c = cat(s3_endpoint, tmp_path)
    assert c.stat("lakefs://repo/main/data").kind == "folder"
    assert c.stat("lakefs://repo").kind == "folder"
    e = c.stat("lakefs://repo/main/data/a.txt")
    assert (e.kind, e.size) == ("file", 2) and e.version
    assert [(str(x.uri), x.kind) for x in c.list("lakefs://repo/main/data")] == [
        ("lakefs://repo/main/data/sub", "folder"), ("lakefs://repo/main/data/a.txt", "file")]


def test_list_of_a_missing_path_or_a_file_is_not_found(s3_endpoint, bucket, tmp_path):
    # S3 lists a missing prefix as empty; provcat must not print [] for a typo.
    c = cat(s3_endpoint, tmp_path)
    with pytest.raises(NotFound, match="no object and no folder"):
        c.list("lakefs://repo/main/nosuch")
    with pytest.raises(NotFound, match="not a folder"):
        c.list("lakefs://repo/main/top.txt")
    bucket.put_object(Bucket="repo", Key="main/empty/", Body=b"")
    assert c.list("lakefs://repo/main/empty") == []
    bucket.create_bucket(Bucket="bare")
    assert c.list("lakefs://bare") == []


def test_list_of_a_folder_needs_no_object_read(s3_endpoint, bucket, tmp_path, monkeypatch):
    # Review of the list fix, finding 2: a credential that may list but not read
    # objects must still list an empty folder.
    from provcat.errors import StoreError
    from provcat.stores.s3 import S3Store

    bucket.put_object(Bucket="repo", Key="main/empty/", Body=b"")
    real = S3Store._call

    def no_head(self, uri, method, **kw):
        if method == "head_object":
            raise StoreError("S3 error 403: Forbidden")
        return real(self, uri, method, **kw)

    monkeypatch.setattr(S3Store, "_call", no_head)
    assert cat(s3_endpoint, tmp_path).list("lakefs://repo/main/empty") == []


def test_list_of_odd_folders_is_not_an_error(s3_endpoint, bucket, tmp_path):
    # Review of the list fix, findings 3 and 4: keys no URI can name, and a key
    # that is both a file and a folder, still make a folder.
    c = cat(s3_endpoint, tmp_path)
    bucket.put_object(Bucket="repo", Key="main/odd//x", Body=b"x")
    assert c.list("lakefs://repo/main/odd") == []
    bucket.put_object(Bucket="repo", Key="main/both", Body=b"f")
    bucket.put_object(Bucket="repo", Key="main/both/", Body=b"")
    assert c.list("lakefs://repo/main/both") == []
    bucket.put_object(Bucket="repo", Key="main/both2", Body=b"f")
    bucket.put_object(Bucket="repo", Key="main/both2//z", Body=b"z")
    assert c.list("lakefs://repo/main/both2") == []


def test_offline(s3_endpoint, bucket, tmp_path):
    cat(s3_endpoint, tmp_path).fetch("lakefs://repo/main/top.txt")
    off = cat("http://127.0.0.1:9", tmp_path, offline=True)
    assert off.read("lakefs://repo/main/top.txt") == b"T"
    with pytest.raises(OfflineMiss):
        off.fetch("lakefs://repo/main/data/a.txt")


def test_missing_and_bad(s3_endpoint, bucket, tmp_path):
    c = cat(s3_endpoint, tmp_path)
    with pytest.raises(NotFound):
        c.fetch("lakefs://repo/main/nope")
    with pytest.raises(UriError):
        c.fetch("lakefs://repo/main/top.txt?at=branch:main")
    with pytest.raises(UriError):
        c.fetch("lakefs:///main/top.txt")


def test_logical_uri_through_lakefs_store(s3_endpoint, bucket, tmp_path):
    cfg = {
        "stores": {"lakefs": {"driver": "s3", "endpoint_url": s3_endpoint}},
        "schemes": {"data": {"parts": ["repo", "name"], "target": "lakefs://{repo}/{ref}/data/{name}", "ref": "main"}},
    }
    c = Catalog(cfg, cache_root=tmp_path / "cache")
    assert c.read("data://repo/a.txt") == b"A1"


def test_unknown_store_option_is_an_error():
    from provcat import ConfigError

    with pytest.raises(ConfigError, match="unknown options"):
        Catalog({"stores": {"s3": {"endpoint": "x"}}})


def test_failed_folder_refresh_leaves_no_done_marker(s3_endpoint, bucket, tmp_path, monkeypatch):
    # Finding 3: an old marker survived a failed refresh, and offline served a mix.
    from provcat.stores.s3 import S3Store
    from provcat import StoreError

    c = cat(s3_endpoint, tmp_path)
    c.fetch("lakefs://repo/main/data")
    bucket.put_object(Bucket="repo", Key="main/data/a.txt", Body=b"A2")
    bucket.put_object(Bucket="repo", Key="main/data/sub/b.txt", Body=b"B2")
    real = S3Store._download
    calls = []

    def flaky(self, entry, cache):
        calls.append(entry)
        if len(calls) == 2:
            raise StoreError("network down")
        return real(self, entry, cache)

    monkeypatch.setattr(S3Store, "_download", flaky)
    with pytest.raises(StoreError):
        c.fetch("lakefs://repo/main/data")
    with pytest.raises(OfflineMiss):
        cat(s3_endpoint, tmp_path, offline=True).fetch("lakefs://repo/main/data")


def test_folder_with_unnameable_keys_is_refused(s3_endpoint, bucket, tmp_path):
    # Finding 4: such keys were skipped and the folder still marked complete.
    from provcat import StoreError

    bucket.put_object(Bucket="repo", Key="main/odd/ok.txt", Body=b"k")
    bucket.put_object(Bucket="repo", Key="main/odd/dbl//z.txt", Body=b"z")
    with pytest.raises(StoreError, match="no URI can name"):
        cat(s3_endpoint, tmp_path).fetch("lakefs://repo/main/odd")


def test_folder_with_case_twins_is_refused(s3_endpoint, bucket, tmp_path):
    # Finding 5: Read.txt and read.txt share one file on a case-blind disk.
    from provcat import StoreError

    bucket.put_object(Bucket="repo", Key="main/case/Read.txt", Body=b"1")
    bucket.put_object(Bucket="repo", Key="main/case/read.txt", Body=b"2")
    with pytest.raises(StoreError, match="one name"):
        cat(s3_endpoint, tmp_path).fetch("lakefs://repo/main/case")


@pytest.mark.parametrize("keys", [
    ["Dir/a", "dir/b"],                      # second review, finding 2: twins in a folder name
    ["caf\u00e9.txt", "cafe\u0301.txt"],     # the same name in NFC and NFD
])
def test_folder_with_prefix_or_unicode_twins_is_refused(s3_endpoint, bucket, tmp_path, keys):
    from provcat import StoreError

    for k in keys:
        bucket.put_object(Bucket="repo", Key=f"main/tw/{k}", Body=k.encode())
    with pytest.raises(StoreError, match="one name"):
        cat(s3_endpoint, tmp_path).fetch("lakefs://repo/main/tw")


def test_single_case_twins_never_serve_the_wrong_bytes(s3_endpoint, bucket, tmp_path):
    bucket.put_object(Bucket="repo", Key="main/Twin.txt", Body=b"upper")
    bucket.put_object(Bucket="repo", Key="main/twin.txt", Body=b"lower")
    c = cat(s3_endpoint, tmp_path)
    for _ in range(2):
        assert c.read("lakefs://repo/main/Twin.txt") == b"upper"
        assert c.read("lakefs://repo/main/twin.txt") == b"lower"


def test_file_and_prefix_with_one_name_gives_a_store_error(s3_endpoint, bucket, tmp_path):
    # Finding 5: the cache collision escaped as FileExistsError.
    from provcat import StoreError

    bucket.put_object(Bucket="repo", Key="main/fp/a", Body=b"file")
    bucket.put_object(Bucket="repo", Key="main/fp/a/b", Body=b"child")
    c = cat(s3_endpoint, tmp_path)
    c.fetch("lakefs://repo/main/fp/a")
    with pytest.raises(StoreError):
        c.fetch("lakefs://repo/main/fp/a/b")


def test_meta_records_cannot_collide(s3_endpoint, bucket, tmp_path):
    # Finding 5: the record for "a" was "a.json", which blocked a key "a.json/x".
    bucket.put_object(Bucket="repo", Key="main/m/a", Body=b"a")
    bucket.put_object(Bucket="repo", Key="main/m2/a.json/x", Body=b"x")
    c = cat(s3_endpoint, tmp_path)
    assert c.read("lakefs://repo/main/m/a") == b"a"
    bucket.put_object(Bucket="repo", Key="main/m/a.json/x", Body=b"x")
    assert c.read("lakefs://repo/main/m/a.json/x") == b"x"


def test_long_key_segment_is_cached(s3_endpoint, bucket, tmp_path):
    # Finding 5: temporary-name prefixes pushed a 245-byte name past the limit.
    name = "n" * 245
    bucket.put_object(Bucket="repo", Key=f"main/{name}", Body=b"long")
    assert cat(s3_endpoint, tmp_path).read(f"lakefs://repo/main/{name}") == b"long"


def test_missing_profile_is_a_store_error(tmp_path):
    # Finding 9: the client was built outside the error wrapper.
    from provcat import StoreError

    c = Catalog({"stores": {"s3": {"profile": "no-such-profile-xyz"}}}, cache_root=tmp_path)
    with pytest.raises(StoreError):
        c.fetch("s3://b/k")


def test_recorded_etag_is_the_one_downloaded(s3_endpoint, bucket, tmp_path, monkeypatch):
    # The object changes between stat and download; the record must match the bytes.
    from provcat.stores.s3 import S3Store

    c = cat(s3_endpoint, tmp_path)
    real = S3Store.stat

    def stat_then_change(self, uri, cache):
        e = real(self, uri, cache)
        bucket.put_object(Bucket="repo", Key="main/top.txt", Body=b"T2")
        return e

    monkeypatch.setattr(S3Store, "stat", stat_then_change)
    assert c.read("lakefs://repo/main/top.txt") == b"T2"
    monkeypatch.setattr(S3Store, "stat", real)
    p = c.fetch("lakefs://repo/main/top.txt")
    assert p.read_bytes() == b"T2"


def test_example_schemes_keep_each_org_apart(s3_endpoint, bucket, tmp_path):
    # The org is the tenant: the same names for two orgs read two repositories,
    # and the environment picks the branch.
    from pathlib import Path

    from provcat import YamlConfig

    cfg = YamlConfig(Path(__file__).parent.parent / "config" / "provcat.example.yaml").load()
    cfg["stores"]["lakefs"] = {"driver": "s3", "endpoint_url": s3_endpoint}
    for org in ("acme", "globex"):
        bucket.create_bucket(Bucket=f"{org}-data-shared")
        for env in ("prod", "dev"):
            bucket.put_object(Bucket=f"{org}-data-shared", Key=f"{env}/tables/agreements/qa/part-0.csv",
                              Body=f"{org} {env}".encode())
    c = Catalog(cfg, cache_root=tmp_path / "cache")
    for org in ("acme", "globex"):
        for env in ("prod", "dev"):
            folder = c.fetch(f"data+table://{org}/shared/{env}/agreements/qa")
            assert folder == tmp_path / "cache" / "lakefs" / f"{org}-data-shared" / env / "tables" / "agreements" / "qa"
            assert (folder / "part-0.csv").read_bytes() == f"{org} {env}".encode()
