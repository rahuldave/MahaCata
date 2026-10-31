"""Commits on a versioned bucket, against a bucket kept in memory, so that the
races a real bucket cannot stage on demand run here: files that change while
a commit lists them, and two commits that want the same number."""
import hashlib
import json

import pytest

from provcat import Catalog, NotFound
from provcat.errors import StoreError, UriError
from provcat.stores import register_driver
from provcat.stores.objects import Exists, ObjectStore


@register_driver
class MemoryStore(ObjectStore):
    """mem://bucket/key, with a version for every write and a marker for every delete."""

    driver = "memtest"
    buckets: dict = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.n = 0
        self.before_history = None  # called before each listing of every version
        self.before_create = None  # called with the key before each create

    # test helpers
    def write(self, bucket, key, data):
        self.n += 1
        self.buckets.setdefault(bucket, {}).setdefault(key, []).append((f"v{self.n}", data))

    def remove(self, bucket, key):
        self.n += 1
        self.buckets[bucket][key].append((f"v{self.n}", None))

    def _current(self, bucket):
        return {k: v[-1] for k, v in self.buckets.get(bucket, {}).items() if v[-1][1] is not None}

    # primitives
    def locate(self, uri):
        if uri.selector is not None:
            self._selector(uri)
        return uri.authority, uri.path

    def _head(self, uri, bucket, key):
        cur = self._current(bucket).get(key)
        if cur is None:
            raise NotFound(f"{uri}: not found")
        return len(cur[1]), cur[0]

    def _pages(self, uri, bucket, prefix, delimiter, limit=None):
        folders, objects = set(), []
        for k, (vid, data) in sorted(self._current(bucket).items()):
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if delimiter and "/" in rest:
                folders.add(prefix + rest.split("/")[0] + "/")
            else:
                objects.append((k, len(data), vid))
        yield sorted(folders)[:limit] if limit else sorted(folders), objects[:limit] if limit else objects

    def _get(self, uri, bucket, key, out):
        size, vid = self._head(uri, bucket, key)
        out.write(self._current(bucket)[key][1])
        return vid

    def _put(self, uri, bucket, key, src):
        self.write(bucket, key, src.read_bytes())
        return f"v{self.n}"

    def _delete(self, uri, bucket, key):
        self._head(uri, bucket, key)
        self.remove(bucket, key)

    def _version_of(self, uri, bucket, key):
        return self._head(uri, bucket, key)[1]

    def _history(self, uri, bucket, prefix):
        if self.before_history:
            self.before_history()
        out = []
        for k, versions in self.buckets.get(bucket, {}).items():
            if k.startswith(prefix):
                for i, (vid, data) in enumerate(versions):
                    out.append((k, vid, None if data is None else len(data), i == len(versions) - 1))
        return out

    def _find(self, uri, bucket, key, version):
        for vid, data in self.buckets.get(bucket, {}).get(key, []):
            if vid == version and data is not None:
                return data
        raise NotFound(f"{uri}: no version {version}")

    def _head_version(self, uri, bucket, key, version):
        return len(self._find(uri, bucket, key, version))

    def _get_version(self, uri, bucket, key, version, out):
        out.write(self._find(uri, bucket, key, version))

    def _create(self, uri, bucket, key, src):
        if self.before_create:
            self.before_create(key)
        if key in self._current(bucket):
            raise Exists(f"{uri}: already there")
        self._put(uri, bucket, key, src)


@pytest.fixture
def cat(tmp_path):
    MemoryStore.buckets = {}
    c = Catalog({"stores": {"mem": {"driver": "memtest"}}}, cache_root=tmp_path / "cache")
    store = c.stores["mem"]
    store.write("b", "main/t/a.csv", b"A1")
    store.write("b", "main/t/sub/b.csv", b"B1")
    store.write("b", "main/other.csv", b"O")
    return c


def manifest(cat, pinned):
    data = cat.stores["mem"]._current("b")[f".provcat/commits/{pinned.selector.value}.json"][1]
    assert hashlib.sha256(data).hexdigest() == pinned.selector.value
    return json.loads(data)


def test_commits_are_numbered_and_name_their_parent(cat):
    first = cat.commit("mem://b/main/t", "first")
    cat.stores["mem"].write("b", "main/t/a.csv", b"A2")
    second = cat.commit("mem://b/main/t", "second")
    m1, m2 = manifest(cat, first), manifest(cat, second)
    assert (m1["number"], m1["parent"]) == (1, None)
    assert (m2["number"], m2["parent"]) == (2, first.selector.value)
    assert sorted(m2["files"]) == ["a.csv", "sub/b.csv"]
    assert cat.read("mem://b/main/t/a.csv?at=" + str(first.selector)) == b"A1"
    assert cat.pin("mem://b/main/t") == second


