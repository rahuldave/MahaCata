"""Configuration sources.

A configuration is a dict with up to seven sections:

- ``stores``: named store instances, each ``{driver: ..., <options>}``.
- ``schemes``: logical URI schemes and their templates (see resolver.py).
- ``stages``: stage pointers, ``{logical-uri: {stage-name: "kind:value"}}``.
- ``entries``: rules for one logical name or prefix, ``{logical-uri: {target: ..., ref: ...}}``
  (see resolver.py).
- ``cache``: ``{root: <folder>}``.
- ``tenants``: ``{name: {stores: ..., stages: ..., entries: ..., cache: ...}}``, the part of
  each tenant. The schemes are shared.
- ``admin``: ``{stores: ..., stages: ..., entries: ..., cache: ...}``, the part that only the
  admin sees, such as a store with keys that read every tenant's data.

``view(cfg, tenant)`` gives the configuration that one tenant sees: the shared
sections and that tenant's part. With no tenant it gives the admin's view, the
shared sections, the admin part and every tenant's part. A tenant may name only its own
stores, stages and entries, so that no tenant can take over another's. Each scheme that
names its tenant part, with ``tenant: org``, says whose a URI is.

In etcd each tenant's part lives under ``<prefix>/tenants/<name>/<section>``,
``<prefix>/tenants/<name>/name`` marks the tenant, and ``<prefix>/tenants``
lists the names. The admin part lives under
``<prefix>/admin/<section>``. A reader for one tenant reads only the shared
keys and its own, so etcd's access control on key prefixes can keep the
tenants apart.

ConfigSource is the interface. ``load()`` returns the configuration with every
``${oc.env:VAR}`` filled in from the environment of the reader. ``raw()`` returns
it as written, with the ``${...}`` left in. ``config push`` copies the raw form,
so each reader of a shared etcd fills in its own environment.

YamlConfig reads a YAML file with PyYAML. EtcdConfig keeps each section as
JSON under ``<prefix>/<section>`` and talks to the etcd v3 HTTP gateway, so it
needs no etcd client library. ``save()`` replaces all sections in one etcd
transaction, and deletes the sections that the new configuration does not have.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .errors import ConfigError

SECTIONS = ("stores", "schemes", "stages", "entries", "cache", "tenants", "admin")
TENANT_SECTIONS = ("stores", "stages", "entries", "cache")
DEFAULT_FILE = "provcat.yaml"
TENANT_NAME = re.compile(r"[a-z0-9]{1,20}")  # as the org in the example schemes


def check(cfg: object, where: str) -> dict:
    """Return cfg if it is a dict of known sections that hold dicts."""
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        raise ConfigError(f"{where}: the top level must be a mapping")
    for key, value in cfg.items():
        if key not in SECTIONS:
            raise ConfigError(f"{where}: unknown section {key!r}; known: {', '.join(SECTIONS)}")
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: section {key!r} must be a mapping")
    parts = [(f"tenants.{n}", p) for n, p in cfg.get("tenants", {}).items()]
    for name in cfg.get("tenants", {}):
        if not isinstance(name, str) or not TENANT_NAME.fullmatch(name):
            raise ConfigError(f"{where}: tenant name {name!r} must be 1 to 20 lowercase letters and digits")
    if "admin" in cfg:
        parts.append(("admin", cfg["admin"]))
    for label, part in parts:
        if not isinstance(part, dict):
            raise ConfigError(f"{where}: {label} must be a mapping")
        for key, value in part.items():
            if key not in TENANT_SECTIONS:
                raise ConfigError(f"{where}: {label}: unknown section {key!r}; "
                                  f"it can have {', '.join(TENANT_SECTIONS)}")
            if not isinstance(value, dict):
                raise ConfigError(f"{where}: {label}.{key} must be a mapping")
    return cfg


def _owner(uri: str, schemes: dict) -> str | None:
    """The tenant a logical URI belongs to, if its scheme names a tenant part."""
    from .errors import UriError
    from .uri import parse

    try:
        u = parse(uri)
    except UriError:
        return None
    spec = schemes.get(u.scheme)
    if not isinstance(spec, dict) or not spec.get("tenant"):
        return None
    parts = spec.get("parts") or []
    if spec["tenant"] not in parts:
        return None
    given = (u.authority, *u.segments)
    i = parts.index(spec["tenant"])
    return given[i] if i < len(given) else None


def _check_part(cfg: dict, name: str, part: dict, where: str) -> None:
    """A tenant names only its own stores, stages and entries."""
    for store in part.get("stores", {}):
        if not str(store).endswith("-" + name):
            raise ConfigError(f"{where}: tenants.{name}: store {store!r} must end with -{name}, "
                              f"as in lakefs-{name}, so it cannot pass for another tenant's")
    schemes = cfg.get("schemes", {})
    for key in part.get("stages", {}):
        owner = _owner(str(key), schemes)
        if owner != name:
            raise ConfigError(f"{where}: tenants.{name}: the stage {key!r} is not a URI of {name}")
    for key, raw in part.get("entries", {}).items():
        if _owner(str(key), schemes) != name:
            raise ConfigError(f"{where}: tenants.{name}: the entry {key!r} is not a URI of {name}")
        target = raw.get("target") if isinstance(raw, dict) else None
        if not isinstance(target, str) or any("${" in str(v) for v in raw.values()):
            # The check reads the text before ${...} is filled in, so a filled-in
            # value could name any store.
            raise ConfigError(f"{where}: tenants.{name}: the entry {key!r} must have a target "
                              "with no ${...} values")
        store, _, rest = target.partition("://")
        if not store.endswith("-" + name):
            raise ConfigError(f"{where}: tenants.{name}: the entry {key!r} must go to a store that ends "
                              f"with -{name}, not {store!r}")
        place = rest.split("/", 1)[0].split("?", 1)[0]
        if place != name and not place.startswith(name + "-"):
            # As a template gives lakefs-{org}://{org}-data-..., so access
            # lists that grant acme-* keep holding.
            raise ConfigError(f"{where}: tenants.{name}: the entry {key!r} must go to a bucket or "
                              f"repository named {name} or {name}-..., not {place!r}")


class View(dict):
    """A configuration cut down to one view. ``tenant`` says whose: None is
    the admin's."""

    def __init__(self, cfg: dict, tenant: str | None):
        super().__init__(cfg)
        self.tenant = tenant


