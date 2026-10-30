"""A configuration for each tenant: the views, the name rules, and the
resolver that refuses another tenant's URIs."""
from pathlib import Path

import pytest

from provcat import Catalog
from provcat.cli import main
from provcat.config import YamlConfig, view
from provcat.errors import ConfigError, ResolveError

EXAMPLE = Path(__file__).parents[2] / "config" / "provcat.example.yaml"

SCHEMES = {"t": {"parts": ["org", "name"], "target": "file-{org}:///{name}", "tenant": "org",
                 "match": {"org": "[a-z0-9]+"}},
           "open": {"parts": ["org", "name"], "target": "file:///{org}/{name}"}}


def cfg(**more):
    base = {"schemes": SCHEMES, "stores": {"shared-x": {"driver": "file"}},
            "admin": {"stores": {"all": {"driver": "file"}}},
            "tenants": {"acme": {"stores": {"file-acme": {"driver": "file"}},
                                 "stages": {"t://acme/a": {"prod": "commit:1"}},
                                 "cache": {"root": "/cache/acme"}},
                        "globex": {"stores": {"file-globex": {"driver": "file"}}}},
            "cache": {"root": "/cache/shared"}}
    base.update(more)
    return base


def test_a_tenant_sees_the_shared_part_and_its_own():
    v = view(cfg(), "acme")
    assert set(v["stores"]) == {"shared-x", "file-acme"}
    assert set(v["stages"]) == {"t://acme/a"}
    assert v["cache"] == {"root": "/cache/acme"}
    assert "tenants" not in v and "admin" not in v


def test_the_admin_sees_every_tenant_and_its_own_part():
    v = view(cfg())
    assert set(v["stores"]) == {"shared-x", "all", "file-acme", "file-globex"}
    assert v["cache"] == {"root": "/cache/shared"}


def test_an_unknown_tenant_is_refused():
    with pytest.raises(ConfigError, match="no tenant 'initech'; tenants: acme, globex"):
        view(cfg(), "initech")


@pytest.mark.parametrize("store", ["file-globex", "acme-files", "acmeplus", "x-acme-y"])
def test_a_tenant_names_only_its_own_stores(store):
    c = cfg()
    c["tenants"]["acme"]["stores"] = {store: {"driver": "file"}}
    with pytest.raises(ConfigError, match="must end with -acme"):
        view(c, "acme")
    with pytest.raises(ConfigError, match="must end with -acme"):
        view(c)  # nor can it slip one into the admin's view


def test_a_tenant_names_only_its_own_stages():
    c = cfg()
    c["tenants"]["acme"]["stages"] = {"t://globex/a": {"prod": "commit:evil"}}
    with pytest.raises(ConfigError, match="not a URI of acme"):
        view(c)
    c["tenants"]["acme"]["stages"] = {"open://acme/a": {"prod": "commit:1"}}
    with pytest.raises(ConfigError, match="not a URI of acme"):
        view(c, "acme")  # a scheme with no tenant part is shared, so its stages are too


def test_a_name_in_two_parts_is_refused():
    c = cfg()
    c["stores"]["file-acme"] = {"driver": "file"}
    with pytest.raises(ConfigError, match="tenants.acme.stores.file-acme is also named elsewhere"):
        view(c, "acme")


@pytest.mark.parametrize("bad, match", [
    ({"tenants": {"Acme": {}}}, "tenant name"),
    ({"tenants": {"acme": {"schemes": {}}}}, "unknown section 'schemes'"),
    ({"tenants": {"acme": []}}, "must be a mapping"),
    ({"admin": {"tenants": {}}}, "unknown section"),
])
def test_bad_tenant_sections(bad, match):
    with pytest.raises(ConfigError, match=match):
        view(bad)


def test_a_catalog_for_a_tenant_refuses_another_tenants_uris(tmp_path):
    c = Catalog(cfg(), tenant="acme", cache_root=tmp_path)
    assert str(c.resolve("t://acme/a?at=stage:prod").physical) == "file-acme:///a"
    with pytest.raises(ResolveError, match="org 'globex' is another tenant; this catalog is for acme"):
        c.resolve("t://globex/a")
    assert str(c.resolve("open://globex/a").physical) == "file:///globex/a"  # no tenant part
    assert "file-globex" not in c.stores and "all" not in c.stores
    admin = Catalog(cfg(), cache_root=tmp_path)
    assert str(admin.resolve("t://globex/a").physical) == "file-globex:///a"


def test_the_tenant_comes_from_the_environment(tmp_path, monkeypatch):
    import yaml

    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(cfg()))
    monkeypatch.setenv("PROVCAT_TENANT", "globex")
    c = Catalog.open(str(f), cache_root=tmp_path)
    assert c.tenant == "globex" and "file-globex" in c.stores and "file-acme" not in c.stores
    assert Catalog.open(str(f), tenant="acme", cache_root=tmp_path).tenant == "acme"


