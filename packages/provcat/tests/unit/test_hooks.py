"""Hooks at the lifecycle spots, and the proxy configuration that names them."""
import json
from importlib.metadata import entry_points

import pytest

from provcat import Catalog, hooks
from provcat.cli import main
from provcat.errors import ConfigError, StoreError
from provcat.hooks import AccessDenied
from provcat.stores import register_driver
from provcat.stores.local import LocalStore
from provcat.uri import Selector


@register_driver
class HistoryStore(LocalStore):
    """A file store that pretends to commit, branch and merge."""

    driver = "hooktest"

    def commit(self, uri, message, metadata):
        self.last = ("commit", message, metadata)
        return uri.with_selector(Selector("commit", "c1"))

    def branch(self, uri, source):
        return uri

    def merge(self, uri, source, message, metadata, strategy):
        self.last = ("merge", source, message, metadata, strategy)
        return uri.with_selector(Selector("commit", "m1"))


class EP:
    """A stand-in for an installed entry point."""

    def __init__(self, name, fn):
        self.name, self.value, self.fn = name, f"tests:{name}", fn

    def load(self):
        return self.fn


SEEN = []


def recorder(ctx, req, next):
    try:
        out = next(req)
    except Exception as e:
        SEEN.append((req.spot, req.logical and str(req.logical), req.physical and str(req.physical),
                     type(e).__name__))
        raise
    SEEN.append((req.spot, req.logical and str(req.logical), req.physical and str(req.physical), "ok"))
    return out


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    """Write a hooks file and install the test hooks; returns a writer."""
    SEEN.clear()
    installed = {"t.rec": EP("t.rec", recorder),
                 "provcat.logical_only": EP("provcat.logical_only", hooks.logical_only),
                 "provcat.log": EP("provcat.log", hooks.log)}
    monkeypatch.setattr(hooks, "_registered", lambda: installed)
    path = tmp_path / "hooks.yaml"
    monkeypatch.setenv("PROVCAT_HOOKS", str(path))

    def write(apps, app=None):
        path.write_text(json.dumps({"apps": apps}))
        if app:
            monkeypatch.setenv("PROVCAT_APP", app)
        hooks._forget_process_app()
        return installed

    return write


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "root"
    (root / "ds" / "sub").mkdir(parents=True)
    (root / "ds" / "a.csv").write_text("A")
    (root / "ds" / "sub" / "b.csv").write_text("B")
    return {"stores": {"data": {"driver": "hooktest", "root": str(root)}},
            "schemes": {"d": {"parts": ["name"], "rest": "path", "target": "data:///{name}/{path}"}}}


def test_provcat_registers_its_hooks():
    names = {ep.name: ep.value for ep in entry_points(group=hooks.GROUP)}
    assert names["provcat.log"] == "provcat.hooks:log"
    assert names["provcat.logical_only"] == "provcat.hooks:logical_only"


def test_no_file_and_no_app_means_no_hooks(tmp_path, data):
    c = Catalog(data, cache_root=tmp_path / "c")
    assert c.app is None and all(not chain for chain in c.hooks.chains.values())
    assert c.read("d://ds/a.csv") == b"A"


def test_a_named_app_with_no_file_is_refused(tmp_path, monkeypatch, data):
    monkeypatch.setenv("PROVCAT_APP", "nb")
    hooks._forget_process_app()
    with pytest.raises(ConfigError, match="does not exist, so app 'nb' has no hooks"):
        Catalog(data, cache_root=tmp_path / "c")


def test_a_named_hooks_file_that_is_missing_is_refused(tmp_path, monkeypatch, data):
    monkeypatch.setenv("PROVCAT_HOOKS", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="does not exist"):
        Catalog(data, cache_root=tmp_path / "c")


def test_a_named_app_with_no_section_is_refused(proxy, tmp_path, data):
    proxy({"default": {}}, app="nb")
    with pytest.raises(ConfigError, match="no app 'nb'"):
        Catalog(data, cache_root=tmp_path / "c")


