"""Catalog.open_file, and the fsspec file system over the catalog."""
import json

import fsspec
import pytest
from fsspec.registry import _registry

from provcat import Catalog, hooks
from provcat.errors import ConfigError, UriError
from provcat.fs import CatalogFileSystem
from provcat.hooks import AccessDenied
from provcat.stores import register_driver
from provcat.stores.local import LocalStore
from provcat.uri import Selector


@register_driver
class MovingStore(LocalStore):
    """A file store with a branch that moves: pin gives the current version,
    and fetch of a version gives the bytes of that version."""

    driver = "movingtest"
    versions = {}  # version -> bytes
    head = None

    def pin(self, uri, cache):
        if uri.selector is not None:
            return uri
        return uri.with_selector(Selector("commit", type(self).head))

    def fetch(self, uri, cache):
        out = cache.root / "moving" / uri.selector.value
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(type(self).versions[uri.selector.value])
        return out


SCHEMES = {"dd": {"parts": ["name"], "rest": "path", "target": "files:///{name}/{path}"}}


@pytest.fixture
def cat(tmp_path):
    root = tmp_path / "root"
    (root / "ds" / "sub").mkdir(parents=True)
    (root / "ds" / "a.csv").write_bytes(b"0123456789")
    (root / "ds" / "sub" / "b.csv").write_bytes(b"B")
    yield Catalog({"stores": {"files": {"driver": "file", "root": str(root)}}, "schemes": SCHEMES},
                  cache_root=tmp_path / "cache")
    for s in list(_registry):
        if isinstance(_registry[s], type) and issubclass(_registry[s], CatalogFileSystem):
            del _registry[s]


SEEN = []


def rec(ctx, req, next):
    out = next(req)
    SEEN.append((req.spot, str(req.physical) if req.physical else None,
                 {k: v for k, v in req.args.items() if k != "metadata"}))
    return out


class EP:
    def __init__(self, fn):
        self.value, self.fn = "tests:x", fn

    def load(self):
        return self.fn


@pytest.fixture
def hooked(tmp_path, monkeypatch):
    """Open catalogs as app nb with the given hooks at every spot."""
    SEEN.clear()

    def use(*fns):
        names = [f"t{i}" for i in range(len(fns))]
        monkeypatch.setattr(hooks, "_registered", lambda: {n: EP(f) for n, f in zip(names, fns)})
        f = tmp_path / "hooks.yaml"
        f.write_text(json.dumps({"apps": {"nb": {"*": names}}}))
        monkeypatch.setenv("PROVCAT_HOOKS", str(f))
        monkeypatch.setenv("PROVCAT_APP", "nb")
        hooks._forget_process_app()

    return use


def test_a_read_pins_at_open_and_records_its_ranges(hooked, cat, tmp_path):
    hooked(rec)
    c = Catalog(cat.config, cache_root=tmp_path / "c2")
    SEEN.clear()
    with c.open_file("dd://ds/a.csv") as f:
        f.seek(2)
        assert f.read(3) == b"234"
        f.seek(0)
        assert f.read(4) == b"0123"
        assert f.read() == b"456789"
        assert str(f.pinned) == "files:///ds/a.csv"
    spots = [s for s, *_ in SEEN]
    assert spots[:4] == ["resolve", "pin", "fetch", "open"]
    assert spots[4:-1] == ["read_block"] * (len(spots) - 5) and spots[-1] == "close"
    assert SEEN[-1][2] == {"mode": "rb", "ranges": [(0, 10)]}
    assert SEEN[4][2] == {"offset": 2, "length": 3}


def test_a_read_keeps_the_version_of_its_open(tmp_path):
    MovingStore.versions = {"v1": b"one", "v2": b"two"}
    MovingStore.head = "v1"
    c = Catalog({"stores": {"m": {"driver": "movingtest", "root": str(tmp_path)}}}, cache_root=tmp_path / "c")
    f = c.open_file("m:///x")
    MovingStore.head = "v2"
    assert f.read() == b"one" and f.pinned.selector == Selector("commit", "v1")
    f.close()
    assert c.open_file("m:///x").read() == b"two"


def test_a_folder_or_a_bad_mode_is_refused(cat):
    with pytest.raises(IsADirectoryError):
        cat.open_file("dd://ds")
    with pytest.raises(ValueError, match="rb or wb"):
        cat.open_file("dd://ds/a.csv", "r+b")


