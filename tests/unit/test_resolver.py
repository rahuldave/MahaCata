from pathlib import Path

import pytest

from provcat import Catalog, ResolveError, TemplateResolver, YamlConfig, parse

EXAMPLE = Path(__file__).parents[2] / "config" / "provcat.example.yaml"

SCHEMES = {
    "data+table": {
        "parts": ["org", "domain", "env", "family", "name"],
        "target": "lakefs://{family}-{name}-data/{ref}/{org}/{domain}/{env}/{family}/{name}",
        "ref": "main",
    },
    "model": {
        "parts": ["org", "team", "env", "project", "model_name"],
        "target": "lakefs://model-{model_name}/main/{project}/versions/v{version}",
    },
    "code": {"parts": ["owner", "repo"], "rest": "path", "target": "github://{owner}/{repo}/{path}",
             "pass_selector": True},
}
TABLE = "data+table://acme/shared/prod/agreements/qa_outputs"
STAGES = {TABLE: {"production": "commit:b8a2f9c", "loop": "stage:production"}}


def r(uri):
    return TemplateResolver(SCHEMES, STAGES).resolve(parse(uri))


def test_default_ref():
    assert str(r(TABLE).physical) == "lakefs://agreements-qa_outputs-data/main/acme/shared/prod/agreements/qa_outputs"


def test_branch_and_commit_set_ref():
    assert r(TABLE + "?at=branch:dev").physical.segments[0] == "dev"
    assert r(TABLE + "?at=commit:abc123").physical.segments[0] == "abc123"


def test_stage_pointer():
    res = r(TABLE + "?at=stage:production")
    assert res.physical.segments[0] == "b8a2f9c"
    assert res.via[0] == "stage:production -> commit:b8a2f9c"


def test_unknown_stage_and_stage_chains_fail():
    with pytest.raises(ResolveError, match="no stage"):
        r(TABLE + "?at=stage:nope")
    with pytest.raises(ResolveError, match="another stage"):
        r(TABLE + "?at=stage:loop")


def test_version():
    assert str(r("model://acme/t/prod/p/m?at=version:3").physical).endswith("/versions/v3")
    with pytest.raises(ResolveError, match="version"):
        r("model://acme/t/prod/p/m")


def test_ts_is_refused():
    with pytest.raises(ResolveError, match="not supported"):
        r(TABLE + "?at=ts:2026-01-01")


def test_empty_first_part():
    with pytest.raises(ResolveError, match="first part"):
        r("data+table:///shared/prod/agreements/qa_outputs")


def test_wrong_part_count():
    with pytest.raises(ResolveError, match="does not match"):
        r("data+table://acme/shared/prod/agreements")
    with pytest.raises(ResolveError, match="does not match"):
        r(TABLE + "/extra")


def test_rest_and_pass_selector():
    res = r("code://rahuldave/provcat/src/x.py?at=tag:v1")
    assert str(res.physical) == "github://rahuldave/provcat/src/x.py?at=tag:v1"
    assert str(r("code://rahuldave/provcat").physical) == "github://rahuldave/provcat"


def test_values_cannot_inject_a_selector_or_be_decoded_twice():
    # Finding 1: a %3F in a logical segment became a real ?at= in the physical URI.
    res = r("code://o/r/README%3Fat=commit:cccc")
    assert res.physical.selector is None
    assert res.physical.segments == ("r", "README?at=commit:cccc")
    assert r("code://o/r/a%2525b").physical.segments == ("r", "a%25b")
    assert r(TABLE + "?at=branch:a%3Fat=x").physical.segments[0] == "a?at=x"


def test_ref_with_slash_cannot_add_segments():
    # A lakeFS ref goes into one path segment; a slash in it is refused, not split.
    with pytest.raises(ResolveError):
        r(TABLE + "?at=branch:feat/x")


def test_pass_selector_passes_stage_result_and_default_ref():
    schemes = {"c": {"parts": ["o", "r"], "target": "github://{o}/{r}", "ref": "dev", "pass_selector": True}}
    res = TemplateResolver(schemes, {"c://a/b": {"prod": "tag:v2"}})
    assert str(res.resolve(parse("c://a/b")).physical) == "github://a/b?at=branch:dev"
    assert str(res.resolve(parse("c://a/b?at=stage:prod")).physical) == "github://a/b?at=tag:v2"


def test_authority_is_encoded_once():
    # Second review, finding 6: o%20x became o%2520x.
    assert str(r("code://o%20x/r/f").physical) == "github://o%20x/r/f"
    assert parse("s3://b%25x/k").authority == "b%x"
    assert parse(str(parse("s3://b%25x/k"))) == parse("s3://b%25x/k")


def test_at_field_is_one_segment():
    # Second review, finding 7: {at} with a / in the ref added a segment.
    res = TemplateResolver({"t": {"parts": ["a"], "target": "s3://bkt/{at}/x", "ref": "main"}})
    with pytest.raises(ResolveError, match="bad path segment"):
        res.resolve(parse("t://v?at=branch:feat/x"))
    q = TemplateResolver({"t": {"parts": ["a"], "target": "github://o/{a}?at={at}"}})
    assert q.resolve(parse("t://v?at=branch:feat/x")).physical.selector.value == "feat/x"


