"""Hooks: functions that run at the lifecycle spots of the catalog.

Each operation of the catalog passes through a spot, for example ``resolve``,
``pin`` or ``fetch``. A hook at a spot wraps the work of the catalog, as
middleware wraps a request in a web framework:

    def deny_physical(ctx, req, next):
        if req.logical is None and req.spot != "session":
            raise AccessDenied(f"{req.physical}: use a logical name")
        return next(req)

A recorder calls ``next`` and then records the request and the answer. A gate
raises an error, or calls ``next``. Hooks run before the base cache, so a read
from the cache passes the gates too.

A program that runs the catalog for others, such as an orchestrator, passes
its hooks to each Catalog, so several apps can share one process. The hooks
of the process, from the proxy configuration below, run last, next to the
store, so they see the request that runs::

    cat = Catalog(config, hooks=Hooks({"*": [record]}), app="ingest", principal="alice")
    cat = Catalog(config, hooks=hooks.load("ingest", "hooks.yaml"))

Otherwise the proxy configuration gives each app its hooks. It is a local
YAML file, in PROVCAT_HOOKS or at ``~/.config/provcat/hooks.yaml``:

    apps:
      default: {}
      notebook:
        cache_root: ~/.cache/provcat-notebook
        "*": [provcat.log]
        fetch: [provcat.logical_only]

Each name is a Python entry point in the group ``provcat.hooks``. The hooks
under ``"*"`` run at every spot, first. The app comes from PROVCAT_APP, which
the process reads once, when it opens its first catalog.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, ProvcatError
from .uri import Uri

#: The lifecycle spots. A spot can run inside another: fetch inside open or snapshot, put inside close.
SPOTS = ("session", "resolve", "pin", "stat", "list", "fetch", "snapshot",
         "open", "read_block", "close", "put", "delete", "commit", "branch", "merge")
GROUP = "provcat.hooks"
DEFAULT_FILE = Path("~/.config/provcat/hooks.yaml")


class AccessDenied(ProvcatError):
    """A gate hook refused a request."""


@dataclass(frozen=True)
class Context:
    """Who uses the catalog. The principal is the user of the process, or the
    name that a caller passes as principal=."""

    app: str | None
    principal: str
    tenant: str | None


@dataclass(frozen=True)
class Request:
    """One operation at one spot.

    ``logical`` is the logical URI that started the operation, or None for a
    physical URI. ``physical`` is None at ``resolve`` and ``session``, because
    no lookup has run. ``args`` holds the rest: the message of a commit, the
    source of a branch, ``event`` ("open" or "close") at ``session``, ``mode``
    at ``open`` and ``close``, ``offset`` and ``length`` at ``read_block``, and
    the merged ``ranges`` read at ``close``."""

    spot: str
    logical: Uri | None = None
    physical: Uri | None = None
    args: dict[str, Any] = field(default_factory=dict)


Hook = Callable[[Context, Request, Callable[[Request], Any]], Any]


def same(a: Any, b: Any) -> bool:
    """Whether two hooks are one: the same object, or the same method of the
    same object. A hook's own __eq__ is not asked."""
    if a is b:
        return True
    return (hasattr(a, "__self__") and hasattr(a, "__func__") and hasattr(b, "__self__")
            and hasattr(b, "__func__") and a.__self__ is b.__self__ and a.__func__ is b.__func__)


class Hooks:
    """The hook chain of each spot for one app.

    ``chains`` maps a spot to a list of hooks. The hooks under ``"*"`` run at
    every spot, before the hooks of the spot, as in the proxy configuration::

        Hooks({"*": [record], "fetch": [only_logical]})
    """

    def __init__(self, chains: Mapping[str, list[Hook]] | None = None):
        if chains is not None and not isinstance(chains, Mapping):
            raise ConfigError(f"hooks must be a mapping of spot to hooks, not {type(chains).__name__}")
        chains = dict(chains or {})
        unknown = set(chains) - {"*", *SPOTS}
        if unknown:
            raise ConfigError(f"hooks: unknown spots {sorted(unknown, key=repr)}; the spots are {', '.join(SPOTS)}")
        for spot, chain in chains.items():
            if not isinstance(chain, (list, tuple)) or not all(callable(h) for h in chain):
                raise ConfigError(f"hooks: {spot} must be a list of functions")
        self.chains = {}
        for spot in SPOTS:
            both = [*chains.get("*", []), *chains.get(spot, [])]
            # Each access to obj.method makes a new bound method, so compare with same().
            if any(same(h, g) for i, h in enumerate(both) for g in both[:i]):
                raise ConfigError(f"hooks: a hook would run twice at {spot}")
            self.chains[spot] = tuple(both)

    def then(self, other: Hooks) -> Hooks:
        """These hooks, and then other's, at each spot. A hook in both runs
        twice, which a gate or a recorder allows."""
        both = Hooks()
        both.chains = {spot: (*self.chains[spot], *other.chains[spot]) for spot in SPOTS}
        return both

    def same_as(self, other: Hooks) -> bool:
        """Whether other runs the same hooks at each spot, in the same order."""
        return all(len(self.chains[s]) == len(other.chains[s])
                   and all(map(same, self.chains[s], other.chains[s])) for s in SPOTS)

    def run(self, ctx: Context, req: Request, work: Callable[[Request], Any]) -> Any:
        """Run the chain of req.spot around work."""
        chain = self.chains[req.spot]

        def call(i: int, r: Request) -> Any:
            if r.spot != req.spot:
                raise ProvcatError(f"a hook at {req.spot} changed the spot to {r.spot}")
            if i == len(chain):
                return work(r)
            return chain[i](ctx, r, lambda r2: call(i + 1, r2))

        return call(0, req)