def test_with_no_app_the_default_section_applies(proxy, tmp_path, data):
    proxy({"default": {"*": ["t.rec"]}})
    c = Catalog(data, cache_root=tmp_path / "c")
    assert c.app is None and c.context.app == "default"
    c.stat("d://ds/a.csv")
    assert ("stat", "d://ds/a.csv", "data:///ds/a.csv", "ok") in SEEN


def test_with_no_app_and_no_default_section_there_are_no_hooks(proxy, tmp_path, data):
    proxy({"nb": {"*": ["provcat.logical_only"]}})
    c = Catalog(data, cache_root=tmp_path / "c")
    assert c.read("data:///ds/a.csv") == b"A"


def test_the_hooks_file_is_read_once(proxy, tmp_path, monkeypatch, data):
    proxy({"nb": {"*": ["provcat.logical_only"]}}, app="nb")
    with pytest.raises(AccessDenied):
        Catalog(data, cache_root=tmp_path / "c").read("data:///ds/a.csv")
    open_file = tmp_path / "open.yaml"
    open_file.write_text(json.dumps({"apps": {"nb": {}}}))
    monkeypatch.setenv("PROVCAT_HOOKS", str(open_file))
    with pytest.raises(AccessDenied):
        Catalog(data, cache_root=tmp_path / "c").read("data:///ds/a.csv")
    (tmp_path / "hooks.yaml").write_text(json.dumps({"apps": {"nb": {}}}))
    with pytest.raises(AccessDenied):
        Catalog(data, cache_root=tmp_path / "c").read("data:///ds/a.csv")


def test_a_cache_root_may_start_with_a_tilde(proxy, tmp_path, monkeypatch, data):
    monkeypatch.setenv("HOME", str(tmp_path))
    proxy({"nb": {"cache_root": "~/nbcache"}}, app="nb")
    assert Catalog(data).cache.root == tmp_path / "nbcache"


@pytest.mark.parametrize("fn, message", [
    (None, "does not load: boom"),
    (42, "is not a function"),
])
def test_a_hook_that_does_not_load_is_named(proxy, tmp_path, data, fn, message):
    installed = proxy({"nb": {"fetch": ["t.x"]}}, app="nb")

    class Broken(EP):
        def load(self):
            if self.fn is None:
                raise ImportError("boom")
            return self.fn

    installed["t.x"] = Broken("t.x", fn)
    with pytest.raises(ConfigError, match=message):
        Catalog(data, cache_root=tmp_path / "c")


@pytest.mark.parametrize("text, message", [
    ("", "must hold one section"),
    ("apps:\n  nb: [", "cannot read"),
    (b"\xff\xfe", "cannot read"),
])
def test_a_hooks_file_that_is_not_yaml_is_named(proxy, tmp_path, data, monkeypatch, text, message):
    proxy({}, app="nb")
    f = tmp_path / "hooks.yaml"
    f.write_bytes(text) if isinstance(text, bytes) else f.write_text(text)
    with pytest.raises(ConfigError, match=message):
        Catalog(data, cache_root=tmp_path / "c")


def test_a_null_app_section_has_no_hooks(proxy, tmp_path, data):
    proxy({"nb": None}, app="nb")
    assert Catalog(data, cache_root=tmp_path / "c").read("data:///ds/a.csv") == b"A"


