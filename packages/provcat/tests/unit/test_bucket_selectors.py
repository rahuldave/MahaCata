"""Selectors on a logical URI whose target is a bucket, which keeps only
branches in its path: a commit or a version goes in ?at=, and no selector is
dropped."""
import pytest

from provcat import Catalog, NotFound, ResolveError
from provcat.resolver import TemplateResolver
from provcat.uri import Uri, parse

from test_bucket_commits import MemoryStore  # noqa: F401  registers the memtest driver

pytestmark = pytest.mark.unit

SCHEMES = {
    "files": {"parts": ["org", "project"], "rest": "path", "target": "s3-{org}://{org}-{project}/{ref}/{path}",
              "ref": "main", "match": {"org": "[a-z0-9]+"}},
    "envfiles": {"parts": ["org", "env"], "rest": "path", "target": "s3://{org}/{ref}/{path}", "ref": "{env}"},
    "vfiles": {"parts": ["org"], "rest": "path", "target": "s3://{org}/{ref}/{path}/v{version}", "ref": "main"},
    "flat": {"parts": ["org"], "rest": "path", "target": "s3://{org}/{path}"},
    "noref": {"parts": ["org"], "rest": "path", "target": "s3://{org}/{ref}/{path}"},
    "lake": {"parts": ["org"], "rest": "path", "target": "lakefs://{org}/{ref}/{path}", "ref": "main"},
    "model": {"parts": ["org", "name"], "target": "lakefs://{org}/main/{name}"},
    "atfield": {"parts": ["org"], "rest": "path", "target": "s3://{org}/main/{path}/{at}"},
    "passing": {"parts": ["org"], "rest": "path", "target": "s3://{org}/{ref}/{path}", "ref": "main",
                "pass_selector": True},
}
ENTRIES = {
    "files://acme/fixed": {"target": "s3-acme://acme-fixed/{ref}/{path}", "ref": "main"},
    "files://acme/plain": {"target": "s3-acme://acme-plain/data/{path}"},
    "files://acme/pinned": {"target": "s3-acme://acme-pinned/main/{path}?at=version:V1"},
    "lake://acme/entry": {"target": "lakefs://acme/{ref}/e/{path}", "ref": "main"},
}
BUCKETS = {"s3": False, "s3-acme": False, "lakefs": True}


def res(buckets=True):
    return TemplateResolver(SCHEMES, None, None, ENTRIES, BUCKETS.get if buckets else None)


def phys(uri, **kw):
    return str(res(**kw).resolve(parse(uri)).physical)


# Templates


def test_a_commit_keeps_the_default_branch_and_goes_in_at():
    assert phys("files://acme/sales/t?at=commit:abc") == "s3-acme://acme-sales/main/t?at=commit:abc"


def test_a_version_keeps_the_default_branch_and_goes_in_at():
    assert phys("files://acme/sales/t/a.csv?at=version:v7") == "s3-acme://acme-sales/main/t/a.csv?at=version:v7"


def test_a_target_with_a_version_field_takes_the_version_there():
    assert phys("vfiles://acme/t?at=version:3") == "s3://acme/main/t/v3"


def test_a_branch_still_names_the_folder():
    assert phys("files://acme/sales/t?at=branch:dev") == "s3-acme://acme-sales/dev/t"
    assert phys("files://acme/sales/t") == "s3-acme://acme-sales/main/t"


def test_a_tag_is_refused_on_a_bucket():
    with pytest.raises(ResolveError, match="no tags"):
        res().resolve(parse("files://acme/sales/t?at=tag:v1"))


def test_a_ts_passes_through_to_the_store():
    assert phys("files://acme/sales/t?at=ts:2026-01-01") == "s3-acme://acme-sales/main/t?at=ts:2026-01-01"


def test_a_default_branch_from_a_part_holds_the_commit():
    assert phys("envfiles://acme/dev/t?at=commit:abc") == "s3://acme/dev/t?at=commit:abc"


def test_a_bucket_target_with_ref_and_no_default_refuses_a_commit():
    with pytest.raises(ResolveError, match="needs a ref"):
        res().resolve(parse("noref://acme/t?at=commit:abc"))


