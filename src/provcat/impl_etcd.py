
"""EtcdResolver

Use etcd to fetch `repos`, `paths`, and `routing` config for the resolver.

- Pass `--cfg etcd://HOST:PORT/prefix` to the CLI to enable etcd-backed resolution.
- Expected keys under the prefix:
    {prefix}/repos         -> JSON dict
    {prefix}/paths         -> JSON dict
    {prefix}/routing       -> JSON dict

We also support a hybrid mode:
- If a local YAML path is provided in env `PROVCAT_FALLBACK_CFG`, we will
  merge missing sections from that YAML (repos/paths/routing), favoring etcd.

Requires: `pip install etcd3`
"""
import os, json
from typing import Any, Dict, Tuple
from .interface import Resolution, split_logical_id, Selector, ResolveError

try:
    import etcd3  # type: ignore
except Exception as e:
    etcd3 = None

def _parse_etcd_url(url: str) -> Tuple[str, int, str]:
    # etcd://host:port/prefix
    assert url.startswith("etcd://"), "cfg must start with etcd://"
    without = url[len("etcd://"):]
    host_port, _, prefix = without.partition("/")
    host, _, port = host_port.partition(":")
    port = int(port) if port else 2379
    prefix = prefix.rstrip("/")
    return host, port, "/" + prefix if prefix else ""

def _etcd_json(cli, key: str):
    val, meta = cli.get(key)
    if not val:
        return {}
    try:
        return json.loads(val.decode("utf-8"))
    except Exception:
        return {}

class EtcdResolver:
    def __init__(self, cfg_url: str):
        if etcd3 is None:
            raise ResolveError("python-etcd3 is not installed. pip install etcd3")
        self.host, self.port, self.prefix = _parse_etcd_url(cfg_url)
        self.cli = etcd3.client(host=self.host, port=self.port)
        # Optional fallback
        self.fallback = os.getenv("PROVCAT_FALLBACK_CFG")

    def _load_cfg(self) -> Dict[str, Any]:
        repos = _etcd_json(self.cli, f"{self.prefix}/repos")
        paths = _etcd_json(self.cli, f"{self.prefix}/paths")
        routing = _etcd_json(self.cli, f"{self.prefix}/routing")
        cfg = {"repos": repos, "paths": paths, "routing": routing}
        # fallback merge if requested
        if self.fallback and os.path.exists(self.fallback):
            try:
                import yaml
                with open(self.fallback, "r") as f:
                    y = yaml.safe_load(f) or {}
                for k in ("repos","paths","routing"):
                    if not cfg.get(k) and y.get(k):
                        cfg[k] = y[k]
            except Exception:
                pass
        return cfg

    def resolve(self, logical_id: str, context: Dict[str, Any]) -> Resolution:
        cfg = self._load_cfg()
        from .impl_yaml import OmegaConfResolver
        # Reuse YAML resolver machinery by instantiating with a dict
        y = OmegaConfResolver.__new__(OmegaConfResolver)
        y.cfg_path = "etcd://"
        y.cfg = cfg
        return y.resolve(logical_id, context)


def put_section(self, section: str, value: dict):
    if section not in ("repos","paths","routing"):
        raise ResolveError(f"unknown section: {section}")
    key = f"{self.prefix}/{section}"
    self.cli.put(key, json.dumps(value))

def get_section(self, section: str) -> dict:
    if section not in ("repos","paths","routing"):
        raise ResolveError(f"unknown section: {section}")
    key = f"{self.prefix}/{section}"
    val, meta = self.cli.get(key)
    if not val:
        return {}
    try:
        return json.loads(val.decode("utf-8"))
    except Exception:
        return {}