def test_a_write_reaches_the_store_at_close(hooked, cat, tmp_path):
    hooked(rec)
    c = Catalog(cat.config, cache_root=tmp_path / "c2")
    SEEN.clear()
    with c.open_file("dd://ds/new.csv", "wb") as f:
        f.write(b"he")
        f.write(b"llo")
        assert not (tmp_path / "root" / "ds" / "new.csv").exists()
    assert (tmp_path / "root" / "ds" / "new.csv").read_bytes() == b"hello"
    assert [s for s, *_ in SEEN] == ["resolve", "open", "put", "close"]
    assert f.entries[0].size == 5


def test_a_write_that_fails_puts_nothing(cat, tmp_path):
    with pytest.raises(KeyError):
        with cat.open_file("dd://ds/new.csv", "wb") as f:
            f.write(b"half")
            raise KeyError("stop")
    assert not (tmp_path / "root" / "ds" / "new.csv").exists()


def test_a_write_to_a_fixed_version_is_refused(cat):
    with pytest.raises(UriError, match="fixed version"):
        cat.open_file("dd://ds/a.csv?at=commit:abc", "wb")


def test_gates_stop_an_open_a_block_and_a_write(hooked, cat, tmp_path):
    def gate(ctx, req, next):
        if req.spot == "read_block" and req.args["offset"] >= 5:
            raise AccessDenied("not past 5")
        if req.spot == "put":
            raise AccessDenied("no writes")
        return next(req)

    hooked(gate, hooks.logical_only)
    c = Catalog(cat.config, cache_root=tmp_path / "c2")
    with pytest.raises(AccessDenied, match="only logical"):
        c.open_file("files:///ds/a.csv")
    with c.open_file("dd://ds/a.csv") as f:
        assert f.read(5) == b"01234"
        with pytest.raises(AccessDenied, match="not past 5"):
            f.read(1)
    with pytest.raises(AccessDenied, match="no writes"):
        with c.open_file("dd://ds/new.csv", "wb") as w:
            w.write(b"x")
    assert not (tmp_path / "root" / "ds" / "new.csv").exists()


def test_the_file_system_lists_and_reads_logical_uris(cat):
    fs = cat.fs()
    assert fs.ls("dd://ds", detail=False) == ["dd://ds/a.csv", "dd://ds/sub"]
    assert fs.ls("dd://ds/a.csv", detail=False) == ["dd://ds/a.csv"]
    info = fs.info("dd://ds/a.csv")
    assert info["size"] == 10 and info["type"] == "file" and info["uri"] == "files:///ds/a.csv"
    assert fs.isdir("dd://ds/sub") and fs.isfile("dd://ds/sub/b.csv")
    assert fs.find("dd://ds") == ["dd://ds/a.csv", "dd://ds/sub/b.csv"]
    assert fs.cat("dd://ds/a.csv") == b"0123456789"
    assert not fs.exists("dd://ds/nope.csv")
    with pytest.raises(FileNotFoundError):
        fs.info("dd://ds/nope.csv")
    with pytest.raises(FileNotFoundError):
        fs.open("dd://ds/nope.csv")


def test_fsspec_open_takes_a_logical_uri_after_fs(cat, tmp_path):
    cat.fs()
    with fsspec.open("dd://ds/a.csv", "rt") as f:
        assert f.read() == "0123456789"
    with fsspec.open("dd://ds/out.txt", "wt") as f:
        f.write("written")
    assert (tmp_path / "root" / "ds" / "out.txt").read_text() == "written"
    fs, path = fsspec.url_to_fs("dd://ds/out.txt")
    assert fs.catalog is cat and path == "dd://ds/out.txt"
    fs.rm("dd://ds/out.txt")
    assert not (tmp_path / "root" / "ds" / "out.txt").exists()


def test_register_false_leaves_fsspec_alone(cat):
    fs = cat.fs(register=False)
    assert "dd" not in _registry and fs.cat("dd://ds/sub/b.csv") == b"B"


def test_a_physical_uri_works_on_the_object_but_is_not_registered(cat):
    fs = cat.fs()
    assert fs.cat("files:///ds/sub/b.csv") == b"B"
    assert "files" not in _registry


def test_a_scheme_that_fsspec_knows_is_refused(tmp_path):
    c = Catalog({"stores": {"files": {"driver": "file", "root": str(tmp_path)}},
                 "schemes": {"memory": {"parts": ["name"], "target": "files:///{name}"}}},
                cache_root=tmp_path / "c")
    with pytest.raises(ConfigError, match="'memory' is already an fsspec protocol"):
        c.fs()
    assert c.fs(register=False).catalog is c


def test_the_last_catalog_to_register_serves_the_scheme(cat, tmp_path):
    cat.fs()
    other = Catalog(cat.config, cache_root=tmp_path / "other")
    other.fs()
    assert fsspec.url_to_fs("dd://ds/a.csv")[0].catalog is other


