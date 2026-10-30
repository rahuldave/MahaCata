import os
import tarfile

import pytest

from provcat import Catalog, NotFound, OfflineMiss, UriError
from provcat import StoreError
from conftest import SHA_A, SHA_B


def cat(github, tmp_path, offline=False):
    cfg = {"stores": {"github": {"api_url": github.api_url, "token_env": ["NO_SUCH_TOKEN_VAR"], "use_gh_cli": False}}}
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


@pytest.mark.skipif(not os.environ.get("PROVCAT_SYSTEM_GITHUB"), reason="set PROVCAT_SYSTEM_GITHUB=1 to call github.com")
def test_live_github(tmp_path):
    c = Catalog(cache_root=tmp_path)
    assert b"Hello" in c.read("github://octocat/Hello-World/README?at=branch:master")
    assert (c.fetch("github://octocat/Hello-World?at=branch:master") / "README").is_file()
