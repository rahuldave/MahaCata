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


@pytest.mark.skipif(not os.environ.get("PROVCAT_SYSTEM_GITHUB"), reason="set PROVCAT_SYSTEM_GITHUB=1 to call github.com")
def test_live_github_ts_follows_first_parents(tmp_path):
    """octocat/Hello-World's master: 553c207 (2011-01-26), then the merge
    7fd1a60 (2012-03-06) of 7629413, a side commit from 2011-09-14. On
    2011-12-01 master was still at 553c207."""
    c = Catalog(cache_root=tmp_path)
    at = lambda ts: c.pin(f"github://octocat/Hello-World/README?at=ts:{ts}").selector.value  # noqa: E731
    assert at("master@2011-12-01") == "553c2077f0edc3d5dc5d17262f6aa498e69d6f8e"
    assert at("2012-03-07") == "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
    with pytest.raises(NotFound, match="no commit at or before"):
        at("2011-01-01")
