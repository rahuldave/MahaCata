"""Catalog entries: rules for one logical name or one prefix."""
import pytest

from provcat import Catalog
from provcat.config import view
from provcat.errors import ConfigError, ResolveError
from provcat.resolver import TemplateResolver
from provcat.uri import parse

pytestmark = pytest.mark.unit

SCHEMES = {
    "code": {"parts": ["org", "repo", "env"], "rest": "path", "target": "github://{org}/{repo}/{path}",
             "ref": "{env}", "match": {"org": "[a-z0-9]+", "env": "[a-z]+"}},
    "data": {"parts": ["org", "name"], "target": "lakefs-{org}://{org}-data/{ref}/{name}", "ref": "main",
             "tenant": "org", "match": {"org": "[a-z0-9]+"}},
}
ENTRIES = {
    "code://acme/tokenizer/prod": {"target": "github://acme/tokenizer/{path}?at=branch:prod"},
    "code://acme/tokenizer/prod/weights": {"target": "hf://models/acme/tokenizer/{path}"},
    "code://acme/tokenizer/prod/card": {"target": "github://acme/cards/tokenizer.md?at=tag:v1"},
    "code://acme/lake/dev": {"target": "lakefs://lake/{ref}/code/{path}", "ref": "main"},
}


def res(entries=None, stages=None, tenant=None):
    return TemplateResolver(SCHEMES, stages, tenant, ENTRIES if entries is None else entries)


def phys(uri, **kw):
    return str(res(**kw).resolve(parse(uri)).physical)


def test_longest_prefix_wins_by_whole_parts():
    assert phys("code://acme/tokenizer/prod/src/a.py") == "github://acme/tokenizer/src/a.py?at=branch:prod"
    assert phys("code://acme/tokenizer/prod/weights/model.bin") == "hf://models/acme/tokenizer/model.bin"
    assert phys("code://acme/tokenizer/prod/weightsx") == "github://acme/tokenizer/weightsx?at=branch:prod"
    # No entry for this env, or for a name that only starts like a key: the template applies.
    assert phys("code://acme/tokenizer/dev/a.py") == "github://acme/tokenizer/a.py"
    assert phys("code://acme/tok/prod/a.py") == "github://acme/tok/a.py"


def test_the_key_itself_and_a_key_shorter_than_the_parts():
    assert phys("code://acme/tokenizer/prod") == "github://acme/tokenizer?at=branch:prod"
    assert phys("code://acme/lake/dev") == "lakefs://lake/main/code"
    r = res({"code://acme/mono": {"target": "s3://mono/{path}"}})
    assert str(r.resolve(parse("code://acme/mono")).physical) == "s3://mono"
    assert str(r.resolve(parse("code://acme/mono/dev/x")).physical) == "s3://mono/dev/x"
    with pytest.raises(ResolveError, match="does not match"):
        r.resolve(parse("code://acme/other"))  # too short for the template


def test_a_target_without_path_serves_only_its_key():
    assert phys("code://acme/tokenizer/prod/card") == "github://acme/cards/tokenizer.md?at=tag:v1"
    # A longer URI is not under it; the next shorter key takes it.
    assert phys("code://acme/tokenizer/prod/card/x") == "github://acme/tokenizer/card/x?at=branch:prod"


def test_path_parts_are_encoded():
    got = res().resolve(parse("code://acme/tokenizer/prod/weights/a%20b/c%3Fd%23e"))
    assert got.physical.segments == ("models", "acme", "tokenizer", "a b", "c?d#e")[1:]
    assert got.physical.selector is None


def test_a_selector_on_the_uri_takes_the_place_of_the_target_selector():
    sha = "a" * 40
    assert phys(f"code://acme/tokenizer/prod/a.py?at=commit:{sha}") == f"github://acme/tokenizer/a.py?at=commit:{sha}"
    assert phys("code://acme/tokenizer/prod/weights/m?at=tag:v2") == "hf://models/acme/tokenizer/m?at=tag:v2"
    sha = "b" * 40
    assert phys(f"code://acme/tokenizer/prod/card?at=commit:{sha}") == f"github://acme/cards/tokenizer.md?at=commit:{sha}"
    for sel in ("branch:x", "tag:v2", "ts:2026-01-01"):  # an entry fixed to a tag takes only commit:
        with pytest.raises(ResolveError, match="takes only commit:"):
            phys(f"code://acme/tokenizer/prod/card?at={sel}")


