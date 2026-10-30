import json
import os

import pytest

from provcat import Catalog, ConfigError, EtcdConfig, NotFound, UriError, YamlConfig, open_config
from provcat.cli import main


@pytest.mark.skipif(not os.environ.get("PROVCAT_SYSTEM_ETCD"), reason="set PROVCAT_SYSTEM_ETCD=host:port to use a real etcd")
def test_live_etcd(tmp_path):
    src = EtcdConfig(f"etcd://{os.environ['PROVCAT_SYSTEM_ETCD']}/provcat-test")
    src.save({"schemes": {"t": {"parts": ["a"], "target": "s3://b/{a}"}}, "stages": {"t://x": {"p": "commit:1"}}})
    cfg = {"schemes": {"t": {"parts": ["a"], "target": "s3://b/{a}"}}, "cache": {"root": "${oc.env:HOME}/c"}}
    src.save(cfg)
    assert src.raw() == cfg
    assert "stages" not in src.load()
    assert src.load()["cache"]["root"] == os.environ["HOME"] + "/c"
