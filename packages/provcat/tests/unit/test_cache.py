import os
import threading

from provcat import LocalCache
from provcat.cache import find_twins


def test_concurrent_puts_never_record_a_wrong_version(tmp_path):
    # Third review: a record could pair one writer's version with the other writer's hash.
    cache = LocalCache(tmp_path)
    key = ["bucket", "obj"]
    bad = []
    for round_ in range(200):
        def writer(tag):
            def fill(tmp):
                tmp.write_bytes(tag.encode() * 1000)
                return tag
            cache.put("s3", key, fill)

        threads = [threading.Thread(target=writer, args=(f"r{round_}w{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(2):
            tag = f"r{round_}w{i}"
            p = cache.get("s3", key, tag)
            if p is not None and p.read_bytes() != tag.encode() * 1000:
                bad.append(tag)
    assert bad == []


def test_another_file_of_the_same_size_and_time_is_not_taken_for_the_cached_one(tmp_path):
    # On Linux a file's time has a coarse tick, so two writers can give two files
    # the same size and time; the record of one must not vouch for the other.
    cache = LocalCache(tmp_path)
    def fill(tmp):
        tmp.write_bytes(b"a" * 10)
        return "v1"

    p = cache.put("s3", ["b", "o"], fill)
    assert cache.get("s3", ["b", "o"], "v1") == p
    st = p.stat()
    other = tmp_path / "other"
    other.write_bytes(b"b" * 10)
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, p)
    assert cache.get("s3", ["b", "o"], "v1") is None


def test_writes_of_one_file_take_turns(tmp_path):
    # Review: with three writers, a freed inode number can come back on ext4, so a
    # late record could still vouch for another writer's file. A put holds the key's lock.
    cache = LocalCache(tmp_path)
    started, release = threading.Event(), threading.Event()
    order = []

    def slow(tmp):
        started.set()
        release.wait(5)
        order.append("a")
        tmp.write_bytes(b"a")
        return "va"

    def quick(tmp):
        order.append("b")
        tmp.write_bytes(b"b")
        return "vb"

    a = threading.Thread(target=cache.put, args=("s3", ["k"], slow))
    a.start()
    started.wait(5)
    b = threading.Thread(target=cache.put, args=("s3", ["k"], quick))
    b.start()
    b.join(0.3)
    assert order == []  # b waits for a's lock
    release.set()
    a.join()
    b.join()
    assert order == ["a", "b"]
    assert cache.get("s3", ["k"], "vb").read_bytes() == b"b"
    assert cache.get("s3", ["k"], "va") is None


def test_a_read_that_sees_the_record_change_returns_nothing(tmp_path, monkeypatch):
    # Review: get checks the file without the lock, so puts that finish while it
    # looks could leave it checking an old record against a new file.
    cache = LocalCache(tmp_path)
    cache.put("s3", ["k"], lambda t: t.write_bytes(b"a") and "va")
    check = cache._unchanged

    def a_put_lands_meanwhile(p, store, key, meta):
        cache.put("s3", ["k"], lambda t: t.write_bytes(b"b") and "vb")
        check(p, store, key, meta)
        return True  # as if a reused inode had fooled the check

    monkeypatch.setattr(cache, "_unchanged", a_put_lands_meanwhile)
    assert cache.get("s3", ["k"], "va") is None


def test_find_twins():
    assert find_twins([("a", "b"), ("a", "c")]) is None
    assert find_twins([("Dir", "a"), ("dir", "b")]) == ("Dir", "dir")
    assert find_twins([("caf\u00e9",), ("cafe\u0301",)]) is not None
