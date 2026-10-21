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


def test_find_twins():
    assert find_twins([("a", "b"), ("a", "c")]) is None
    assert find_twins([("Dir", "a"), ("dir", "b")]) == ("Dir", "dir")
    assert find_twins([("caf\u00e9",), ("cafe\u0301",)]) is not None