def test_bad_stage_keys():
    # Second review, findings 5 and 8.
    with pytest.raises(ResolveError, match="URI string"):
        TemplateResolver(SCHEMES, {123: {}})
    with pytest.raises(ResolveError, match="must not have"):
        TemplateResolver(SCHEMES, {TABLE + "?at=branch:x": {}})


def test_stage_keys_are_made_canonical():
    res = TemplateResolver(SCHEMES, {TABLE + "/": {"p": "commit:c9"}})
    assert res.resolve(parse(TABLE + "?at=stage:p")).physical.segments[0] == "c9"


def test_bad_specs():
    with pytest.raises(ResolveError):
        TemplateResolver({"x": {"parts": [], "target": "s3://a/b"}})
    with pytest.raises(ResolveError):
        TemplateResolver({"x": {"parts": ["ref"], "target": "s3://a/b"}})
    with pytest.raises(ResolveError):
        TemplateResolver({"x": {"parts": ["a"], "target": "s3://a/b", "typo": 1}})
    # Finding 13: unknown fields and string booleans used to load.
    with pytest.raises(ResolveError, match="unknown field"):
        TemplateResolver({"x": {"parts": ["a"], "target": "s3://b/{nmae}"}})
    with pytest.raises(ResolveError, match="true or false"):
        TemplateResolver({"x": {"parts": ["a"], "target": "s3://b/{a}", "pass_selector": "false"}})
    with pytest.raises(ResolveError, match="bad key"):
        TemplateResolver({}, {"no scheme": {}})


def test_template_cannot_reach_attributes():
    for bad in ["s3://b/{a.__class__}", "s3://b/{a[0]}", "s3://b/{a!r}", "s3://b/{a:>9}", "s3://b/{0}", "s3://b/{"]:
        with pytest.raises(ResolveError):
            TemplateResolver({"x": {"parts": ["a"], "target": bad}})


def test_resolved_value_cannot_climb():
    # A ref of ".." would make the physical URI climb; the URI parser refuses it.
    with pytest.raises(ResolveError):
        r(TABLE + "?at=branch:..")


def test_example_config_gives_each_org_its_own_repositories():
    cat = Catalog(YamlConfig(EXAMPLE).load(), cache_root="/nonexistent")

    def phys(u):
        return str(cat.resolve(u).physical)

    assert phys(TABLE) == "lakefs://acme-data-shared/prod/tables/agreements/qa_outputs"
    assert phys(TABLE + "?at=stage:production") == "lakefs://acme-data-shared/b8a2f9c123456/tables/agreements/qa_outputs"
    assert phys("data+table://globex/shared/dev/agreements/qa_outputs") == (
        "lakefs://globex-data-shared/dev/tables/agreements/qa_outputs")
    assert phys("model://acme/ml/prod/churn/xgb?at=version:3") == "lakefs://acme-ml-churn/prod/ml/models/xgb/v3"
    assert phys("model://globex/ml/prod/churn/xgb?at=version:3") == "lakefs://globex-ml-churn/prod/ml/models/xgb/v3"
    assert phys("artifact+prompt://acme/teams/prod/credit/default_v1") == (
        "lakefs://acme-ml-credit/prod/teams/prompts/default_v1")
    # The env is the GitHub branch, and a selector replaces it.
    assert phys("code://rahuldave/x/prod/README.md") == "github://rahuldave/x/README.md?at=branch:prod"
    assert phys("code://rahuldave/x/prod/src/a.py?at=tag:v1") == "github://rahuldave/x/src/a.py?at=tag:v1"
    assert phys("code://rahuldave/x/prod") == "github://rahuldave/x?at=branch:prod"
    with pytest.raises(ResolveError, match="does not match"):
        phys("code://rahuldave/x")
    with pytest.raises(ResolveError, match="org '-bad' does not match"):
        phys("code://-bad/x/prod/a")
    # Review of the code env, findings 2 and 3: GitHub and git name rules.
    for bad in ("code://bad-/x/main/a", "code://a--b/x/main/a", "code://{}/x/main/a".format("a" * 40),
                "code://o/r/main..x/a", "code://o/r/main.lock/a", "code://o/r/main./a", "code://o/r/@/a"):
        with pytest.raises(ResolveError, match="does not match"):
            phys(bad)
    for good in ("code://{}/x/main/a".format("a" * 39), "code://o/r/_wip/a", "code://o/r/v1.2+build/a"):
        assert phys(good).startswith("github://")
    # Finding 4: a branch with "/" goes in the selector.
    assert phys("code://o/r/main/a?at=branch:feature/x") == "github://o/r/a?at=branch:feature/x"
    # Tenant review, findings 1 and 2: no two names give one repository.
    with pytest.raises(ResolveError, match="org 'a-b' does not match"):
        phys("data+table://a-b/c/prod/f/n")
    assert phys("data+table://a/b-c/prod/f/n").startswith("lakefs://a-data-b-c/")
    assert phys("data+table://acme/churn/prod/models/x").startswith("lakefs://acme-data-churn/")
    assert phys("model://acme/tables/prod/churn/x?at=version:3").startswith("lakefs://acme-ml-churn/")
    with pytest.raises(ResolveError, match="does not match"):
        phys("model://acme/t/prod/Churn/x?at=version:3")
    # Second tenant review, findings 1 and 2: lengths and branch names stay
    # valid for lakeFS.
    org, longest = "o" * 20, phys("data+table://{}/{}/prod/f/n".format("o" * 20, "d" * 37))
    assert len(longest.split("/")[2]) == 63
    assert len(phys("model://{}/t/prod/{}/x?at=version:1".format(org, "p" * 39)).split("/")[2]) == 63
    for bad in ("data+table://{}/d/prod/f/n".format("o" * 21), "data+table://acme/{}/prod/f/n".format("d" * 38),
                "model://acme/t/prod/{}/x?at=version:1".format("p" * 40),
                "data+table://acme/shared/pr%20od/f/n", "model://acme/t/Prod/p/x?at=version:1"):
        with pytest.raises(ResolveError, match="does not match"):
            phys(bad)