def test_a_bucket_target_without_ref_takes_a_commit_or_version_in_at():
    assert phys("flat://acme/t?at=commit:abc") == "s3://acme/t?at=commit:abc"
    assert phys("flat://acme/t/a.csv?at=version:v1") == "s3://acme/t/a.csv?at=version:v1"


def test_a_target_with_at_places_the_selector_itself():
    assert phys("atfield://acme/t?at=commit:abc") == "s3://acme/main/t/commit:abc"


def test_pass_selector_passes_the_commit_once():
    assert phys("passing://acme/t?at=commit:abc") == "s3://acme/main/t?at=commit:abc"


def test_lakefs_keeps_the_commit_in_the_path():
    assert phys("lake://acme/t?at=commit:abc") == "lakefs://acme/abc/t"
    assert phys("lake://acme/t?at=tag:v1") == "lakefs://acme/v1/t"


def test_an_unused_version_is_refused_not_dropped():
    # lakeFS has a place for a commit, but this target has none for a version.
    with pytest.raises(ResolveError, match="no \\{version\\}"):
        res().resolve(parse("lake://acme/t?at=version:3"))
    with pytest.raises(ResolveError, match="no \\{version\\}"):
        res(buckets=False).resolve(parse("lake://acme/t?at=version:3"))


def test_without_the_catalog_every_store_keeps_refs_in_the_path():
    assert phys("files://acme/sales/t?at=commit:abc", buckets=False) == "s3-acme://acme-sales/abc/t"


def test_a_stage_that_names_a_commit_goes_in_at():
    r = TemplateResolver(SCHEMES, {"files://acme/sales/t": {"prod": "commit:abc"}}, None, None, BUCKETS.get)
    assert str(r.resolve(parse("files://acme/sales/t?at=stage:prod")).physical) == \
        "s3-acme://acme-sales/main/t?at=commit:abc"


# Entries


def test_an_entry_to_a_bucket_keeps_its_branch_for_a_commit_or_version():
    assert phys("files://acme/fixed/t?at=commit:abc") == "s3-acme://acme-fixed/main/t?at=commit:abc"
    assert phys("files://acme/fixed/t/a.csv?at=version:v1") == "s3-acme://acme-fixed/main/t/a.csv?at=version:v1"
    assert phys("files://acme/fixed/t?at=branch:dev") == "s3-acme://acme-fixed/dev/t"
    with pytest.raises(ResolveError, match="no tags"):
        res().resolve(parse("files://acme/fixed/t?at=tag:v1"))


def test_an_entry_to_a_bucket_without_ref_takes_the_version_in_at():
    assert phys("files://acme/plain/a.csv?at=version:v1") == "s3-acme://acme-plain/data/a.csv?at=version:v1"
    assert phys("files://acme/plain/t?at=commit:abc") == "s3-acme://acme-plain/data/t?at=commit:abc"


def test_an_entry_fixed_to_a_version_refuses_another_version():
    with pytest.raises(ResolveError, match="fixed to version:V1"):
        res().resolve(parse("files://acme/pinned/a.csv?at=version:V2"))


def test_an_entry_to_lakefs_keeps_the_commit_in_the_path_and_refuses_a_version():
    assert phys("lake://acme/entry/x?at=commit:abc") == "lakefs://acme/abc/e/x"
    with pytest.raises(ResolveError, match="not version"):
        res().resolve(parse("lake://acme/entry/x?at=version:3"))


# The catalog tells the resolver which stores are buckets


def test_the_catalog_names_its_buckets(tmp_path):
    cfg = {"stores": {"s3-acme": {"driver": "s3"}, "lakefs": {"driver": "lakefs", "endpoint_url": "http://x"}},
           "schemes": SCHEMES}
    c = Catalog(cfg, cache_root=tmp_path)
    assert str(c.resolve("files://acme/sales/t?at=commit:abc").physical) == "s3-acme://acme-sales/main/t?at=commit:abc"
    assert str(c.resolve("lake://acme/t?at=commit:abc").physical) == "lakefs://acme/abc/t"