def test_a_writer_dropped_without_close_puts_nothing(cat, tmp_path):
    import gc

    f = cat.open_file("dd://ds/dropped.csv", "wb")
    f.write(b"partial")
    del f
    gc.collect()
    assert not (tmp_path / "root" / "ds" / "dropped.csv").exists()


def test_a_refusal_after_open_drops_the_handle_unseen(hooked, cat, tmp_path):
    import gc

    def late(ctx, req, next):
        out = next(req)
        if req.spot == "open":
            raise AccessDenied("post-check refused")
        return out

    hooked(rec, late)
    c = Catalog(cat.config, cache_root=tmp_path / "c2")
    for mode in ("wb", "rb"):
        with pytest.raises(AccessDenied):
            c.open_file("dd://ds/a.csv" if mode == "rb" else "dd://ds/never.csv", mode)
    gc.collect()
    assert not (tmp_path / "root" / "ds" / "never.csv").exists()
    assert "close" not in [s for s, *_ in SEEN] and "put" not in [s for s, *_ in SEEN]


def test_lines_and_whole_reads_run_one_hook_each(hooked, tmp_path):
    hooked(rec)
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "l.txt").write_bytes(b"one\ntwo\nthree")
    c = Catalog({"stores": {"files": {"driver": "file", "root": str(tmp_path / "root")}}},
                cache_root=tmp_path / "c")
    with c.open_file("files:///l.txt") as f:
        assert list(f) == [b"one\n", b"two\n", b"three"]
        f.seek(0)
        assert f.readline(2) == b"on"
        f.seek(0)
        assert f.read() == b"one\ntwo\nthree"
        assert f.ranges == [(0, 13)]
    blocks = [a for s, _, a in SEEN if s == "read_block"]
    assert [b["offset"] for b in blocks] == [0, 4, 8, 13, 0, 0]


def test_a_read_never_goes_past_the_size_at_open(tmp_path):
    (tmp_path / "f").write_bytes(b"0123456789")
    c = Catalog(cache_root=tmp_path / "c")
    with c.open_file(f"file://{tmp_path}/f") as f:
        with (tmp_path / "f").open("ab") as more:
            more.write(b"EXTRA")
        f.seek(6)
        buf = bytearray(100)
        assert f.readinto(buf) == 4 and bytes(buf[:4]) == b"6789"
        assert f.read() == b"" and f.ranges == [(6, 10)]


@pytest.mark.filterwarnings("error")
def test_a_handle_that_fails_to_open_leaves_nothing(tmp_path):
    import gc

    from provcat.files import CatalogReader
    from provcat.uri import parse

    with pytest.raises(FileNotFoundError):
        CatalogReader(parse("file:///x"), parse("file:///x"), tmp_path / "nope", None, None)
    gc.collect()


def test_a_transaction_puts_only_when_it_ends_well(cat, tmp_path):
    fs = cat.fs()
    with pytest.raises(KeyError):
        with fs.transaction:
            with fs.open("dd://ds/t1.csv", "wb") as f:
                f.write(b"1")
            raise KeyError("stop")
    assert not (tmp_path / "root" / "ds" / "t1.csv").exists()
    with fs.transaction:
        with fs.open("dd://ds/t2.csv", "wb") as f:
            f.write(b"2")
        assert not (tmp_path / "root" / "ds" / "t2.csv").exists()
    assert (tmp_path / "root" / "ds" / "t2.csv").read_bytes() == b"2"


def test_copy_and_a_recursive_rm(cat, tmp_path):
    fs = cat.fs()
    fs.copy("dd://ds/a.csv", "dd://ds/sub/copy.csv")
    assert (tmp_path / "root" / "ds" / "sub" / "copy.csv").read_bytes() == b"0123456789"
    fs.rm("dd://ds/sub", recursive=True)
    assert not list((tmp_path / "root" / "ds" / "sub").rglob("*.csv"))


def test_a_path_with_a_selector_is_not_a_pattern(tmp_path):
    MovingStore.versions = {"v1": b"one", "feature/x": b"fx"}
    MovingStore.head = "v1"
    c = Catalog({"stores": {"m": {"driver": "movingtest", "root": str(tmp_path)}}}, cache_root=tmp_path / "c")
    fs = c.fs(register=False)
    assert fs.cat("m:///x?at=commit:v1") == b"one"
    assert fs.cat_file("m:///x?at=commit:feature%2Fx") == b"fx"
    assert fs._parent("m:///d/x?at=branch:feature/x") == "m:///d?at=branch:feature%2Fx"