def test_a_listing_that_changes_is_taken_again(cat):
    store = cat.stores["mem"]
    calls = []

    def change():
        calls.append(1)
        if len(calls) == 2:  # between the first two listings
            store.write("b", "main/t/a.csv", b"A2")

    store.before_history = change
    pinned = cat.commit("mem://b/main/t", "m")
    assert len(calls) == 4  # two that differ, then two that match
    assert cat.read(f"mem://b/main/t/a.csv?at={pinned.selector}") == b"A2"


def test_a_listing_that_never_settles_fails(cat):
    store = cat.stores["mem"]
    store.before_history = lambda: store.write("b", "main/t/a.csv", b"again")
    with pytest.raises(StoreError, match="kept changing"):
        cat.commit("mem://b/main/t", "m")
    assert ".provcat/log" not in " ".join(store._current("b"))


def test_a_commit_that_loses_the_number_takes_the_next(cat):
    store = cat.stores["mem"]
    other = {}

    def race(key):
        if key.startswith(".provcat/log/") and not other:
            # Another writer commits first, with a change of its own.
            store.before_create = None
            store.write("b", "main/t/c.csv", b"C")
            other["uri"] = cat.commit("mem://b/main/t", "theirs")
            store.write("b", "main/t/d.csv", b"D")  # after their commit
            store.before_create = race

    store.before_create = race
    mine = cat.commit("mem://b/main/t", "mine")
    m = manifest(cat, mine)
    assert m["number"] == 2 and m["parent"] == other["uri"].selector.value
    assert {"c.csv", "d.csv"} <= set(m["files"])  # listed again after the loss
    assert cat.pin("mem://b/main/t") == mine


def test_a_commit_that_loses_to_the_same_files_has_nothing_to_commit(cat):
    store = cat.stores["mem"]

    def race(key):
        if key.startswith(".provcat/log/"):
            store.before_create = None
            cat.commit("mem://b/main/t", "theirs")

    store.before_create = race
    with pytest.raises(StoreError, match="nothing to commit"):
        cat.commit("mem://b/main/t", "mine")


def test_pin_takes_the_newest_commit_of_the_folder_or_above(cat):
    t = cat.commit("mem://b/main/t", "t")
    top = cat.commit("mem://b/main", "main")
    assert cat.pin("mem://b/main/t").selector == top.selector
    assert cat.pin("mem://b/main/t/sub") == top.child("t").child("sub")
    cat.stores["mem"].write("b", "main/t/a.csv", b"A2")
    newer = cat.commit("mem://b/main/t", "t again")
    assert cat.pin("mem://b/main/t") == newer
    assert t != newer


def test_a_damaged_log_is_named(cat):
    cat.commit("mem://b/main/t", "first")
    store = cat.stores["mem"]
    log = next(k for k in store._current("b") if k.startswith(".provcat/log/"))
    store.write("b", log, b"not an id")
    with pytest.raises(StoreError, match="damaged"):
        cat.pin("mem://b/main/t")


def test_reads_at_a_commit_only_inside_its_folder(cat):
    pinned = cat.commit("mem://b/main/t", "first")
    with pytest.raises(NotFound, match="does not hold"):
        cat.stat(f"mem://b/main/other.csv?at={pinned.selector}")
    with pytest.raises(NotFound):
        cat.read(f"mem://b/main/t/nope.csv?at={pinned.selector}")
    with pytest.raises(UriError, match="64 hex"):
        cat.stat("mem://b/main/t?at=commit:" + "A" * 64)


def test_a_commit_of_files_whose_versions_expired_fails_on_read(cat):
    pinned = cat.commit("mem://b/main/t", "first")
    cat.stores["mem"].buckets["b"]["main/t/a.csv"][0] = ("gone", None)  # as a lifecycle rule would
    with pytest.raises(NotFound):
        cat.read(f"mem://b/main/t/a.csv?at={pinned.selector}")


def test_the_cli_commits_pins_and_branches(cat, tmp_path, capsys, monkeypatch):
    from provcat import cli

    cfg = tmp_path / "provcat.yaml"
    cfg.write_text("stores:\n  mem:\n    driver: memtest\n")
    args = ["--config", str(cfg), "--cache", str(tmp_path / "c2")]
    assert cli.main(args + ["commit", "mem://b/main/t", "-m", "first", "--meta", "run=1"]) == 0
    pinned = capsys.readouterr().out.strip()
    assert "?at=commit:" in pinned
    assert cli.main(args + ["pin", "mem://b/main/t"]) == 0
    assert capsys.readouterr().out.strip() == pinned
    assert cli.main(args + ["branch", "mem://b/dev", "--from", "main"]) == 0
    capsys.readouterr()
    assert cli.main(args + ["cat", "mem://b/dev/t/a.csv"]) == 0
    assert capsys.readouterr().out == "A1"


