import json
import os

import pytest

from provcat import Catalog, ConfigError, EtcdConfig, NotFound, UriError, YamlConfig, open_config
from provcat.cli import main


def test_etcd_round_trip_and_same_resolution(etcd, tmp_path):
    cfg = {"schemes": {"t": {"parts": ["a"], "target": "s3://b/{ref}/{a}", "ref": "main"}},
           "stages": {"t://x": {"prod": "commit:c1"}}}
    src = EtcdConfig(etcd.url + "/provcat")
    src.save(cfg)
    assert set(etcd.kv) == {"/provcat/schemes", "/provcat/stages"}
    assert src.load() == cfg
    from_etcd = Catalog(src.load(), cache_root=tmp_path).resolve("t://x?at=stage:prod").physical
    from_dict = Catalog(cfg, cache_root=tmp_path).resolve("t://x?at=stage:prod").physical
    assert from_etcd == from_dict and str(from_etcd) == "s3://b/c1/x"


def test_push_keeps_env_references_and_drops_old_sections(etcd, tmp_path, monkeypatch, capsys):
    # Finding 10: push froze this machine's paths into shared etcd, and left old sections.
    monkeypatch.setenv("HOME", "/home/pusher")
    src = EtcdConfig(etcd.url + "/p")
    src.save({"stages": {"t://x": {"prod": "commit:1"}}, "cache": {"root": "/old"}})
    f = tmp_path / "c.yaml"
    f.write_text("cache:\n  root: ${oc.env:HOME}/.cache/provcat\n")
    assert main(["--config", str(f), "config", "push", "--to", etcd.url + "/p"]) == 0
    assert json.loads(etcd.kv["/p/cache"]) == {"root": "${oc.env:HOME}/.cache/provcat"}
    assert "/p/stages" not in etcd.kv
    monkeypatch.setenv("HOME", "/home/reader")
    assert src.load()["cache"]["root"] == "/home/reader/.cache/provcat"


def test_bad_yaml_and_bad_etcd_json_are_config_errors(tmp_path, etcd):
    # Finding 12: these escaped as yaml.ParserError and JSONDecodeError.
    f = tmp_path / "broken.yaml"
    f.write_text("schemes: [unclosed\n")
    with pytest.raises(ConfigError):
        YamlConfig(f).load()
    etcd.kv["/q/schemes"] = "not json"
    with pytest.raises(ConfigError, match="not JSON"):
        EtcdConfig(etcd.url + "/q").load()


def test_empty_etcd_value_is_a_missing_section(etcd):
    # Second review, finding 4: etcd leaves out an empty value, and that raised KeyError.
    etcd.kv["/e/stores"] = ""
    assert EtcdConfig(etcd.url + "/e").load() == {}


def test_etcd_unreachable():
    with pytest.raises(ConfigError, match="cannot reach"):
        EtcdConfig("etcd://127.0.0.1:9/p", timeout=1).load()


def test_cli(tmp_path, capsys, etcd):
    f = tmp_path / "c.yaml"
    f.write_text("schemes:\n  t:\n    parts: [a]\n    target: 'file://" + str(tmp_path) + "/{a}'\n")
    (tmp_path / "x.txt").write_text("hi")
    assert main(["--config", str(f), "resolve", "t://x.txt"]) == 0
    assert json.loads(capsys.readouterr().out)["physical"] == f"file://{tmp_path}/x.txt"
    assert main(["--config", str(f), "cat", "t://x.txt"]) == 0
    assert capsys.readouterr().out == "hi"
    assert main(["--config", str(f), "fetch", "t://nope"]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main(["--config", str(f), "config", "push", "--to", etcd.url + "/p"]) == 0
    assert "/p/schemes" in etcd.kv
    assert main(["--config", etcd.url + "/p", "resolve", "t://x.txt"]) == 0


TENANTS = {
    "schemes": {"t": {"parts": ["org", "a"], "target": "file-{org}:///{a}", "tenant": "org",
                      "match": {"org": "[a-z0-9]+"}}},
    "admin": {"stores": {"all": {"driver": "file"}}},
    "tenants": {"acme": {"stores": {"file-acme": {"driver": "file"}}, "cache": {"root": "/c/acme"}},
                "globex": {"stores": {"file-globex": {"driver": "file"}}}},
}


def test_each_tenant_has_its_own_keys(etcd):
    src = EtcdConfig(etcd.url + "/p")
    src.save(TENANTS)
    assert set(etcd.kv) == {"/p/schemes", "/p/admin/stores", "/p/tenants",
                            "/p/tenants/acme/name", "/p/tenants/acme/stores", "/p/tenants/acme/cache",
                            "/p/tenants/globex/name", "/p/tenants/globex/stores"}
    assert json.loads(etcd.kv["/p/tenants"]) == ["acme", "globex"]
    assert src.raw() == TENANTS
    assert set(src.load()["stores"]) == {"all", "file-acme", "file-globex"}


def test_a_tenant_reads_only_the_shared_keys_and_its_own(etcd):
    src = EtcdConfig(etcd.url + "/p")
    src.save(TENANTS)
    etcd.reads.clear()
    acme = src.load("acme")
    assert set(acme["stores"]) == {"file-acme"} and acme["cache"] == {"root": "/c/acme"}
    assert all(not k.startswith(("/p/tenants/globex", "/p/admin")) for k in etcd.reads), etcd.reads
    assert "/p/tenants" not in etcd.reads  # not even the list of names
    with pytest.raises(ConfigError, match="no tenant 'initech'"):
        src.load("initech")


def test_push_drops_a_tenant_that_is_gone(etcd):
    src = EtcdConfig(etcd.url + "/p")
    src.save(TENANTS)
    smaller = {**TENANTS, "tenants": {"acme": TENANTS["tenants"]["acme"]}}
    del smaller["admin"]
    src.save(smaller)
    assert not any(k.startswith(("/p/tenants/globex", "/p/admin")) for k in etcd.kv)
    assert json.loads(etcd.kv["/p/tenants"]) == ["acme"]
    src.save({"schemes": TENANTS["schemes"]})
    assert set(etcd.kv) == {"/p/schemes"}


def test_a_tenant_with_an_empty_part_loads(etcd):
    # Review finding 5: an empty part read as "no tenant" from etcd.
    src = EtcdConfig(etcd.url + "/p")
    src.save({**TENANTS, "tenants": {**TENANTS["tenants"], "initech": {}}})
    assert src.load("initech").tenant == "initech"
    assert "initech" in src.raw()["tenants"]


def test_save_refuses_what_load_would_refuse(etcd):
    # Review finding 6: a save that every later load refused.
    src = EtcdConfig(etcd.url + "/p")
    bad = {**TENANTS, "tenants": {"acme": {"stores": {"file-globex": {"driver": "file"}}}}}
    with pytest.raises(ConfigError, match="must end with -acme"):
        src.save(bad)
    assert etcd.kv == {}
