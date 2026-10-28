import os
import tarfile

import pytest

from provcat import Catalog, NotFound, OfflineMiss, UriError
from provcat import StoreError
from conftest import SHA_A, SHA_B


def cat(github, tmp_path, offline=False):
    cfg = {"stores": {"github": {"api_url": github.api_url, "token_env": ["NO_SUCH_TOKEN_VAR"], "use_gh_cli": False}}}
    return Catalog(cfg, cache_root=tmp_path / "cache", offline=offline)


def test_zero_timeout_is_refused():
    from provcat import ConfigError

    with pytest.raises(ConfigError, match="more than 0"):
        Catalog({"stores": {"github": {"timeout": 0}}})


def test_https_to_http_redirect_drops_the_token():
    import urllib.request
    from provcat.stores.github import _DropAuthOnHostChange

    req = urllib.request.Request("https://h.example/x", headers={"Authorization": "Bearer t"})
    h = _DropAuthOnHostChange()
    same = h.redirect_request(req, None, 302, "Found", {}, "https://h.example/y")
    down = h.redirect_request(req, None, 302, "Found", {}, "http://h.example/y")
    assert same.get_header("Authorization") == "Bearer t"
    assert down.get_header("Authorization") is None


def test_gh_cli_is_asked_for_the_api_host(monkeypatch):
    # Finding 11: gh gave the github.com token for any api_url.
    from provcat.stores.github import GithubStore

    calls = []

    class Done:
        returncode, stdout = 0, "tok\n"

    monkeypatch.setattr("provcat.stores.github.shutil.which", lambda _: "/bin/gh")
    monkeypatch.setattr("provcat.stores.github.subprocess.run", lambda args, **kw: calls.append(args) or Done())
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert GithubStore("github", {"api_url": "https://ghe.example.com/api/v3"}).token == "tok"
    assert GithubStore("github", {}).token == "tok"
    assert calls == [["gh", "auth", "token", "--hostname", "ghe.example.com"],
                     ["gh", "auth", "token", "--hostname", "github.com"]]
