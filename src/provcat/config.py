"""Configuration sources.

A configuration is a dict with up to four sections:

- ``stores``: named store instances, each ``{driver: ..., <options>}``.
- ``schemes``: logical URI schemes and their templates (see resolver.py).
- ``stages``: stage pointers, ``{logical-uri: {stage-name: "kind:value"}}``.
- ``cache``: ``{root: <folder>}``.

ConfigSource is the interface. ``load()`` returns the configuration with every
``${oc.env:VAR}`` filled in from the environment of the reader. ``raw()`` returns
it as written, with the ``${...}`` left in. ``config push`` copies the raw form,
so each reader of a shared etcd fills in its own environment.

YamlConfig reads a YAML file through OmegaConf. EtcdConfig keeps each section as
JSON under ``<prefix>/<section>`` and talks to the etcd v3 HTTP gateway, so it
needs no etcd client library. ``save()`` replaces all sections in one etcd
transaction, and deletes the sections that the new configuration does not have.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

from .errors import ConfigError

SECTIONS = ("stores", "schemes", "stages", "cache")
DEFAULT_FILE = "provcat.yaml"


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
    return cfg


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

    def load(self) -> dict:
        """Return the whole configuration with ${...} values filled in."""
        return check(_interpolate(self.raw(), self.location), self.location)

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

    def raw(self) -> dict:
        cfg = {}
        for section in SECTIONS:
            raw = self.get(f"{self.prefix}/{section}")
            if raw is None:
                continue
            try:
                cfg[section] = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{self.location}/{section} is not JSON: {e}") from e
        return check(cfg, self.location)

    def save(self, cfg: dict) -> None:
        cfg = check(cfg, "config to save")
        ops = []
        for section in SECTIONS:
            key = self._b64(f"{self.prefix}/{section}")
            if section in cfg:
                value = self._b64(json.dumps(cfg[section], sort_keys=True))
                ops.append({"request_put": {"key": key, "value": value}})
            else:
                ops.append({"request_delete_range": {"key": key}})
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
