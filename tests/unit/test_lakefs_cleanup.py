"""The lakeFS system backend deletes only the storage folders it made."""
import os

import pytest

from object_store_suite import LakefsBackend


def backend(data_dir, created, fail=()):
    b = object.__new__(LakefsBackend)  # skip __init__, which needs a lakeFS profile
    b.data_dir = data_dir
    b.created = created
    b.deleted = []

    def api(method, path, body=None):
        if path.rsplit("/", 1)[1] in fail:
            raise OSError("HTTP Error 500")
        b.deleted.append(path)

    b._api = api
    return b


@pytest.fixture
def data(tmp_path):
    d = (tmp_path / "data").resolve()
    for name in ("provcat-test-aaa", "provcat-test-bbb", "quickstart", "notmine"):
        (d / name).mkdir(parents=True)
        (d / name / "f").write_text("x")
    return d


def test_cleanup_deletes_only_its_own_folders(data):
    b = backend(data, ["provcat-test-aaa"])
    b.cleanup()
    assert b.deleted == ["/repositories/provcat-test-aaa"]
    assert not (data / "provcat-test-aaa").exists()
    for name in ("provcat-test-bbb", "quickstart", "notmine"):
        assert (data / name / "f").exists()


def test_an_org_prefix_is_its_own_rule(data):
    # With access control, the test repositories start with the org.
    (data / "acme-provcat-test-aaa").mkdir()
    b = backend(data, ["acme-provcat-test-aaa", "provcat-test-aaa"])
    b.prefix = "acme-provcat-test-"
    with pytest.raises(RuntimeError, match="'provcat-test-aaa' is not a name"):
        b.cleanup()
    assert not (data / "acme-provcat-test-aaa").exists()
    assert (data / "provcat-test-aaa" / "f").exists()


def test_a_prefix_must_end_with_provcat_test():
    with pytest.raises(ValueError, match="must end with provcat-test-"):
        LakefsBackend("http://x", "p", prefix="acme-")


def test_cleanup_without_a_data_folder_deletes_no_files(data):
    backend(None, ["provcat-test-aaa"]).cleanup()
    assert (data / "provcat-test-aaa").exists()


def test_a_failure_does_not_stop_the_rest(data, tmp_path):
    # Review of the cleanup, finding 1: try every name, then raise once.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    os.symlink(outside, data / "provcat-test-link")
    b = backend(data, ["provcat-test-link", "provcat-test-ccc", "provcat-test-aaa", "notmine", "x/../../y"],
                fail={"provcat-test-ccc"})
    with pytest.raises(RuntimeError) as e:
        b.cleanup()
    msg = str(e.value)
    assert "provcat-test-link is a link" in msg and "provcat-test-ccc was not deleted" in msg
    assert "'notmine' is not a name" in msg and "'x/../../y' is not a name" in msg
    assert not (data / "provcat-test-aaa").exists()  # the name after the failures was still cleaned
    assert (outside / "keep").exists() and (data / "notmine" / "f").exists()


def test_a_wrong_data_folder_says_so(tmp_path):
    # Finding 2: a data folder that does not exist is an error, not silence.
    b = backend(tmp_path / "nowhere", ["provcat-test-aaa"])
    with pytest.raises(RuntimeError, match="is not a folder"):
        b.cleanup()
    assert b.deleted == ["/repositories/provcat-test-aaa"]


def test_a_missing_repository_folder_says_so(data):
    # Finding 2: a folder missing after the DELETE points at a wrong path.
    with pytest.raises(RuntimeError, match="provcat-test-zzz is missing"):
        backend(data, ["provcat-test-zzz"]).cleanup()


def test_data_folder_values(tmp_path, monkeypatch):
    # Finding 12: the value from the environment.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert LakefsBackend.data_folder(None) is None
    assert LakefsBackend.data_folder("") is None
    assert LakefsBackend.data_folder("~/d") == (tmp_path / "d").resolve()
    assert LakefsBackend.data_folder("rel") == (tmp_path / "rel").resolve()


def test_cleanup_all_runs_every_backend(data):
    # Review of the isolation test, finding 1: B is cleaned up when A fails.
    from object_store_suite import cleanup_all

    a = backend(data, ["provcat-test-aaa"], fail={"provcat-test-aaa"})
    b = backend(data, ["provcat-test-bbb"])
    with pytest.raises(RuntimeError, match="provcat-test-aaa was not deleted"):
        cleanup_all(a, b)
    assert b.deleted == ["/repositories/provcat-test-bbb"]
    assert not (data / "provcat-test-bbb").exists()


def test_a_repository_that_was_never_made_is_skipped(data):
    # Finding 6: the name is recorded before the create; a 404 on delete is fine.
    import urllib.error

    b = backend(data, ["provcat-test-new"])

    def api(method, path, body=None):
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    b._api = api
    b.cleanup()  # no error, and nothing else is touched
    assert (data / "provcat-test-aaa").exists()
