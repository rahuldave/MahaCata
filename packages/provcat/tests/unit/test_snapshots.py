"""Read-only snapshots named by their content."""
import hashlib
import os

import pytest

from provcat import Catalog
from provcat.cache import snapshot, tree_digest
from provcat.errors import StoreError


def make_folder(root):
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("A")
    (root / "sub" / "b.txt").write_text("B")
    return root


def test_file_snapshot_is_read_only_and_named_by_content(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("one")
    c = Catalog(cache_root=tmp_path / "cache")
    snap = c.snapshot(f"file://{src}")
    assert snap.kind == "file" and snap.path.name == "doc.txt"
    assert snap.digest == hashlib.sha256(b"one").hexdigest()
    assert snap.path.parent.name == snap.digest
    assert snap.path.read_text() == "one"
    assert not os.access(snap.path, os.W_OK)


def test_a_change_to_the_source_does_not_change_the_snapshot(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("one")
    c = Catalog(cache_root=tmp_path / "cache")
    first = c.snapshot(f"file://{src}")
    src.write_text("two")
    second = c.snapshot(f"file://{src}")
    assert first.path.read_text() == "one"
    assert second.path.read_text() == "two"
    assert first.digest != second.digest


def test_same_content_shares_one_snapshot(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("same")
    c = Catalog(cache_root=tmp_path / "cache")
    assert c.snapshot(f"file://{src}").path == c.snapshot(f"file://{src}").path
    assert not list((tmp_path / "cache" / ".provcat" / "tmp").iterdir())


def test_folder_snapshot(tmp_path):
    src = make_folder(tmp_path / "data")
    c = Catalog(cache_root=tmp_path / "cache")
    snap = c.snapshot(f"file://{src}")
    assert snap.kind == "folder" and snap.digest == tree_digest(src)
    assert (snap.path / "sub" / "b.txt").read_text() == "B"
    assert not os.access(snap.path, os.W_OK)
    assert not os.access(snap.path / "sub", os.W_OK)
    with pytest.raises(PermissionError):
        (snap.path / "new.txt").write_text("x")
    (src / "sub" / "b.txt").write_text("changed")
    assert (snap.path / "sub" / "b.txt").read_text() == "B"


def test_folder_digest_covers_names_and_bytes(tmp_path):
    a = make_folder(tmp_path / "a")
    b = make_folder(tmp_path / "b")
    assert tree_digest(a) == tree_digest(b)
    (b / "sub" / "b.txt").rename(b / "sub" / "c.txt")
    assert tree_digest(a) != tree_digest(b)


def test_a_link_is_refused(tmp_path):
    src = make_folder(tmp_path / "data")
    (src / "link").symlink_to(src / "a.txt")
    c = Catalog(cache_root=tmp_path / "cache")
    with pytest.raises(StoreError, match="plain"):
        c.snapshot(f"file://{src}")


def test_a_bad_name_is_refused(tmp_path):
    from provcat import Catalog as C
    src = tmp_path / "doc.txt"
    src.write_text("x")
    with pytest.raises(Exception):
        snapshot(C(cache_root=tmp_path / "cache").cache, src, "..")


# Findings of the adversarial review

def test_a_file_never_stands_for_a_folder_with_the_same_digest(tmp_path):
    from provcat.cache import _sha256
    folder = make_folder(tmp_path / "data")
    forged = tmp_path / "forged"
    forged.mkdir()
    lines = b"provcat-tree-v1\n" + b"".join(
        (f"D\0{r}\0\n" if (folder / r).is_dir() else f"F\0{r}\0{_sha256(folder / r)}\n").encode()
        for r in sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")))
    (forged / "data").write_bytes(lines)
    c = Catalog(cache_root=tmp_path / "cache")
    as_file = c.snapshot(f"file://{forged}/data")
    as_folder = c.snapshot(f"file://{folder}")
    assert as_file.digest == as_folder.digest  # the same bytes hash the same...
    assert as_folder.kind == "folder" and as_folder.path.is_dir()  # ...but the kinds stay apart
    assert as_file.path != as_folder.path


def test_an_empty_file_and_an_empty_folder_stay_apart(tmp_path):
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "x").write_bytes(b"")
    (tmp_path / "d" / "x").mkdir(parents=True)
    c = Catalog(cache_root=tmp_path / "cache")
    assert c.snapshot(f"file://{tmp_path}/f/x").kind == "file"
    assert c.snapshot(f"file://{tmp_path}/d/x").path.is_dir()


def test_an_empty_folder_changes_the_digest(tmp_path):
    a = make_folder(tmp_path / "a")
    b = make_folder(tmp_path / "b")
    (b / "empty").mkdir()
    assert tree_digest(a) != tree_digest(b)
    c = Catalog(cache_root=tmp_path / "cache")
    c.snapshot(f"file://{a}")
    assert (c.snapshot(f"file://{b}").path / "empty").is_dir()


def test_a_changed_snapshot_is_replaced_before_it_is_used_again(tmp_path):
    src = tmp_path / "doc.txt"
    src.write_text("Revenue was 42 million.")
    c = Catalog(cache_root=tmp_path / "cache")
    first = c.snapshot(f"file://{src}")
    first.path.chmod(0o644)
    first.path.write_text("Revenue was 1 million.")
    again = c.snapshot(f"file://{src}")
    assert again.path == first.path
    assert again.path.read_text() == "Revenue was 42 million."
    assert tree_digest(again.path) == again.digest


def test_many_snapshots_of_one_folder_at_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    src = make_folder(tmp_path / "data")
    c = Catalog(cache_root=tmp_path / "cache")
    with ThreadPoolExecutor(16) as pool:
        snaps = list(pool.map(lambda _: c.snapshot(f"file://{src}"), range(32)))
    assert len({s.path for s in snaps}) == 1
    assert tree_digest(snaps[0].path) == snaps[0].digest