def test_ref_fills_the_path_of_a_store_that_keeps_the_branch_there():
    assert phys("code://acme/lake/dev/f.py") == "lakefs://lake/main/code/f.py"
    assert phys("code://acme/lake/dev/f.py?at=commit:c1") == "lakefs://lake/c1/code/f.py"
    with pytest.raises(ResolveError, match="bad path segment"):
        phys("code://acme/lake/dev/f.py?at=branch:a%2Fb")  # as with a template: a path holds no "/" branch
    assert phys("code://acme/lake/dev/f.py?at=ts:2026-01-01") == "lakefs://lake/main/code/f.py?at=ts:2026-01-01"


def test_ts_keeps_the_branch_of_the_target(tmp_path):
    r = res().resolve(parse("code://acme/tokenizer/prod/a.py?at=ts:2026-01-01"))
    assert str(r.physical) == "github://acme/tokenizer/a.py?at=ts:2026-01-01" and r.ts_branch == "prod"
    entries = {k: v for k, v in ENTRIES.items() if "lake" not in k}
    c = Catalog({"schemes": SCHEMES, "entries": entries}, cache_root=tmp_path)
    assert str(c.resolve("code://acme/tokenizer/prod/a.py?at=ts:2026-01-01").physical) == \
        "github://acme/tokenizer/a.py?at=ts:prod@2026-01-01"
    r = res().resolve(parse("code://acme/tokenizer/prod/weights/m?at=ts:2026-01-01"))
    assert r.ts_branch is None  # the target names no branch, so the store uses its default
    with pytest.raises(ResolveError):
        phys("code://acme/tokenizer/prod/a.py?at=ts:not-a-time")


def test_version_is_refused_and_a_stage_comes_first():
    with pytest.raises(ResolveError, match="not version"):
        phys("code://acme/tokenizer/prod/a.py?at=version:3")
    stages = {"code://acme/tokenizer/prod/a.py": {"production": "commit:c9"}}
    r = res(stages=stages).resolve(parse("code://acme/tokenizer/prod/a.py?at=stage:production"))
    assert str(r.physical) == "github://acme/tokenizer/a.py?at=commit:c9"
    assert r.via == ("stage:production -> commit:c9", f"entry code://acme/tokenizer/prod -> {r.physical}")


@pytest.mark.parametrize("key, raw, msg", [
    ("code://acme/tokenizer/prod?at=branch:x", {"target": "s3://b/{path}"}, "must not have"),
    ("github://acme/tokenizer", {"target": "s3://b/{path}"}, "not a URI of a logical scheme"),
    ("nope://acme", {"target": "s3://b/{path}"}, "not a URI of a logical scheme"),
    ("code://Acme/tokenizer", {"target": "s3://b/{path}"}, "does not match"),
    ("code://acme/t/PROD", {"target": "s3://b/{path}"}, "does not match"),
    ("data://acme/a/b", {"target": "s3://b/{path}"}, "does not match"),  # data has no rest
    ("code://acme/t", "s3://b/{path}", "must be a mapping"),
    ("code://acme/t", {"target": "s3://b/{path}", "kind": "data"}, "unknown keys"),
    ("code://acme/t", {"target": "no-scheme/{path}"}, "URI template"),
    ("code://acme/t", {"target": "s3://b/{org}/{path}"}, "only fields"),
    ("code://acme/t", {"target": "s3://b/{path!r}"}, "only fields"),
    ("code://acme/t", {"target": "s3-{path}://b/x"}, "must be fixed text"),
    ("code://acme/t", {"target": "s3://{path}"}, "must be fixed text"),  # the bucket from the name
    ("code://acme/t", {"target": "lakefs://{ref}/x/{path}", "ref": "main"}, "must be fixed text"),
    ("code://acme/t", {"target": "lakefs://r/{ref}/{path}", "ref": "a/b"}, "without /"),
    ("code://acme/t", {"target": "lakefs://r/{ref}/{path}", "ref": ".."}, "without /"),
    ("code://acme/t", {"target": "lakefs://r/{ref}/{path}"}, "needs a ref"),
    ("code://acme/t", {"target": "s3://b/{path}", "ref": "main"}, "needs a ref"),
    ("code://acme/t", {"target": "lakefs://r/{ref}/{path}?at=ts:1", "ref": "main"}, "cannot also have"),
    ("code://acme/t", {"target": "lakefs://r/{ref}/{path}", "ref": "{x}"}, "branch name"),
    ("code://acme/t", {"target": "s3://b/{path}?at=nokind"}, "valid URI"),
    ("code://acme/t", {"target": "s3://b/x/../{path}"}, "valid URI"),
])
def test_bad_entries_are_refused(key, raw, msg):
    with pytest.raises(ResolveError, match=msg):
        res({key: raw})


