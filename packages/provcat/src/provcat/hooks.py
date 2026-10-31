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

The proxy configuration gives each app its hooks. It is a local YAML file, in
PROVCAT_HOOKS or at ``~/.config/provcat/hooks.yaml``:

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
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, ProvcatError
from .uri import Uri

#: The lifecycle spots, in the order the catalog meets them.
SPOTS = ("session", "resolve", "pin", "stat", "list", "fetch", "snapshot",
         "put", "delete", "commit", "branch", "merge")
GROUP = "provcat.hooks"
DEFAULT_FILE = Path("~/.config/provcat/hooks.yaml")


class AccessDenied(ProvcatError):
    """A gate hook refused a request."""


@dataclass(frozen=True)
class Context:
    """Who uses the catalog. The principal is the user of the process until
    profiles come."""

    app: str | None
    principal: str
    tenant: str | None


@dataclass(frozen=True)
class Request:
    """One operation at one spot.

    ``logical`` is the logical URI that started the operation, or None for a
    physical URI. ``physical`` is None at ``resolve`` and ``session``, because
    no lookup has run. ``args`` holds the rest: the message of a commit, the
    source of a branch, or ``event`` ("open" or "close") at ``session``."""

    spot: str
    logical: Uri | None = None
    physical: Uri | None = None
    args: dict[str, Any] = field(default_factory=dict)


Hook = Callable[[Context, Request, Callable[[Request], Any]], Any]


class Hooks:
    """The hook chain of each spot for one app."""

    def __init__(self, chains: dict[str, list[Hook]] | None = None):
        self.chains = {s: list((chains or {}).get(s, [])) for s in SPOTS}

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


# The app of this process and its part of the proxy configuration, read once.
_UNREAD = object()
_process_app: Any = _UNREAD
_process_proxy: Proxy | None = None
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
    global _process_proxy
    name = process_app(app)
    with _lock:
        if _process_proxy is None:
            _process_proxy = load(name)
        return _process_proxy


def _forget_process_app() -> None:
    """For tests: read PROVCAT_APP and the hooks file again at the next call."""
    global _process_app, _process_proxy
    with _lock:
        _process_app = _UNREAD
        _process_proxy = None


def context(app: str | None, tenant: str | None) -> Context:
    """Who uses a catalog: the app, the user of the process and the tenant."""
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