def _name(section: str, key: str) -> str:
    """The form in which two names are the same: a stage key as the resolver
    reads it, so a trailing "/" or a percent-encoding is not a new name."""
    if section not in ("stages", "entries"):
        return key
    from .errors import UriError
    from .uri import parse

    try:
        return str(parse(key).without_selector())
    except UriError:
        return key  # the resolver refuses it later, with the reason


def view(cfg: dict, tenant: str | None = None, where: str = "config") -> View:
    """The configuration that a tenant sees, or with no tenant the admin's.
    The result has no tenants or admin section."""
    cfg = check(cfg, where)
    tenant = tenant or None
    tenants = cfg.get("tenants", {})
    if tenant is not None and tenant not in tenants:
        known = ", ".join(sorted(tenants)) or "none"
        raise ConfigError(f"{where}: no tenant {tenant!r}; tenants: {known}")
    out = {k: dict(v) for k, v in cfg.items() if k not in ("tenants", "admin")}
    if tenant is None:
        parts = [("admin", cfg.get("admin", {}))] + [(f"tenants.{n}", tenants[n]) for n in sorted(tenants)]
    else:
        parts = [(f"tenants.{tenant}", tenants[tenant])]
    for label, part in parts:
        if label != "admin":
            _check_part(cfg, label.split(".", 1)[1], part, where)
        for section in ("stores", "stages", "entries"):
            mine = out.setdefault(section, {})
            names = {_name(section, k) for k in mine}
            for key, value in part.get(section, {}).items():
                if _name(section, key) in names:
                    raise ConfigError(f"{where}: {label}.{section}.{key} is also named elsewhere")
                names.add(_name(section, key))
                mine[key] = value
        if "cache" in part and (label == "admin") == (tenant is None):
            out["cache"] = part["cache"]  # each view takes its own part's cache
    return View({k: v for k, v in out.items() if v or k in cfg}, tenant)