@dataclass(frozen=True)
class Proxy:
    """The part of the proxy configuration that one app gets."""

    app: str | None
    cache_root: str | None
    hooks: Hooks


def _registered() -> dict[str, Any]:
    found: dict[str, Any] = {}
    for ep in entry_points(group=GROUP):
        if ep.name in found and found[ep.name].value != ep.value:
            raise ConfigError(f"two packages register the hook {ep.name!r}: "
                              f"{found[ep.name].value} and {ep.value}")
        found[ep.name] = ep
    return found


def _section(path: Path, app: str, data: Any) -> dict:
    if not isinstance(data, dict) or set(data) != {"apps"} or not isinstance(data["apps"], dict):
        raise ConfigError(f"{path}: the file must hold one section, apps:")
    if app not in data["apps"]:
        if app == "default":
            return {}  # no app was named, so no gate is lost
        raise ConfigError(f"{path}: no app {app!r} under apps:")
    sec = data["apps"][app]
    if sec is None:
        sec = {}
    if not isinstance(sec, dict):
        raise ConfigError(f"{path}: apps.{app} must be a mapping")
    unknown = set(sec) - {"cache_root", "*", *SPOTS}
    if unknown:
        raise ConfigError(f"{path}: apps.{app}: unknown keys {sorted(unknown)}; "
                          f"the spots are {', '.join(SPOTS)}")
    return sec


