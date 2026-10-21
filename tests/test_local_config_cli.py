import json
import os

import pytest

from provcat import Catalog, ConfigError, EtcdConfig, NotFound, UriError, YamlConfig, open_config
from provcat.cli import main


def test_local_files_are_returned_in_place(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("x")
    c = Catalog(cache_root=tmp_path / "cache")
    assert c.fetch(f"file://{tmp_path}/d/f.txt") == tmp_path / "d" / "f.txt"
    assert [e.uri.segments[-1] for e in c.list(f"file://{tmp_path}/d")] == ["f.txt"]
    assert not (tmp_path / "cache").exists()
    with pytest.raises(NotFound):
        c.fetch(f"file://{tmp_path}/nope")
    with pytest.raises(UriError):
        c.fetch("file://host/x")


def test_local_root_option(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    c = Catalog({"stores": {"data": {"driver": "file", "root": str(tmp_path)}}})
    assert c.read("data:///f.txt") == b"x"


def test_root_refuses_links_that_leave_it(tmp_path):
    # Finding 14: a link inside root let a read reach /etc.
    jail = tmp_path / "jail"
    jail.mkdir()
    (jail / "ok.txt").write_text("ok")
    (jail / "out").symlink_to(tmp_path)
    (tmp_path / "secret.txt").write_text("s")
    c = Catalog({"stores": {"jail": {"driver": "file", "root": str(jail)}}})
    assert c.read("jail:///ok.txt") == b"ok"
    with pytest.raises(UriError, match="leaves the store root"):
        c.read("jail:///out/secret.txt")
    assert [e.uri.segments[-1] for e in c.list("jail:///")] == ["ok.txt"]


def test_store_option_types_are_checked():
    # Finding 13: token_env as a string was read letter by letter.
    with pytest.raises(ConfigError, match="list of strings"):
        Catalog({"stores": {"github": {"token_env": "MYTOKEN"}}})
    with pytest.raises(ConfigError, match="number"):
        Catalog({"stores": {"github": {"timeout": "soon"}}})
    with pytest.raises(ConfigError, match="true or false"):
        Catalog({"stores": {"github": {"use_gh_cli": "no"}}})


def test_yaml_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        YamlConfig(tmp_path / "none.yaml").load()
    bad = tmp_path / "bad.yaml"
    bad.write_text("shemes: {}\n")
    with pytest.raises(ConfigError, match="unknown section"):
        YamlConfig(bad).load()


def test_yaml_interpolates_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_ENDPOINT", "http://e:1")
    f = tmp_path / "c.yaml"
    f.write_text("stores:\n  s3:\n    endpoint_url: ${oc.env:MY_ENDPOINT}\n")
    assert YamlConfig(f).load()["stores"]["s3"]["endpoint_url"] == "http://e:1"


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


def test_ls_into_a_closed_pipe(tmp_path):
    # Second review, finding 9: only cat handled a closed pipe.
    import subprocess
    import sys

    for i in range(3000):
        (tmp_path / f"f{i:05}").write_text("x")
    out = subprocess.run(f"{sys.executable} -m provcat.cli ls file://{tmp_path} | head -c 10; exit ${{PIPESTATUS[0]}}",
                         shell=True, executable="/bin/bash", capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "Broken pipe" not in out.stderr


def test_cli_reports_errors_without_tracebacks(tmp_path, capsys):
    f = tmp_path / "broken.yaml"
    f.write_text("schemes: [unclosed\n")
    assert main(["--config", str(f), "resolve", "s3://b/k"]) == 1
    assert capsys.readouterr().err.startswith("provc: ")
    (tmp_path / "d").mkdir()
    assert main(["cat", f"file://{tmp_path}/d"]) == 1
    assert "folder" in capsys.readouterr().err


def test_cat_into_a_closed_pipe(tmp_path):
    import subprocess
    import sys

    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (1 << 22))
    out = subprocess.run(f"{sys.executable} -m provcat.cli cat file://{big} | head -c 10",
                         shell=True, capture_output=True, text=True)
    assert out.stdout == "x" * 10
    assert "Traceback" not in out.stderr


def test_etcd_unreachable():
    with pytest.raises(ConfigError, match="cannot reach"):
        EtcdConfig("etcd://127.0.0.1:9/p", timeout=1).load()


def test_open_config_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROVCAT_CONFIG", raising=False)
    assert open_config().load() == {}
    (tmp_path / "provcat.yaml").write_text("cache: {root: /tmp/x}\n")
    assert open_config().load() == {"cache": {"root": "/tmp/x"}}
    monkeypatch.setenv("PROVCAT_CONFIG", "etcd://h:1/p")
    assert isinstance(open_config(), EtcdConfig)


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


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("PROVCAT_TEST_ETCD"), reason="set PROVCAT_TEST_ETCD=host:port to use a real etcd")
def test_live_etcd(tmp_path):
    src = EtcdConfig(f"etcd://{os.environ['PROVCAT_TEST_ETCD']}/provcat-test")
    src.save({"schemes": {"t": {"parts": ["a"], "target": "s3://b/{a}"}}, "stages": {"t://x": {"p": "commit:1"}}})
    cfg = {"schemes": {"t": {"parts": ["a"], "target": "s3://b/{a}"}}, "cache": {"root": "${oc.env:HOME}/c"}}
    src.save(cfg)
    assert src.raw() == cfg
    assert "stages" not in src.load()
    assert src.load()["cache"]["root"] == os.environ["HOME"] + "/c"