def test_two_keys_for_one_name_are_refused():
    with pytest.raises(ResolveError, match="another key also names"):
        res({"code://acme/t": {"target": "s3://b/{path}"}, "code://acme/t/": {"target": "s3://c/{path}"}})


def test_the_store_of_a_target_must_exist(tmp_path):
    with pytest.raises(ConfigError, match="no store named 'nostore'"):
        Catalog({"schemes": SCHEMES, "entries": {"code://acme/t": {"target": "nostore://b/{path}"}}},
                cache_root=tmp_path)


def test_reads_and_writes_go_through_an_entry(tmp_path):
    root = tmp_path / "files"
    cfg = {"schemes": SCHEMES, "entries": {"code://acme/notes/dev": {"target": f"file://{root}/{{path}}"}}}
    c = Catalog(cfg, cache_root=tmp_path / "cache")
    c.put("code://acme/notes/dev/a/b.txt", b"hello")
    assert (root / "a" / "b.txt").read_bytes() == b"hello"
    assert c.read("code://acme/notes/dev/a/b.txt") == b"hello"
    assert [e.uri.segments[-1] for e in c.list("code://acme/notes/dev/a")] == ["b.txt"]


# tenants

TENANTS = {
    "schemes": SCHEMES,
    "entries": {"data://acme/shared": {"target": "s3://shared/{path}"}},
    "admin": {"entries": {"data://globex/audit": {"target": "s3://audit/{path}"}}},
    "tenants": {
        "acme": {"stores": {"lakefs-acme": {"driver": "lakefs", "endpoint_url": "http://127.0.0.1:9"}, "s3-acme": {"driver": "s3"}},
                 "entries": {"data://acme/corpus": {"target": "s3-acme://acme-corpus/all.parquet"}}},
        "globex": {"stores": {"lakefs-globex": {"driver": "lakefs", "endpoint_url": "http://127.0.0.1:9"}}},
    },
}


def test_a_tenant_sees_the_shared_entries_and_its_own(tmp_path):
    assert set(view(TENANTS, "acme")["entries"]) == {"data://acme/shared", "data://acme/corpus"}
    assert set(view(TENANTS, "globex")["entries"]) == {"data://acme/shared"}
    assert set(view(TENANTS)["entries"]) == {"data://acme/shared", "data://acme/corpus", "data://globex/audit"}
    acme = Catalog(TENANTS, cache_root=tmp_path, tenant="acme")
    assert str(acme.resolve("data://acme/corpus").physical) == "s3-acme://acme-corpus/all.parquet"
    with pytest.raises(ResolveError, match="does not match"):
        acme.resolve("data://acme/corpus/x")  # an entry does not make the scheme take longer names
    assert str(acme.resolve("data://acme/other").physical) == "lakefs-acme://acme-data/main/other"
    with pytest.raises(ResolveError, match="another tenant"):
        Catalog(TENANTS, cache_root=tmp_path, tenant="globex").resolve("data://acme/corpus")