def load(app: str | None, path: str | Path | None = None) -> Proxy:
    """The hooks and the cache root of app, from the proxy configuration at
    path, PROVCAT_HOOKS or ~/.config/provcat/hooks.yaml. With no app and no
    file there are no hooks. A named app with no file or no section is an
    error, so a missing file never turns a gate off quietly."""
    named = path or os.environ.get("PROVCAT_HOOKS")
    file = Path(named or DEFAULT_FILE).expanduser()
    if not file.is_file():
        if app is None and not named:
            return Proxy(None, None, Hooks())
        raise ConfigError(f"the hooks file {file} does not exist"
                          + (f", so app {app!r} has no hooks" if app else ""))
    try:
        data = yaml.safe_load(file.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read the hooks file {file}: {e}") from e
    name = app or "default"
    sec = _section(file, name, data)
    root = sec.get("cache_root")
    if root is not None and not isinstance(root, str):
        raise ConfigError(f"{file}: apps.{name}.cache_root must be a path")
    if root is not None:
        root = str(Path(root).expanduser())
    registered = _registered()
    loaded: dict[str, Hook] = {}

    def hook(spot: str, n: Any) -> Hook:
        if not isinstance(n, str):
            raise ConfigError(f"{file}: apps.{name}.{spot}: {n!r} is not a hook name")
        if n not in registered:
            raise ConfigError(f"{file}: apps.{name}.{spot}: no hook named {n!r} is installed; "
                              f"installed: {', '.join(sorted(registered)) or 'none'}")
        if n not in loaded:
            try:
                fn = registered[n].load()
            except Exception as e:
                raise ConfigError(f"the hook {n!r} ({registered[n].value}) does not load: {e}") from e
            if not callable(fn):
                raise ConfigError(f"the hook {n!r} ({registered[n].value}) is not a function")
            loaded[n] = fn
        return loaded[n]

    def names(spot: str) -> list:
        v = sec.get(spot, [])
        if not isinstance(v, list):
            raise ConfigError(f"{file}: apps.{name}.{spot} must be a list of hook names")
        return v

    chains = {}
    for spot in SPOTS:
        both = [*names("*"), *names(spot)]
        twice = sorted({n for n in both if both.count(n) > 1})
        if twice:
            raise ConfigError(f"{file}: apps.{name}: {twice} would run twice at {spot}")
        chains[spot] = [hook(spot if n in names(spot) else "*", n) for n in both]
    return Proxy(name, root, Hooks(chains))


def given(hooks: Hooks | Proxy | Mapping | None, app: str | None) -> Proxy:
    """The proxy of a caller that passes its own hooks to a Catalog: a Hooks,
    a Proxy from load(), a mapping for Hooks, or None for none of its own.
    The hooks of the process, from PROVCAT_HOOKS or the default file, run
    last, next to the store, so they see the request that runs; a caller
    adds hooks and never drops or gets round one. A process that a launcher
    started with PROVCAT_APP refuses. An app that the hooks file names is
    refused unless the caller passes its Proxy from load()."""
    launched = process_app()
    if launched is not None:
        raise ConfigError(f"this process was started as app {launched!r}, so its hooks come from "
                          f"the hooks file; a caller cannot pass its own")
    own = process_proxy()
    if isinstance(hooks, Proxy):
        if app is not None and app != hooks.app:
            raise ConfigError(f"the hooks given are those of app {hooks.app!r}, not {app!r}")
        if not isinstance(hooks.hooks, Hooks) or not isinstance(getattr(hooks.hooks, "chains", None), dict):
            raise ConfigError(f"the Proxy's hooks must be a Hooks, not {type(hooks.hooks).__name__}")
        if hooks.app in _process_file_apps and not hooks.hooks.same_as(load(hooks.app).hooks):
            raise ConfigError(f"the hooks given for app {hooks.app!r} are not those of the hooks file")
        return Proxy(hooks.app, hooks.cache_root or own.cache_root, hooks.hooks.then(own.hooks))
    if app is not None and app in _process_file_apps:
        raise ConfigError(f"the hooks file has hooks for app {app!r}; pass hooks.load({app!r}) "
                          f"to open the catalog as that app")
    if isinstance(hooks, Mapping):
        hooks = Hooks(hooks)
    if hooks is not None and not isinstance(getattr(hooks, "chains", None), dict):
        raise ConfigError(f"hooks must be a Hooks, a Proxy or a mapping, not {type(hooks).__name__}")
    return Proxy(app if app is not None else own.app, own.cache_root, (hooks or Hooks()).then(own.hooks))


# The app of this process and its part of the proxy configuration, read once.
_UNREAD = object()
_process_app: Any = _UNREAD
_process_proxy: Proxy | None = None
_process_file_apps: frozenset[str] = frozenset()  # the apps of the hooks file, read with it
_lock = threading.Lock()


def process_app(app: str | None = None) -> str | None:
    """The app of this process. PROVCAT_APP is read once, at the first call.
    A later call may name only that app, so code in the process cannot pick
    an app with fewer hooks."""
    global _process_app
    with _lock:
        if _process_app is _UNREAD:
            _process_app = os.environ.get("PROVCAT_APP") or None
    if app is not None and app != _process_app:
        raise ConfigError(f"this process is app {_process_app!r}; it cannot open the catalog as {app!r}")
    return _process_app


def process_proxy(app: str | None = None) -> Proxy:
    """The hooks of this process. The hooks file is read once, at the first
    catalog that opens without an error. A later change to PROVCAT_HOOKS or to
    the file does not change the hooks."""
    global _process_proxy, _process_file_apps
    name = process_app(app)
    with _lock:
        if _process_proxy is None:
            _process_proxy = load(name)
            _process_file_apps = _apps_in_file()
        return _process_proxy


def _apps_in_file() -> frozenset[str]:
    """The apps that the hooks file names, after load() has read it once."""
    file = Path(os.environ.get("PROVCAT_HOOKS") or DEFAULT_FILE).expanduser()
    try:
        data = yaml.safe_load(file.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return frozenset()
    apps = data.get("apps") if isinstance(data, dict) else None
    return frozenset(a for a in apps if isinstance(a, str) and a != "default") if isinstance(apps, dict) else frozenset()


def _forget_process_app() -> None:
    """For tests: read PROVCAT_APP and the hooks file again at the next call."""
    global _process_app, _process_proxy, _process_file_apps
    with _lock:
        _process_app = _UNREAD
        _process_proxy = None
        _process_file_apps = frozenset()


def context(app: str | None, tenant: str | None, principal: str | None = None) -> Context:
    """Who uses a catalog: the app, the principal (by default the user of the
    process) and the tenant."""
    if principal is not None:
        if not isinstance(principal, str) or not principal.isprintable() or principal.strip() != principal \
                or not principal:
            raise ConfigError(f"the principal must be a name, not {principal!r}")
        return Context(app, principal, tenant)
    try:
        who = getpass.getuser()
    except (KeyError, OSError):
        who = str(os.getuid()) if hasattr(os, "getuid") else "unknown"
    return Context(app, who, tenant)


# Hooks that come with provcat

def log(ctx: Context, req: Request, next: Callable[[Request], Any]) -> Any:
    """A recorder: one JSON line on stderr for each request, after it ends."""
    line = {"app": ctx.app, "spot": req.spot,
            "logical": str(req.logical) if req.logical else None,
            "physical": str(req.physical) if req.physical else None}
    line.update({k: str(v) for k, v in req.args.items()})
    try:
        out = next(req)
    except Exception as e:
        line["error"] = f"{type(e).__name__}: {e}"
        print(json.dumps(line), file=sys.stderr)
        raise
    if out is not None:
        line["answer"] = str(getattr(out, "physical", out))
    print(json.dumps(line), file=sys.stderr)
    return out


def logical_only(ctx: Context, req: Request, next: Callable[[Request], Any]) -> Any:
    """A gate: refuse a physical URI, so the app reads and writes through the
    names of its configuration. It holds back code that cooperates; code that
    writes its own configuration can name any place."""
    if req.spot != "session" and req.logical is None:
        raise AccessDenied(f"{req.physical}: app {ctx.app or 'default'} reads and writes "
                           f"only logical URIs")
    return next(req)