def test_a_snapshot_of_a_store_that_pins_in_fetch_pins_once(proxy, tmp_path, data, monkeypatch):
    proxy({"nb": {"*": ["t.rec"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    monkeypatch.setattr(c.stores["data"], "fetch_pins", True, raising=False)
    SEEN.clear()
    c.snapshot("d://ds/a.csv")
    assert [s for s, *_ in SEEN] == ["resolve", "pin", "fetch", "snapshot"]


def test_a_refusal_at_close_is_reported_by_provc(proxy, tmp_path, data, capsys):
    def no_close(ctx, req, next):
        if req.args.get("event") == "close":
            raise AccessDenied("not now")
        return next(req)

    installed = proxy({"nb": {"session": ["t.close"]}}, app="nb")
    installed["t.close"] = EP("t.close", no_close)
    cfg = tmp_path / "provcat.yaml"
    cfg.write_text(json.dumps(data))
    assert main(["--config", str(cfg), "--cache", str(tmp_path / "c"), "cat", "d://ds/a.csv"]) == 1
    assert main(["--config", str(cfg), "--cache", str(tmp_path / "c"), "cat", "d://ds/no.csv"]) == 1
    err = capsys.readouterr().err
    assert err.count("provc: not now") == 2 and "no.csv" in err


def test_with_keeps_the_error_of_its_body(proxy, tmp_path, data):
    installed = proxy({"nb": {"session": ["t.close"]}}, app="nb")
    installed["t.close"] = EP("t.close", lambda ctx, req, next: (_ for _ in ()).throw(AccessDenied("x"))
                              if req.args["event"] == "close" else next(req))
    with pytest.raises(KeyError):
        with Catalog(data, cache_root=tmp_path / "c"):
            raise KeyError("body")


@pytest.mark.parametrize("apps, message", [
    ({"nb": {"fetc": ["t.rec"]}}, r"unknown keys \['fetc'\]"),
    ({"nb": {"fetch": ["nosuch"]}}, "no hook named 'nosuch' is installed"),
    ({"nb": {"*": ["t.rec"], "fetch": ["t.rec"]}}, r"\['t.rec'\] would run twice at fetch"),
    ({"nb": {"fetch": "t.rec"}}, "must be a list"),
    ({"nb": {"cache_root": 3}}, "must be a path"),
    ({"nb": []}, "must be a mapping"),
])
def test_a_bad_hooks_file_is_named(proxy, tmp_path, data, apps, message):
    proxy(apps, app="nb")
    with pytest.raises(ConfigError, match=message):
        Catalog(data, cache_root=tmp_path / "c")


def test_two_packages_with_one_hook_name_are_refused(monkeypatch):
    class Fake:
        def __init__(self, value):
            self.name, self.value = "x", value

    monkeypatch.setattr(hooks, "entry_points", lambda group: [Fake("a:f"), Fake("b:f")])
    with pytest.raises(ConfigError, match="two packages register the hook 'x'"):
        hooks._registered()


def test_the_app_is_read_once_and_cannot_be_changed(proxy, tmp_path, monkeypatch, data):
    proxy({"nb": {}, "free": {}}, app="nb")
    assert Catalog(data, cache_root=tmp_path / "c").app == "nb"
    monkeypatch.setenv("PROVCAT_APP", "free")  # too late
    assert Catalog(data, cache_root=tmp_path / "c").app == "nb"
    with pytest.raises(ConfigError, match="cannot open the catalog as 'free'"):
        Catalog(data, cache_root=tmp_path / "c", app="free")
    assert Catalog(data, cache_root=tmp_path / "c", app="nb").app == "nb"


def test_with_no_app_code_cannot_name_one(tmp_path, data):
    with pytest.raises(ConfigError, match="this process is app None"):
        Catalog(data, cache_root=tmp_path / "c", app="nb")


def test_the_cache_root_comes_from_the_argument_then_the_app_then_the_config(proxy, tmp_path, data):
    proxy({"nb": {"cache_root": str(tmp_path / "app")}}, app="nb")
    cfg = {**data, "cache": {"root": str(tmp_path / "cfg")}}
    assert Catalog(cfg, cache_root=tmp_path / "arg").cache.root == tmp_path / "arg"
    assert Catalog(cfg).cache.root == tmp_path / "app"
    proxy({"nb": {}})
    assert Catalog(cfg, app="nb").cache.root == tmp_path / "cfg"


def test_each_spot_gets_the_logical_and_the_physical_uri(proxy, tmp_path, data):
    proxy({"nb": {"*": ["t.rec"]}}, app="nb")
    with Catalog(data, cache_root=tmp_path / "c") as c:
        c.read("d://ds/a.csv")
        c.list("d://ds")
    assert SEEN == [
        ("session", None, None, "ok"),
        ("resolve", "d://ds/a.csv", None, "ok"),
        ("fetch", "d://ds/a.csv", "data:///ds/a.csv", "ok"),
        ("resolve", "d://ds", None, "ok"),
        ("list", "d://ds", "data:///ds", "ok"),
        ("session", None, None, "ok"),
    ]


def test_a_physical_uri_runs_no_resolve_and_has_no_logical(proxy, tmp_path, data):
    proxy({"nb": {"*": ["t.rec"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    SEEN.clear()
    c.stat("data:///ds/a.csv")
    assert SEEN == [("stat", None, "data:///ds/a.csv", "ok")]


def test_a_store_that_pins_in_fetch_is_pinned_first(proxy, tmp_path, data, monkeypatch):
    proxy({"nb": {"pin": ["t.rec"], "fetch": ["t.rec"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    monkeypatch.setattr(c.stores["data"], "fetch_pins", True, raising=False)
    calls = []
    monkeypatch.setattr(c.stores["data"], "pin", lambda u, cache: calls.append(u) or u)
    c.read("d://ds/a.csv")
    assert [s for s, *_ in SEEN] == ["pin", "fetch"] and len(calls) == 1


def test_the_gate_refuses_physical_uris_and_the_recorder_sees_it(proxy, tmp_path, data):
    proxy({"nb": {"*": ["t.rec", "provcat.logical_only"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    assert c.read("d://ds/a.csv") == b"A"
    with pytest.raises(AccessDenied, match="only logical URIs"):
        c.read("data:///ds/a.csv")
    with pytest.raises(AccessDenied):
        c.put("data:///ds/new.csv", b"x")
    assert not (tmp_path / "root" / "ds" / "new.csv").exists()
    assert ("fetch", None, "data:///ds/a.csv", "AccessDenied") in SEEN


def test_a_gate_at_resolve_stops_the_lookup(proxy, tmp_path, data):
    def deny(ctx, req, next):
        raise AccessDenied(f"{ctx.app} may not use {req.logical}")

    installed = proxy({"nb": {"resolve": ["t.deny"]}}, app="nb")
    installed["t.deny"] = EP("t.deny", deny)
    c = Catalog(data, cache_root=tmp_path / "c")
    with pytest.raises(AccessDenied, match="nb may not use d://ds/a.csv"):
        c.fetch("d://ds/a.csv")
    assert c.read("data:///ds/a.csv") == b"A"  # a physical URI has no lookup


def test_a_snapshot_runs_pin_and_fetch_with_the_logical_uri(proxy, tmp_path, data):
    proxy({"nb": {"*": ["t.rec"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    SEEN.clear()
    snap = c.snapshot("d://ds/a.csv")
    assert snap.path.read_text() == "A"
    assert SEEN == [
        ("resolve", "d://ds/a.csv", None, "ok"),
        ("pin", "d://ds/a.csv", "data:///ds/a.csv", "ok"),
        ("fetch", "d://ds/a.csv", "data:///ds/a.csv", "ok"),
        ("snapshot", "d://ds/a.csv", "data:///ds/a.csv", "ok"),
    ]


def test_each_file_of_a_folder_put_runs_put_with_its_logical_uri(proxy, tmp_path, data):
    proxy({"nb": {"put": ["t.rec"]}}, app="nb")
    src = tmp_path / "src"
    (src / "s").mkdir(parents=True)
    (src / "x").write_text("X")
    (src / "s" / "y").write_text("Y")
    Catalog(data, cache_root=tmp_path / "c").put("d://out/dir", src)
    assert SEEN == [("put", "d://out/dir/s/y", "data:///out/dir/s/y", "ok"),
                    ("put", "d://out/dir/x", "data:///out/dir/x", "ok")]


def test_history_spots_see_the_message_and_the_source(proxy, tmp_path, data):
    got = []

    def watch(ctx, req, next):
        got.append((req.spot, dict(req.args)))
        return next(req)

    installed = proxy({"nb": {"commit": ["t.watch"], "branch": ["t.watch"], "merge": ["t.watch"],
                              "delete": ["t.watch"]}}, app="nb")
    installed["t.watch"] = EP("t.watch", watch)
    c = Catalog(data, cache_root=tmp_path / "c")
    pinned = c.commit("d://ds", "first", {"k": "v"})
    assert pinned.selector == Selector("commit", "c1")
    c.branch("data:///ds", "main")
    c.merge("d://ds", "x", "m", strategy="source-wins")
    c.delete("d://ds/a.csv")
    assert got == [
        ("commit", {"message": "first", "metadata": {"k": "v", "provcat.uri": "d://ds"}}),
        ("branch", {"source": "main"}),
        ("merge", {"source": "x", "message": "m", "metadata": {"provcat.uri": "d://ds"},
                   "strategy": "source-wins"}),
        ("delete", {}),
    ]
    assert c.stores["data"].last[3] == {"provcat.uri": "d://ds"}


def test_a_store_with_no_commits_is_refused_before_the_hooks(proxy, tmp_path):
    proxy({"nb": {"*": ["t.rec"]}}, app="nb")
    c = Catalog(cache_root=tmp_path / "c")
    SEEN.clear()
    with pytest.raises(StoreError, match="has no commits"):
        c.commit(f"file://{tmp_path}", "x")
    assert SEEN == []


def test_a_hook_cannot_change_the_spot(proxy, tmp_path, data):
    from dataclasses import replace

    installed = proxy({"nb": {"stat": ["t.bad"]}}, app="nb")
    installed["t.bad"] = EP("t.bad", lambda ctx, req, next: next(replace(req, spot="fetch")))
    c = Catalog(data, cache_root=tmp_path / "c")
    with pytest.raises(Exception, match="changed the spot"):
        c.stat("d://ds/a.csv")


def test_the_log_hook_writes_one_json_line_each(proxy, tmp_path, data, capsys):
    proxy({"nb": {"fetch": ["provcat.log"]}}, app="nb")
    c = Catalog(data, cache_root=tmp_path / "c")
    path = c.fetch("d://ds/a.csv")
    with pytest.raises(StoreError):
        c.fetch("d://ds/none.csv")
    ok, bad = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert ok == {"app": "nb", "spot": "fetch", "logical": "d://ds/a.csv",
                  "physical": "data:///ds/a.csv", "answer": str(path)}
    assert bad["error"].startswith("NotFound")


def test_provc_reports_a_refusal(proxy, tmp_path, data, capsys):
    proxy({"nb": {"*": ["provcat.logical_only"]}}, app="nb")
    cfg = tmp_path / "provcat.yaml"
    cfg.write_text(json.dumps(data))
    assert main(["--config", str(cfg), "--cache", str(tmp_path / "c"), "cat", "d://ds/a.csv"]) == 0
    assert main(["--config", str(cfg), "--cache", str(tmp_path / "c"), "cat", "data:///ds/a.csv"]) == 1
    assert "only logical URIs" in capsys.readouterr().err


# Hooks that a caller, such as an orchestrator, passes in

def test_a_caller_passes_hooks_with_no_file(tmp_path, data):
    seen = []

    def watch(ctx, req, next):
        seen.append((ctx.app, ctx.principal, req.spot))
        return next(req)

    c = Catalog(data, cache_root=tmp_path / "c", hooks={"*": [watch]}, app="ingest", principal="alice")
    assert c.read("d://ds/a.csv") == b"A"
    assert seen == [("ingest", "alice", "session"), ("ingest", "alice", "resolve"),
                    ("ingest", "alice", "fetch")]


def test_two_apps_share_one_process(tmp_path, data):
    nb = Catalog(data, cache_root=tmp_path / "c", hooks={"*": [hooks.logical_only]}, app="nb")
    free = Catalog(data, cache_root=tmp_path / "c", hooks={}, app="free")
    with pytest.raises(AccessDenied):
        nb.read("data:///ds/a.csv")
    assert free.read("data:///ds/a.csv") == b"A"
    assert hooks.process_app() is None


def test_a_caller_passes_an_app_loaded_from_a_file(proxy, tmp_path, data, monkeypatch):
    proxy({"nb": {"cache_root": str(tmp_path / "nbc"), "*": ["provcat.logical_only"]}, "free": {}})
    monkeypatch.delenv("PROVCAT_HOOKS")
    nb = Catalog(data, hooks=hooks.load("nb", tmp_path / "hooks.yaml"))
    assert nb.app == "nb" and nb.cache.root == tmp_path / "nbc"
    with pytest.raises(AccessDenied):
        nb.read("data:///ds/a.csv")
    free = Catalog(data, cache_root=tmp_path / "c", hooks=hooks.load("free", tmp_path / "hooks.yaml"))
    assert free.read("data:///ds/a.csv") == b"A"
    with pytest.raises(ConfigError, match="those of app 'nb', not 'x'"):
        Catalog(data, hooks=hooks.load("nb", tmp_path / "hooks.yaml"), app="x")


def test_a_launched_process_refuses_hooks_from_a_caller(proxy, tmp_path, data):
    proxy({"nb": {"*": ["provcat.logical_only"]}}, app="nb")
    for kw in ({"hooks": {}}, {"hooks": hooks.Hooks()}, {"principal": "root"}):
        with pytest.raises(ConfigError, match="started as app 'nb'"):
            Catalog(data, cache_root=tmp_path / "c", **kw)


@pytest.mark.parametrize("chains, message", [
    ({"fetc": []}, r"unknown spots \['fetc'\]"),
    ({"fetch": [42]}, "must be a list of functions"),
    ({"fetch": recorder}, "must be a list of functions"),
    ({"*": [recorder], "fetch": [recorder]}, "would run twice at fetch"),
    ({1: [], "zz": []}, r"unknown spots \['zz', 1\]"),
    ([("fetch", [])], "must be a mapping of spot to hooks, not list"),
    ("ab", "must be a mapping of spot to hooks, not str"),
])
def test_bad_hooks_from_a_caller_are_refused(chains, message):
    with pytest.raises(ConfigError, match=message):
        hooks.Hooks(chains)


def test_hooks_of_another_type_are_refused(tmp_path, data):
    with pytest.raises(ConfigError, match="not list"):
        Catalog(data, cache_root=tmp_path / "c", hooks=[recorder])


class Watcher:
    def __init__(self):
        self.seen = []

    def hook(self, ctx, req, next):
        self.seen.append(req.spot)
        return next(req)


def test_a_bound_method_cannot_run_twice_at_a_spot():
    w = Watcher()
    with pytest.raises(ConfigError, match="would run twice at fetch"):
        hooks.Hooks({"*": [w.hook], "fetch": [w.hook]})


def test_a_read_only_mapping_of_hooks_is_taken(tmp_path, data):
    from types import MappingProxyType

    w = Watcher()
    c = Catalog(data, cache_root=tmp_path / "c", hooks=MappingProxyType({"fetch": [w.hook]}))
    c.read("d://ds/a.csv")
    assert w.seen == ["fetch"]


def test_a_proxy_must_hold_a_hooks(tmp_path, data):
    with pytest.raises(ConfigError, match="must be a Hooks, not dict"):
        Catalog(data, cache_root=tmp_path / "c", hooks=hooks.Proxy("x", None, {"fetch": []}))


def test_an_empty_principal_is_refused(tmp_path, data):
    with pytest.raises(ConfigError, match="principal must be a name"):
        Catalog(data, cache_root=tmp_path / "c", principal="")


def test_the_hooks_of_the_process_run_after_a_callers(proxy, tmp_path, data):
    """A launcher that sets only PROVCAT_HOOKS gates the default app; a caller
    can add hooks, and cannot drop the process's, with hooks={} or a principal."""
    proxy({"default": {"*": ["provcat.logical_only", "t.rec"]}})
    for kw in ({"hooks": {}}, {"principal": "mallory"}, {"hooks": hooks.Hooks()},
               {"hooks": hooks.Proxy("x", None, hooks.Hooks())}):
        c = Catalog(data, cache_root=tmp_path / "c", **kw)
        with pytest.raises(AccessDenied):
            c.read("data:///ds/a.csv")
    order = []
    mine = lambda ctx, req, next: (order.append("caller"), next(req))[1]  # noqa: E731
    SEEN.clear()
    c = Catalog(data, cache_root=tmp_path / "c", hooks={"fetch": [mine]}, principal="alice")
    c.read("d://ds/a.csv")
    assert c.context.principal == "alice" and order == ["caller"]
    assert [s for s, *_ in SEEN if s == "fetch"]  # the process's recorder ran too
    assert c.hooks.chains["fetch"] == (mine, hooks.logical_only, recorder)


def test_a_callers_hook_cannot_change_a_request_past_the_process_gate(proxy, tmp_path, data):
    import dataclasses

    from provcat.uri import parse

    proxy({"default": {"*": ["provcat.logical_only"]}})

    def swap(ctx, req, next):  # turns a logical read into a physical one
        return next(dataclasses.replace(req, logical=None, physical=parse("data:///ds/sub/b.csv")))

    c = Catalog(data, cache_root=tmp_path / "c", hooks={"fetch": [swap]})
    with pytest.raises(AccessDenied):
        c.read("d://ds/a.csv")


def test_a_principal_alone_keeps_the_hooks_of_the_process(proxy, tmp_path, data):
    proxy({"default": {"fetch": ["t.rec"]}})
    c = Catalog(data, cache_root=tmp_path / "c", principal="alice")
    assert c.hooks.chains["fetch"] == (recorder,) and c.context.principal == "alice"


@pytest.mark.parametrize("principal", [" alice", "alice\n", 5])
def test_a_principal_that_is_not_a_plain_name_is_refused(tmp_path, data, principal):
    with pytest.raises(ConfigError, match="principal must be a name"):
        Catalog(data, cache_root=tmp_path / "c", principal=principal)


def test_an_app_of_the_hooks_file_needs_its_hooks(proxy, tmp_path, data):
    proxy({"ingest": {"*": ["provcat.logical_only"]}})
    with pytest.raises(ConfigError, match="has hooks for app 'ingest'"):
        Catalog(data, cache_root=tmp_path / "c", hooks={}, app="ingest")
    other = tmp_path / "other.yaml"
    other.write_text(json.dumps({"apps": {"ingest": {}}}))
    with pytest.raises(ConfigError, match="not those of the hooks file"):
        Catalog(data, cache_root=tmp_path / "c", hooks=hooks.load("ingest", other))
    c = Catalog(data, cache_root=tmp_path / "c", hooks=hooks.load("ingest"))
    with pytest.raises(AccessDenied):
        c.read("data:///ds/a.csv")


def test_a_hooks_with_no_chains_is_refused(tmp_path, data):
    class Bare(hooks.Hooks):
        def __init__(self):
            pass

    with pytest.raises(ConfigError, match="not Bare"):
        Catalog(data, cache_root=tmp_path / "c", hooks=Bare())


def test_a_hooks_own_eq_is_not_asked():
    class Eq:
        def __eq__(self, other):
            return True

        __hash__ = object.__hash__

        def __call__(self, ctx, req, next):
            return next(req)

    a, b = Eq(), Eq()
    h = hooks.Hooks({"fetch": [a, b]})  # two hooks, not one hook twice
    assert not h.same_as(hooks.Hooks({"fetch": [b, a]}))


def test_the_chains_of_a_catalog_cannot_be_cleared(tmp_path, data):
    c = Catalog(data, cache_root=tmp_path / "c", hooks={"fetch": [recorder]})
    with pytest.raises(AttributeError):
        c.hooks.chains["fetch"].clear()