def test_a_tenant_entry_names_only_its_own_uris_and_stores():
    bad = {**TENANTS, "tenants": {**TENANTS["tenants"], "globex": {
        "stores": {"s3-globex": {"driver": "s3"}},
        "entries": {"data://acme/corpus": {"target": "s3-globex://steal/{path}"}}}}}
    with pytest.raises(ConfigError, match="not a URI of globex"):
        view(bad, "globex")
    bad["tenants"]["globex"]["entries"] = {"data://globex/x": {"target": "s3-acme://acme-corpus/{path}"}}
    with pytest.raises(ConfigError, match="must go to a store that ends with -globex"):
        view(bad, "globex")
    bad["tenants"]["globex"]["entries"] = {"data://globex/x": {"target": "s3://anything/{path}"}}
    with pytest.raises(ConfigError, match="must go to a store that ends with -globex"):
        view(bad, "globex")
    bad["tenants"]["globex"]["entries"] = {"data://globex/x": "s3-globex://b/{path}"}
    with pytest.raises(ConfigError, match="must have a target"):
        view(bad, "globex")


def test_a_tenant_entry_cannot_take_a_shared_key():
    cfg = {**TENANTS, "tenants": {**TENANTS["tenants"], "acme": {
        **TENANTS["tenants"]["acme"], "entries": {"data://acme/shared/": {"target": "s3-acme://acme-x/{path}"}}}}}
    with pytest.raises(ConfigError, match="also named elsewhere"):
        view(cfg, "acme")


def test_a_tenant_entry_goes_only_to_buckets_named_for_the_tenant():
    def part(target):
        return {**TENANTS, "tenants": {**TENANTS["tenants"], "acme": {
            **TENANTS["tenants"]["acme"], "entries": {"data://acme/x": {"target": target}}}}}
    for ok in ("s3-acme://acme/{path}", "s3-acme://acme-raw/{path}", "lakefs-acme://acme-data/main/{path}"):
        view(part(ok), "acme")
    for bad in ("lakefs-acme://globex-data/main/{path}", "s3-acme://acmex/{path}", "s3-acme://other/{path}"):
        with pytest.raises(ConfigError, match="named acme or acme-"):
            view(part(bad), "acme")


def test_a_tenant_entry_cannot_use_values_filled_in_later(tmp_path, monkeypatch):
    """The check reads the text before ${...} is filled in, so such a value is refused."""
    from provcat.config import YamlConfig
    f = tmp_path / "c.yaml"
    f.write_text("""
schemes:
  data: {parts: [org, name], target: "lakefs-{org}://{org}-data/{ref}/{name}", ref: main, tenant: org,
         match: {org: "[a-z0-9]+"}}
tenants:
  acme:
    stores: {s3-acme: {driver: s3}}
    entries:
      "data://acme/steal":
        target: "${oc.env:N1,'file:/'}${oc.env:N2,'/'}/x${oc.env:N3,'-acme://acme'}/{path}"
""")
    for tenant in ("acme", None):
        with pytest.raises(ConfigError, match=r"no \$\{...\} values"):
            YamlConfig(str(f)).load(tenant)


def test_a_shared_entry_for_one_tenant_does_not_break_the_others(tmp_path):
    cfg = {**TENANTS, "entries": {"data://acme/shared": {"target": "s3-acme://acme-shared/{path}"}}}
    assert str(Catalog(cfg, cache_root=tmp_path, tenant="acme").resolve("data://acme/shared").physical) == \
        "s3-acme://acme-shared"
    Catalog(cfg, cache_root=tmp_path, tenant="globex")  # cannot use the entry, so needs no s3-acme
    Catalog(cfg, cache_root=tmp_path)  # the admin sees s3-acme


def test_a_uri_too_short_to_name_its_tenant_is_refused(tmp_path):
    schemes = {"d": {"parts": ["env", "org", "name"], "target": "s3://{org}/{name}", "tenant": "org"}}
    r = TemplateResolver(schemes, None, "acme", {"d://prod": {"target": "s3://open/{path}"}})
    with pytest.raises(ResolveError, match="too short to name its org"):
        r.resolve(parse("d://prod"))
