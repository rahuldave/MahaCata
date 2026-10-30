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


def test_open_config_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROVCAT_CONFIG", raising=False)
    assert open_config().load() == {}
    (tmp_path / "provcat.yaml").write_text("cache: {root: /tmp/x}\n")
    assert open_config().load() == {"cache": {"root": "/tmp/x"}}
    monkeypatch.setenv("PROVCAT_CONFIG", "etcd://h:1/p")
    assert isinstance(open_config(), EtcdConfig)