def test_pin_finds_a_deleted_path_in_a_commit(cat):
    first = cat.commit("mem://b/main/t", "first")
    cat.stores["mem"].remove("b", "main/t/sub/b.csv")
    assert cat.pin("mem://b/main/t/sub") == first.child("sub")
    assert cat.pin("mem://b/main/t/sub/b.csv") == first.child("sub").child("b.csv")
    with pytest.raises(NotFound):
        cat.pin("mem://b/main/t/never")


def test_a_commit_never_builds_on_files_older_than_its_parent(cat, monkeypatch):
    """Another writer commits just before this commit reads the log. Its
    listing must come after that read, so it holds the other writer's file."""
    store = cat.stores["mem"]
    real = type(store)._newest_commit
    done = {}

    def newest(self, uri, bucket, folder):
        if not done and folder == "main/t":
            done["x"] = 1
            store.write("b", "main/t/c.csv", b"C")
            done["theirs"] = cat.commit("mem://b/main/t", "theirs")
        return real(self, uri, bucket, folder)

    monkeypatch.setattr(type(store), "_newest_commit", newest)
    with pytest.raises(StoreError, match="nothing to commit"):
        cat.commit("mem://b/main/t", "mine")  # theirs already holds every file


def test_a_commit_folder_with_case_twins_is_refused(cat, tmp_path):
    store = cat.stores["mem"]
    store.write("b", "main/t/A.csv", b"upper")
    pinned = cat.commit("mem://b/main/t", "first")
    with pytest.raises(StoreError, match="one name on a disk"):
        cat.fetch(pinned)
    assert cat.read(f"mem://b/main/t/A.csv?at={pinned.selector}") == b"upper"
    assert cat.read(f"mem://b/main/t/a.csv?at={pinned.selector}") == b"A1"


def _frozen_clock(monkeypatch):
    import provcat.stores.objects as objects
    from datetime import UTC, datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(objects, "datetime", Frozen)


def test_in_one_clock_tick_the_folder_above_wins(cat, monkeypatch):
    """The flaky case: a folder and the folder above it committed in one tick.
    Commit IDs are hashes, so the answer must not hang on them."""
    _frozen_clock(monkeypatch)
    cat.commit("mem://b/main/t", "t")
    top = cat.commit("mem://b/main", "main")
    for _ in range(3):
        assert cat.pin("mem://b/main/t").selector == top.selector


def test_a_commit_time_has_microseconds(cat):
    pinned = cat.commit("mem://b/main/t", "t")
    store = cat.stores["mem"]
    cid = pinned.selector.value
    m = json.loads(store._read_small(pinned, "b", f".provcat/commits/{cid}.json"))
    assert len(m["time"].split(".")[1].split("+")[0]) == 6


def test_a_commit_with_no_time_is_named(cat):
    pinned = cat.commit("mem://b/main/t", "t")
    store = cat.stores["mem"]
    cid = pinned.selector.value
    m = json.loads(store._read_small(pinned, "b", f".provcat/commits/{cid}.json"))
    del m["time"]
    store._manifest = lambda uri, c, cache: m
    with pytest.raises(StoreError, match="records no time"):
        cat.pin("mem://b/main/t/sub")


@pytest.mark.parametrize("time, message", [
    ("2026-01-01T00:00:00.000", "without a zone"),
    ("yesterday", "which is not a time"),
])
def test_a_bad_commit_time_is_named(cat, time, message):
    pinned = cat.commit("mem://b/main/t", "t")
    store = cat.stores["mem"]
    m = json.loads(store._read_small(pinned, "b", f".provcat/commits/{pinned.selector.value}.json"))
    m["time"] = time
    store._manifest = lambda uri, c, cache: m
    with pytest.raises(StoreError, match=message):
        cat.pin("mem://b/main/t")


@pytest.mark.parametrize("old", ["2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000+00:00"])
def test_an_old_millisecond_time_sorts_with_new_ones(cat, old):
    """Commits made before this change stored milliseconds."""
    first = cat.commit("mem://b/main/t", "t")
    store = cat.stores["mem"]
    top = cat.commit("mem://b/main", "main")
    real = store._manifest

    def manifest(uri, c, cache):
        m = dict(real(uri, c, cache))
        if c == first.selector.value:
            m["time"] = old
        return m

    store._manifest = manifest
    assert cat.pin("mem://b/main/t").selector == top.selector
