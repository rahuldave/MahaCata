import os
import tarfile

import pytest

from provcat import Catalog, NotFound, OfflineMiss, UriError
from provcat import StoreError
from conftest import SHA_A, SHA_B


def cat(github, tmp_path, offline=False):
    cfg = {"stores": {"github": {"api_url": github.api_url, "token_env": ["NO_SUCH_TOKEN_VAR"], "use_gh_cli": False}}}
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


def test_default_branch_is_pinned_to_its_commit(github, tmp_path):
    c = cat(github, tmp_path)
    assert str(c.pin("github://owner/repo/README.md")) == f"github://owner/repo/README.md?at=commit:{SHA_B}"
    assert c.pin("github://owner/repo/README.md?at=tag:v1").selector.value == SHA_A
    assert c.pin("github://owner/repo?at=branch:feat/x").selector.value == SHA_A


def test_fetch_file_lands_under_the_commit(github, tmp_path):
    c = cat(github, tmp_path)
    p = c.fetch("github://owner/repo/src/app.py?at=branch:main")
    assert p == tmp_path / "cache" / "github" / "owner" / "repo" / SHA_B / "src" / "app.py"
    assert p.read_bytes() == b"print('B')\n"


def test_cached_file_costs_no_contents_call(github, tmp_path):
    c = cat(github, tmp_path)
    c.fetch("github://owner/repo/README.md?at=branch:main")
    before = github.hits["contents"]
    c.fetch("github://owner/repo/README.md?at=branch:main")
    assert github.hits["contents"] == before  # only the ref was pinned again


def test_full_sha_needs_no_network_when_cached(github, tmp_path):
    c = cat(github, tmp_path)
    uri = f"github://owner/repo/README.md?at=commit:{SHA_A}"
    c.fetch(uri)
    github.hits.clear()
    assert c.read(uri) == b"hello A\n"
    assert sum(github.hits.values()) == 0


def test_folder_comes_from_one_tarball(github, tmp_path):
    c = cat(github, tmp_path)
    p = c.fetch("github://owner/repo/src?at=tag:v1")
    assert sorted(str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()) == ["app.py", "lib/util.py"]
    assert github.hits["tarball"] == 1
    c.fetch("github://owner/repo/src?at=tag:v1")
    assert github.hits["tarball"] == 1


def test_whole_repository(github, tmp_path):
    p = cat(github, tmp_path).fetch(f"github://owner/repo?at=commit:{SHA_A}")
    assert (p / "src" / "lib" / "util.py").read_bytes() == b"u = 1\n"


def test_tarball_links_and_climbing_names_are_skipped(github, tmp_path):
    github.extra_tar_members = [("src/evil", b"/etc/passwd", tarfile.SYMTYPE)]
    p = cat(github, tmp_path).fetch(f"github://owner/repo/src?at=commit:{SHA_A}")
    assert not (p / "evil").exists()
    assert not os.path.lexists(p / "evil")


def test_tarball_names_that_climb_are_skipped_not_fatal(github, tmp_path):
    # Finding 12: a member such as src/../../evil.txt crashed the fetch.
    github.extra_tar_members = [("src/../../evil.txt", b"x", tarfile.REGTYPE), ("src/ok2.txt", b"k", tarfile.REGTYPE)]
    p = cat(github, tmp_path).fetch(f"github://owner/repo/src?at=commit:{SHA_A}")
    assert (p / "ok2.txt").read_bytes() == b"k"
    assert not any("evil" in str(f) for f in (tmp_path / "cache").rglob("*"))


def test_folder_with_case_twins_is_refused(github, tmp_path):
    # Second review, finding 1: Guide.md and guide.md became one file with the wrong bytes.
    github.commits[SHA_A]["docs/Guide.md"] = b"upper"
    github.commits[SHA_A]["docs/guide.md"] = b"lower"
    c = cat(github, tmp_path)
    with pytest.raises(StoreError, match="one name"):
        c.fetch(f"github://owner/repo/docs?at=commit:{SHA_A}")
    assert c.read(f"github://owner/repo/docs/Guide.md?at=commit:{SHA_A}") == b"upper"
    assert c.read(f"github://owner/repo/docs/guide.md?at=commit:{SHA_A}") == b"lower"
    assert c.read(f"github://owner/repo/docs/Guide.md?at=commit:{SHA_A}") == b"upper"