def test_ref_default_can_name_parts():
    schemes = {"t": {"parts": ["org", "env", "name"], "target": "lakefs://{org}/{ref}/{name}/v{version}",
                     "ref": "{env}"}}
    res = TemplateResolver(schemes).resolve
    assert str(res(parse("t://acme/dev/x?at=version:2")).physical) == "lakefs://acme/dev/x/v2"
    with pytest.raises(ResolveError, match="version"):
        res(parse("t://acme/dev/x"))
    with pytest.raises(ResolveError, match="version"):
        res(parse("t://acme/dev/x?at=commit:abc"))


def test_pass_selector_passes_the_part_default_or_only_the_version():
    # Tenant review, finding 5: the name promised a version case.
    schemes = {"c": {"parts": ["org", "env"], "rest": "path", "target": "github://{org}/{env}/{path}",
                     "ref": "{env}", "pass_selector": True}}
    res = TemplateResolver(schemes).resolve
    assert str(res(parse("c://o/main/a.txt")).physical) == "github://o/main/a.txt?at=branch:main"
    assert str(res(parse("c://o/main/a.txt?at=version:3")).physical) == "github://o/main/a.txt?at=version:3"


def test_ref_template_values_are_encoded_once():
    # Tenant review, finding 5: a part that fills the default branch is encoded
    # once, and cannot add a segment or a selector.
    schemes = {"t": {"parts": ["org", "env", "name"], "target": "lakefs://{org}/{ref}/{name}", "ref": "{env}"}}
    res = TemplateResolver(schemes).resolve
    assert res(parse("t://a/50%25/x")).physical.segments == ("50%", "x")
    assert res(parse("t://a/de%3Fat%3Dx/x")).physical.segments == ("de?at=x", "x")
    assert res(parse("t://a/de%3Fat%3Dx/x")).physical.selector is None


def test_match_patterns():
    schemes = {"t": {"parts": ["org", "name"], "target": "file:///{org}/{name}", "match": {"org": "[a-z]+"}}}
    res = TemplateResolver(schemes).resolve
    assert str(res(parse("t://abc/x")).physical) == "file:///abc/x"
    with pytest.raises(ResolveError, match="org 'ab1' does not match"):
        res(parse("t://ab1/x"))
    # Second tenant review, finding 3: an empty value that is not a mapping is refused.
    for bad in ({"nope": "x"}, {"org": 3}, {"org": "("}, ["org"], [], "", 0, False):
        with pytest.raises(ResolveError, match="match"):
            TemplateResolver({"t": {**schemes["t"], "match": bad}})
    assert TemplateResolver({"t": {**schemes["t"], "match": None}}).handles("t")
    # Finding 4: the rest name gets its own message.
    with pytest.raises(ResolveError, match="rest name"):
        TemplateResolver({"c": {"parts": ["o"], "rest": "path", "target": "file:///{o}/{path}",
                                "match": {"path": "x"}}})


@pytest.mark.parametrize("ref", ["{path}", "{ref}", "{version}", "{env!r}", "{env:>3}", "{env.x}", "{", ""])
def test_bad_ref_templates(ref):
    with pytest.raises(ResolveError, match="ref"):
        TemplateResolver({"c": {"parts": ["org", "env"], "rest": "path",
                                "target": "lakefs://{org}/{ref}/{path}", "ref": ref}})


def test_catalog_refuses_scheme_clash_and_dangling_targets():
    with pytest.raises(Exception, match="both stores and logical"):
        Catalog({"schemes": {"s3": {"parts": ["a"], "target": "file:///x"}}})
    cat = Catalog({"schemes": {"x": {"parts": ["a"], "target": "nostore://a/b"}}})
    with pytest.raises(ResolveError, match="not a store"):
        cat.resolve("x://v")
