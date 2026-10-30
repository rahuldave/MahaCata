"""Writes to local disk, and the write commands of provc."""
import json
import os

import pytest

from provcat import Catalog, NotFound, UriError
from provcat.cli import main
from provcat.errors import StoreError


def test_put_reads_back_and_delete(tmp_path):
    c = Catalog(cache_root=tmp_path / "cache")
    uri = f"file://{tmp_path}/out/deep/f.txt"
    [entry] = c.put(uri, b"hello")
    assert entry.kind == "file" and entry.size == 5
    assert (tmp_path / "out" / "deep" / "f.txt").read_bytes() == b"hello"
    c.put(uri, b"again")
    assert c.read(uri) == b"again"
    assert [p.name for p in (tmp_path / "out" / "deep").iterdir()] == ["f.txt"]  # no temporary file left
    c.delete(uri)
    assert not (tmp_path / "out" / "deep" / "f.txt").exists()
    with pytest.raises(NotFound):
        c.delete(uri)


def test_put_a_folder(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a").write_text("A")
    (src / "sub" / "b").write_text("B")
    c = Catalog(cache_root=tmp_path / "cache")
    written = c.put(f"file://{tmp_path}/dst", src)
    assert sorted(e.uri.segments[-1] for e in written) == ["a", "b"]
    assert (tmp_path / "dst" / "sub" / "b").read_text() == "B"


def test_folders_are_not_written_over_or_deleted(tmp_path):
    (tmp_path / "d").mkdir()
    c = Catalog(cache_root=tmp_path / "cache")
    with pytest.raises(StoreError, match="is a folder"):
        c.put(f"file://{tmp_path}/d", b"x")
    with pytest.raises(StoreError, match="is a folder"):
        c.delete(f"file://{tmp_path}/d")
    with pytest.raises(FileNotFoundError):
        c.put(f"file://{tmp_path}/x", tmp_path / "nosuch")


def test_root_confines_writes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    c = Catalog({"stores": {"data": {"driver": "file", "root": str(root)}}}, cache_root=tmp_path / "cache")
    c.put("data:///in/f.txt", b"ok")
    assert (root / "in" / "f.txt").read_bytes() == b"ok"
    with pytest.raises(UriError, match="leaves the store root"):
        c.put("data:///link/f.txt", b"no")
    assert not (outside / "f.txt").exists()


def test_a_failed_write_leaves_no_temporary_file(tmp_path, monkeypatch):
    import provcat.stores.local as local

    def broken(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(local.shutil, "copyfileobj", broken)
    c = Catalog(cache_root=tmp_path / "cache")
    with pytest.raises(StoreError, match="No space left"):
        c.put(f"file://{tmp_path}/f.txt", b"x")
    assert list(tmp_path.iterdir()) == [tmp_path / "cache"] or list(tmp_path.iterdir()) == []


def test_cli_put_rm_and_stdin(tmp_path, capsys, monkeypatch):
    src = tmp_path / "src.txt"
    src.write_text("S")
    assert main(["put", f"file://{tmp_path}/a.txt", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["size"] == 1
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"from stdin")))
    assert main(["put", f"file://{tmp_path}/b.txt", "-"]) == 0
    assert (tmp_path / "b.txt").read_bytes() == b"from stdin"
    capsys.readouterr()
    assert main(["rm", f"file://{tmp_path}/a.txt"]) == 0
    assert not (tmp_path / "a.txt").exists()


def test_cli_commit_needs_key_value_metadata_and_a_store_with_commits(tmp_path, capsys):
    assert main(["commit", f"file://{tmp_path}/x", "-m", "m", "--meta", "novalue"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err
    assert main(["commit", f"file://{tmp_path}/x", "-m", "m"]) == 1
    assert "has no commits" in capsys.readouterr().err
    assert main(["branch", f"file://{tmp_path}/x", "--from", "main"]) == 1
    assert "has no branches" in capsys.readouterr().err


def test_github_is_read_only(tmp_path):
    with pytest.raises(StoreError, match="cannot be written"):
        Catalog(cache_root=tmp_path / "cache").put("github://o/r/f.txt?at=branch:main", b"x")


# Review of the write feature


def test_a_scheme_that_drops_the_branch_refuses_the_write(tmp_path):
    # Finding 1: ?at=branch:dev on a target without {ref} wrote to the default.
    c = Catalog({"stores": {"data": {"driver": "file", "root": str(tmp_path)}},
                 "schemes": {"t": {"parts": ["team"], "rest": "path", "target": "data:///{team}/{path}"}}},
                cache_root=tmp_path / "cache")
    c.put("t://ml/a.csv", b"default")  # no selector: fine
    with pytest.raises(UriError, match="does not use the branch"):
        c.put("t://ml/a.csv?at=branch:dev", b"dev")
    assert (tmp_path / "ml" / "a.csv").read_bytes() == b"default"


def test_put_keeps_the_mode_of_the_file_it_replaces(tmp_path):
    # Finding 2: the file came out 0600.
    c = Catalog(cache_root=tmp_path / "cache")
    old = os.umask(0o022)
    try:
        c.put(f"file://{tmp_path}/new.txt", b"n")
        assert (tmp_path / "new.txt").stat().st_mode & 0o777 == 0o644
        (tmp_path / "shared.txt").write_text("s")
        (tmp_path / "shared.txt").chmod(0o664)
        c.put(f"file://{tmp_path}/shared.txt", b"t")
        assert (tmp_path / "shared.txt").stat().st_mode & 0o777 == 0o664
    finally:
        os.umask(old)


def test_put_refuses_to_write_over_a_link(tmp_path):
    (tmp_path / "target.txt").write_text("t")
    os.symlink(tmp_path / "target.txt", tmp_path / "ln")
    with pytest.raises(StoreError, match="is a link"):
        Catalog(cache_root=tmp_path / "cache").put(f"file://{tmp_path}/ln", b"x")
    assert (tmp_path / "ln").is_symlink() and (tmp_path / "target.txt").read_text() == "t"


def test_a_folder_put_checks_every_name_first(tmp_path):
    # Finding 3: a bad name stopped the upload half way.
    src = tmp_path / "src"
    src.mkdir()
    for name in ("a", "b\\c", "z"):
        (src / name).write_text(name)
    with pytest.raises(UriError):
        Catalog(cache_root=tmp_path / "cache").put(f"file://{tmp_path}/dst", src)
    assert not (tmp_path / "dst").exists()