@pytest.fixture
def mem(tmp_path):
    MemoryStore.buckets = {}
    cfg = {"stores": {"mem": {"driver": "memtest"}},
           "schemes": {"m": {"parts": ["project"], "rest": "path", "target": "mem://{project}/{ref}/{path}",
                             "ref": "main"}}}
    c = Catalog(cfg, cache_root=tmp_path / "cache")
    c.stores["mem"].write("b", "main/t/a.csv", b"A1")
    c.stores["mem"].write("b", "dev/t/a.csv", b"D1")
    return c


def test_a_commit_and_a_version_read_the_old_bytes_through_a_logical_uri(mem):
    pinned = mem.commit("m://b/t", "first")
    version = mem.pin("m://b/t/a.csv").selector
    mem.put("m://b/t/a.csv", b"A2")
    assert mem.read(f"m://b/t/a.csv?at={pinned.selector}") == b"A1"
    assert mem.read(f"m://b/t/a.csv?at={version}") == b"A1"
    assert mem.read("m://b/t/a.csv") == b"A2"


def test_a_commit_made_on_another_branch_is_refused_with_its_physical_uri(mem):
    pinned = mem.commit("m://b/t?at=branch:dev", "on dev")
    assert str(pinned).startswith("mem://b/dev/t?at=commit:")
    with pytest.raises(NotFound, match="made on branch 'dev', not 'main'; read it as mem://b/dev/t/a.csv"):
        mem.read(f"m://b/t/a.csv?at={pinned.selector}")
    assert mem.read(str(pinned.with_selector(None).child("a.csv").with_selector(pinned.selector))) == b"D1"


# Review findings


def test_a_bucket_target_with_ref_and_at_keeps_the_branch_for_a_commit():
    schemes = {"refat": {"parts": ["org"], "rest": "path", "target": "s3://{org}/{ref}/{path}?at={at}",
                         "ref": "main"}}
    r = TemplateResolver(schemes, None, None, None, BUCKETS.get)
    assert str(r.resolve(parse("refat://acme/t?at=commit:abc")).physical) == "s3://acme/main/t?at=commit:abc"
    assert str(r.resolve(parse("refat://acme/t?at=version:v1")).physical) == "s3://acme/main/t?at=version:v1"
    assert str(r.resolve(parse("refat://acme/t?at=branch:dev")).physical) == "s3://acme/dev/t?at=branch:dev"


@pytest.mark.parametrize("uri", ["model://acme/m?at=commit:abc", "model://acme/m?at=tag:v1",
                                 "model://acme/m?at=branch:dev", "flat://acme/t?at=branch:dev"])
def test_a_ref_selector_that_nothing_uses_is_refused(uri):
    with pytest.raises(ResolveError, match="no \\{ref\\}.*would be dropped"):
        res().resolve(parse(uri))


def test_a_resolver_without_refs_in_path_set_still_resolves():
    r = TemplateResolver.__new__(TemplateResolver)
    r.__dict__.update(res().__dict__)
    del r.__dict__["refs_in_path"]
    assert str(r.resolve(parse("lake://acme/t?at=commit:abc")).physical) == "lakefs://acme/abc/t"


class ContainerStore(MemoryStore):
    """mem2://account/container/key, with the container in the path, as on Azure."""

    driver = "memtest-container"

    def locate(self, uri):
        if uri.selector is not None:
            self._selector(uri)
        return uri.segments[0], "/".join(uri.segments[1:])

    def _root(self, uri):
        return Uri(uri.scheme, uri.authority, uri.segments[:1])


def test_the_other_branch_hint_keeps_an_azure_container(tmp_path):
    from provcat.stores import register_driver

    register_driver(ContainerStore)
    MemoryStore.buckets = {}
    c = Catalog({"stores": {"mem2": {"driver": "memtest-container"}}}, cache_root=tmp_path / "cache")
    c.stores["mem2"].write("cont", "dev/t/a.csv", b"D1")
    pinned = c.commit("mem2://acct/cont/dev/t", "on dev")
    with pytest.raises(NotFound, match="read it as mem2://acct/cont/dev/t/a.csv\\?at=commit:"):
        c.read(f"mem2://acct/cont/main/t/a.csv?at={pinned.selector}")