def _interpolate(cfg: dict, where: str) -> dict:
    from omegaconf import OmegaConf
    from omegaconf.errors import OmegaConfBaseException

    try:
        return OmegaConf.to_container(OmegaConf.create(cfg), resolve=True)
    except (OmegaConfBaseException, ValueError) as e:
        raise ConfigError(f"{where}: {e}") from e


class ConfigSource(ABC):
    location: str

    @abstractmethod
    def raw(self) -> dict:
        """Return the whole configuration as written."""

    def raw_for(self, tenant: str | None) -> dict:
        """The configuration as written, with at least the shared sections
        and the tenant's part. A source may leave out other tenants."""
        return self.raw()

    def load(self, tenant: str | None = None) -> View:
        """The configuration that the tenant sees, with ${...} values filled
        in. With no tenant, the admin's view of every tenant. The view is cut
        first, so a ${...} in another tenant's part is never read, and a
        tenant's part cannot reach into another part."""
        tenant = tenant or None
        cut = view(self.raw_for(tenant), tenant, self.location)
        return View(check(_interpolate(dict(cut), self.location), self.location), tenant)

    def save(self, cfg: dict) -> None:
        raise ConfigError(f"{self.location} is read-only")


class EmptyConfig(ConfigSource):
    location = "(built-in defaults)"

    def raw(self) -> dict:
        return {}


class YamlConfig(ConfigSource):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.location = str(self.path)

    def raw(self) -> dict:
        import yaml

        if not self.path.is_file():
            raise ConfigError(f"config file not found: {self.path}")
        try:
            cfg = yaml.safe_load(self.path.read_text())
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"{self.path}: {e}") from e
        return check(cfg, self.location)


