from provcat.stores.github import GithubStore, lfs_pointer

OID = "a" * 64


def test_pointer_is_parsed():
    assert lfs_pointer(f"version https://git-lfs.github.com/spec/v1\noid sha256:{OID}\nsize 12\n".encode()) == (OID, 12, False)


def test_pointer_with_extensions_is_parsed():
    ptr = (f"version https://git-lfs.github.com/spec/v1\next-0-foo sha256:{'b' * 64}\n"
           f"oid sha256:{OID}\nsize 0\n").encode()
    assert lfs_pointer(ptr) == (OID, 0, True)


def test_other_files_are_not_pointers():
    good = f"version https://git-lfs.github.com/spec/v1\noid sha256:{OID}\nsize 12\n"
    for bad in (good.replace("sha256", "md5"), good + "extra\n", good[:-1], good.replace("12", "-1"),
                good.upper(), "hello\n", "", good.replace("\n", "\r\n")):
        assert lfs_pointer(bad.encode()) is None
    assert lfs_pointer(good.encode() + b" " * 1024) is None


def test_web_host_of_each_api_url():
    def web(api):
        return GithubStore("github", {"api_url": api})._web()
    assert web("https://api.github.com") == "https://github.com"
    assert web("https://api.acme.ghe.com") == "https://acme.ghe.com"  # GHE.com, with data residency
    assert web("https://ghe.example.com/api/v3") == "https://ghe.example.com"
    assert web("https://ghe.example.com/api/v3/") == "https://ghe.example.com"
    assert web("http://127.0.0.1:9999") == "http://127.0.0.1:9999"


def test_redirects_go_only_to_http_and_https():
    import urllib.request
    from provcat.stores.github import _DropAuthOnHostChange
    req = urllib.request.Request("https://example.com/a", headers={"Authorization": "x"})
    h = _DropAuthOnHostChange()
    assert h.redirect_request(req, None, 302, "Found", {}, "ftp://example.com/a") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd") is None
    assert h.redirect_request(req, None, 302, "Found", {}, "https://example.com/b") is not None