def test_the_scheme_tenant_must_be_a_part():
    with pytest.raises(ResolveError, match="tenant must be one of the parts"):
        Catalog({"schemes": {"t": {"parts": ["a"], "target": "file:///{a}", "tenant": "org"}}})


def test_the_example_gives_each_tenant_its_own_stores(tmp_path):
    src = YamlConfig(EXAMPLE)
    acme = Catalog(src.load("acme"), tenant="acme", cache_root=tmp_path)
    assert {"lakefs-acme", "s3-acme"} <= set(acme.stores)
    assert not {"lakefs", "lakefs-globex", "s3-globex"} & set(acme.stores)
    assert str(acme.resolve("files://acme/sales/t/a.csv?at=branch:dev").physical) == "s3-acme://acme-sales/dev/t/a.csv"
    with pytest.raises(ResolveError, match="another tenant"):
        acme.resolve("model://globex/ml/prod/churn/xgb?at=version:3")
    admin = Catalog(src.load(), cache_root=tmp_path)
    assert {"lakefs", "lakefs-acme", "lakefs-globex"} <= set(admin.stores)


def test_the_cli_takes_a_tenant(tmp_path, capsys):
    import json

    import yaml

    f = tmp_path / "c.yaml"
    (tmp_path / "a").write_text("A")
    c = cfg()
    c["tenants"]["acme"]["stores"]["file-acme"] = {"driver": "file", "root": str(tmp_path)}
    f.write_text(yaml.safe_dump(c))
    assert main(["--config", str(f), "--tenant", "acme", "--cache", str(tmp_path / "c"), "cat", "t://acme/a"]) == 0
    assert capsys.readouterr().out == "A"
    assert main(["--config", str(f), "--tenant", "acme", "resolve", "t://globex/a"]) == 1
    assert "another tenant" in capsys.readouterr().err
    assert main(["--config", str(f), "--tenant", "acme", "config", "show"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["tenant"] == "acme" and "file-globex" not in json.dumps(shown)



@pytest.mark.parametrize("key", ["t://acme/a/", "t://%61cme/a"])
def test_a_stage_written_another_way_is_the_same_name(key):
    # Review finding 1: a trailing "/" or a percent-encoding got past the check.
    c = cfg()
    c["admin"]["stages"] = {"t://acme/a": {"prod": "commit:admin"}}
    c["tenants"]["acme"]["stages"] = {key: {"prod": "commit:evil"}}
    with pytest.raises(ConfigError, match="is also named elsewhere"):
        view(c)


def test_the_resolver_refuses_two_keys_for_one_stage():
    with pytest.raises(ResolveError, match="another key also names"):
        Catalog({"schemes": SCHEMES, "stages": {"t://acme/a": {"p": "commit:1"}, "t://acme/a/": {"p": "commit:2"}}})


def test_an_admin_section_alone_is_read(tmp_path):
    # Review finding 2: it was dropped without a tenants section.
    c = Catalog({"admin": {"stores": {"all": {"driver": "file"}}}}, cache_root=tmp_path)
    assert "all" in c.stores


def test_a_view_opens_only_as_its_own_tenant(tmp_path):
    # Review finding 3: the admin's view opened as acme's catalog kept every store.
    with pytest.raises(ConfigError, match="view of the admin, not of acme"):
        Catalog(view(cfg()), tenant="acme", cache_root=tmp_path)
    with pytest.raises(ConfigError, match="has no tenants section"):
        Catalog({"stores": {}}, tenant="acme", cache_root=tmp_path)
    assert Catalog(view(cfg(), "acme"), tenant="acme", cache_root=tmp_path).tenant == "acme"


def test_another_tenants_environment_is_never_read(tmp_path, monkeypatch):
    # Review finding 4: globex's ${oc.env:...} broke acme's load, and acme's
    # part could copy the admin's values.
    import yaml

    c = cfg()
    c["tenants"]["globex"]["stores"]["file-globex"]["root"] = "${oc.env:GLOBEX_ONLY}"
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(c))
    monkeypatch.delenv("GLOBEX_ONLY", raising=False)
    assert "file-acme" in YamlConfig(f).load("acme")["stores"]
    c["tenants"]["acme"]["stores"]["file-acme"]["root"] = "${admin.stores.all.driver}"
    f.write_text(yaml.safe_dump(c))
    with pytest.raises(ConfigError):
        YamlConfig(f).load("acme")


def test_an_empty_tenant_is_the_admin(tmp_path, monkeypatch):
    import yaml

    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump(cfg()))
    monkeypatch.setenv("PROVCAT_TENANT", "")
    assert Catalog.open(str(f), cache_root=tmp_path).tenant is None
    assert Catalog(cfg(), tenant="", cache_root=tmp_path).tenant is None


def test_the_tenant_part_must_pick_the_store():
    with pytest.raises(ResolveError, match="must be the part that picks the store"):
        Catalog({"schemes": {"x": {"parts": ["org", "team"], "target": "s3-{org}://b/{team}",
                                   "tenant": "team", "match": {"org": "[a-z]+"}}}})