class EtcdConfig(ConfigSource):
    """Configuration in etcd, addressed as ``etcd://host:port/prefix``."""

    def __init__(self, url: str, timeout: float = 5.0):
        if not url.startswith("etcd://"):
            raise ConfigError(f"etcd config must start with etcd://, got {url!r}")
        hostport, _, prefix = url[len("etcd://"):].partition("/")
        if not hostport:
            raise ConfigError(f"no host in {url!r}")
        if ":" not in hostport:
            hostport += ":2379"
        self.base = f"http://{hostport}"
        self.prefix = "/" + prefix.strip("/") if prefix.strip("/") else ""
        self.location = url
        self.timeout = timeout

    def _call(self, op: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/v3/kv/{op}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read() or b"{}")
        except (urllib.error.URLError, OSError) as e:
            raise ConfigError(f"cannot reach etcd at {self.base}: {e}") from e
        except ValueError as e:
            raise ConfigError(f"etcd at {self.base} sent a reply that is not JSON: {e}") from e
        if not isinstance(out, dict):
            raise ConfigError(f"etcd at {self.base} sent an unexpected reply")
        return out

    @staticmethod
    def _b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    def get(self, key: str) -> str | None:
        out = self._call("range", {"key": self._b64(key)})
        kvs = out.get("kvs") or []
        if not kvs:
            return None
        value = base64.b64decode(kvs[0].get("value", "")).decode()
        return value or None  # etcd leaves out an empty value; treat it as missing

    def _json(self, key: str):
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{self.location}: {key} is not JSON: {e}") from e

    def _tenant_names(self) -> list[str]:
        names = self._json(f"{self.prefix}/tenants")
        if names is None:
            return []
        if not isinstance(names, list) or not all(isinstance(n, str) and TENANT_NAME.fullmatch(n) for n in names):
            raise ConfigError(f"{self.location}: {self.prefix}/tenants must be a list of tenant names")
        return names

    def _tenant(self, name: str) -> dict | None:
        """A tenant's part, or None if there is no such tenant. The key
        <prefix>/tenants/<name>/name marks a tenant, even one with an empty part."""
        if self._json(f"{self.prefix}/tenants/{name}/name") != name:
            return None
        part = {}
        for section in TENANT_SECTIONS:
            value = self._json(f"{self.prefix}/tenants/{name}/{section}")
            if value is not None:
                part[section] = value
        return part

    def _admin(self) -> dict:
        part = {}
        for section in TENANT_SECTIONS:
            value = self._json(f"{self.prefix}/admin/{section}")
            if value is not None:
                part[section] = value
        return part

    def _shared(self) -> dict:
        cfg = {}
        for section in SECTIONS:
            if section in ("tenants", "admin"):
                continue
            value = self._json(f"{self.prefix}/{section}")
            if value is not None:
                cfg[section] = value
        return cfg

    def raw(self) -> dict:
        cfg = self._shared()
        names = self._tenant_names()
        if names:
            cfg["tenants"] = {n: self._tenant(n) or {} for n in names}
        admin = self._admin()
        if admin:
            cfg["admin"] = admin
        return check(cfg, self.location)

    def raw_for(self, tenant: str | None) -> dict:
        if tenant is None:
            return self.raw()
        # Only the shared keys and this tenant's, so that etcd's access control
        # can refuse a tenant the others' keys.
        if not TENANT_NAME.fullmatch(tenant):
            raise ConfigError(f"{self.location}: no tenant {tenant!r}")
        cfg = self._shared()
        part = self._tenant(tenant)
        cfg["tenants"] = {tenant: part} if part is not None else {}
        return check(cfg, self.location)

    def save(self, cfg: dict) -> None:
        cfg = check(cfg, "config to save")
        view(cfg, None, "config to save")  # every part must load again
        ops = []

        def put(key: str, value) -> None:
            ops.append({"request_put": {"key": self._b64(key), "value": self._b64(json.dumps(value, sort_keys=True))}})

        def drop(key: str) -> None:
            ops.append({"request_delete_range": {"key": self._b64(key)}})

        for section in SECTIONS:
            if section in ("tenants", "admin"):
                continue
            if section in cfg:
                put(f"{self.prefix}/{section}", cfg[section])
            else:
                drop(f"{self.prefix}/{section}")
        for section in TENANT_SECTIONS:
            if section in cfg.get("admin", {}):
                put(f"{self.prefix}/admin/{section}", cfg["admin"][section])
            else:
                drop(f"{self.prefix}/admin/{section}")
        tenants = cfg.get("tenants", {})
        for name in set(self._tenant_names()) - set(tenants):
            for section in (*TENANT_SECTIONS, "name"):
                drop(f"{self.prefix}/tenants/{name}/{section}")
        for name, part in tenants.items():
            put(f"{self.prefix}/tenants/{name}/name", name)
            for section in TENANT_SECTIONS:
                if section in part:
                    put(f"{self.prefix}/tenants/{name}/{section}", part[section])
                else:
                    drop(f"{self.prefix}/tenants/{name}/{section}")
        if tenants:
            put(f"{self.prefix}/tenants", sorted(tenants))
        else:
            drop(f"{self.prefix}/tenants")
        out = self._call("txn", {"success": ops})
        if out.get("succeeded") is False:
            raise ConfigError(f"etcd at {self.base} refused the update")


def open_config(spec: str | None = None) -> ConfigSource:
    """Pick a config source.

    The order is: the given spec, then PROVCAT_CONFIG, then ./provcat.yaml if it
    exists, then the built-in defaults. A spec that starts with etcd:// is etcd;
    anything else is a YAML path.
    """
    spec = spec or os.environ.get("PROVCAT_CONFIG")
    if not spec:
        return YamlConfig(DEFAULT_FILE) if Path(DEFAULT_FILE).is_file() else EmptyConfig()
    if spec.startswith("etcd://"):
        return EtcdConfig(spec)
    return YamlConfig(spec)