def test_copied_cache_still_serves_offline(github, tmp_path):
    # Second review, finding 3: a copy that dropped sub-second times missed every file.
    import os
    import shutil

    cat(github, tmp_path).fetch("github://owner/repo/README.md")
    copy = tmp_path / "copy"
    shutil.copytree(tmp_path / "cache", copy)
    for f in copy.rglob("*"):
        if f.is_file():
            st = f.stat()
            os.utime(f, ns=(st.st_atime_ns, (st.st_mtime_ns // 10**9) * 10**9 - 10**9))
    off = Catalog({"stores": {"github": {"api_url": github.api_url}}}, cache_root=copy, offline=True)
    assert off.read("github://owner/repo/README.md") == b"hello B\n"


def test_offline_needs_the_whole_cache_root(github, tmp_path):
    # Tour review, finding 1: a mount without .provcat serves nothing offline.
    import shutil

    cat(github, tmp_path).fetch("github://owner/repo/README.md")
    stores = {"stores": {"github": {"api_url": github.api_url}}}
    part = tmp_path / "part"
    shutil.copytree(tmp_path / "cache" / "github", part / "github")
    off = Catalog(stores, cache_root=part, offline=True)
    with pytest.raises(OfflineMiss):
        off.read("github://owner/repo/README.md")
    whole = tmp_path / "whole"
    shutil.copytree(tmp_path / "cache", whole)
    off = Catalog(stores, cache_root=whole, offline=True)
    assert off.read("github://owner/repo/README.md") == b"hello B\n"


def test_changed_cached_bytes_are_fetched_again(github, tmp_path):
    c = cat(github, tmp_path)
    p = c.fetch(f"github://owner/repo/README.md?at=commit:{SHA_A}")
    p.write_bytes(b"tampered")
    assert c.read(f"github://owner/repo/README.md?at=commit:{SHA_A}") == b"hello A\n"


def test_odd_replies_are_store_errors(github, tmp_path, monkeypatch):
    from provcat.stores.github import GithubStore

    monkeypatch.setattr(GithubStore, "_json", lambda self, uri, path: ["not", "a", "dict"])
    with pytest.raises(StoreError, match="unexpected shape"):
        cat(github, tmp_path).pin("github://owner/repo")


def test_ref_kind_is_enforced(github, tmp_path):
    # Finding 6: ?at=tag:main pinned the branch main.
    c = cat(github, tmp_path)
    assert c.pin("github://owner/repo?at=tag:main").selector.value == SHA_A
    assert c.pin("github://owner/repo?at=branch:main").selector.value == SHA_B
    with pytest.raises(NotFound):
        c.pin("github://owner/repo?at=branch:v1")
    assert c.pin("github://owner/repo?at=tag:v2").selector.value == SHA_A  # annotated tag
    assert c.pin(f"github://owner/repo?at=commit:{SHA_A[:7]}").selector.value == SHA_A


def test_refs_with_slashes_do_not_block_each_other(github, tmp_path):
    # Finding 6: pinning release/1.0 then release hit a file-versus-folder clash.
    github.heads["release/1.0"] = SHA_B
    c = cat(github, tmp_path)
    c.pin("github://owner/repo?at=branch:release/1.0")
    assert c.pin("github://owner/repo?at=branch:release").selector.value == SHA_A
    c.pin("github://owner/repo?at=tag:v1")
    github.heads["v1/x"] = SHA_B
    assert c.pin("github://owner/repo?at=branch:v1/x").selector.value == SHA_B


def test_offline_reads_a_file_inside_a_fetched_folder(github, tmp_path):
    # Finding 7: the file was on disk but offline mode missed it.
    c = cat(github, tmp_path)
    c.fetch("github://owner/repo/src?at=branch:main")
    github.hits.clear()
    assert c.read("github://owner/repo/src/app.py?at=branch:main") == b"print('B')\n"
    assert github.hits["contents"] == 0
    off = cat(github, tmp_path, offline=True)
    assert off.read("github://owner/repo/src/app.py?at=branch:main") == b"print('B')\n"
    assert off.stat("github://owner/repo/src/app.py?at=branch:main").kind == "file"


def test_parallel_folder_fetches_do_not_crash(github, tmp_path):
    # Finding 8: concurrent installs failed with "Directory not empty".
    from concurrent.futures import ThreadPoolExecutor

    uri = f"github://owner/repo/src?at=commit:{SHA_A}"
    with ThreadPoolExecutor(8) as pool:
        paths = list(pool.map(lambda _: cat(github, tmp_path).fetch(uri), range(8)))
    assert len(set(paths)) == 1
    assert (paths[0] / "app.py").read_bytes() == b"print('A')\n"


def test_token_is_not_sent_to_another_host(github, github2, tmp_path, monkeypatch):
    # Finding 11: urllib copied Authorization across a redirect to another host.
    monkeypatch.setenv("PROVCAT_TEST_TOKEN", "ghp_SECRET123")
    github.tarball_redirect = github2.api_url
    cfg = {"stores": {"github": {"api_url": github.api_url, "token_env": ["PROVCAT_TEST_TOKEN"], "use_gh_cli": False}}}
    p = Catalog(cfg, cache_root=tmp_path / "cache").fetch(f"github://owner/repo/src?at=commit:{SHA_A}")
    assert (p / "app.py").exists()
    assert any(h.get("Authorization") == "Bearer ghp_SECRET123" for h in github.headers_seen)
    assert github2.headers_seen and not any("Authorization" in h for h in github2.headers_seen)


def test_offline_uses_the_last_pinned_commit(github, tmp_path):
    cat(github, tmp_path).fetch("github://owner/repo/README.md")
    github.hits.clear()
    off = cat(github, tmp_path, offline=True)
    assert off.read("github://owner/repo/README.md") == b"hello B\n"
    assert sum(github.hits.values()) == 0
    with pytest.raises(OfflineMiss):
        off.fetch("github://owner/repo/README.md?at=tag:v1")


def test_stat_and_list(github, tmp_path):
    c = cat(github, tmp_path)
    assert c.stat("github://owner/repo/src?at=tag:v1").kind == "folder"
    e = c.stat("github://owner/repo/README.md?at=tag:v1")
    assert (e.kind, e.size, e.version) == ("file", 8, SHA_A)
    names = [(str(x.uri.without_selector()), x.kind) for x in c.list("github://owner/repo/src?at=tag:v1")]
    assert names == [("github://owner/repo/src/app.py", "file"), ("github://owner/repo/src/lib", "folder")]


def test_missing_things(github, tmp_path):
    c = cat(github, tmp_path)
    with pytest.raises(NotFound):
        c.fetch("github://owner/repo/nope.txt")
    with pytest.raises(NotFound):
        c.fetch("github://owner/repo/README.md?at=branch:nope")
    with pytest.raises(UriError):
        c.fetch("github://owner")
    with pytest.raises(UriError):
        c.fetch("github:///repo/x")
    with pytest.raises(UriError):
        c.fetch("github://owner/repo/x?at=version:3")
