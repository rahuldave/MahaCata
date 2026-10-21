
"""Bootstrap etcd with resolver config.

Usage:
  ETCD_ENDPOINT=localhost:2379 python examples/etcd_bootstrap.py

This writes the same structure as config/resolver.sample.yaml into:
  etcd://HOST:PORT/provcat

Keys:
  /provcat/repos
  /provcat/paths
  /provcat/routing
"""
import os, json
import etcd3  # pip install etcd3

HOST, PORT = (os.getenv("ETCD_ENDPOINT") or "localhost:2379").split(":")
PORT = int(PORT)
PREFIX = "/provcat"

client = etcd3.client(host=HOST, port=PORT)

cfg = {
  "repos": {
    "data": {"template": "agreements-qa_outputs-data"},
    "experiments": {"template": "exp-proj-{project}"},
    "models": {"template": "model-{model_name}"},
    "artifacts": {"template": "proj-{project}-artifacts"}
  },
  "paths": {
    "experiment": {"base": "{org}/{team}/{env}/mlflow/experiments/{project}/{pipeline}"},
    "run":        {"base": "{project}/{pipeline}/{run_id}"},
    "model":      {"base": "{org}/{team}/{env}/{project}/versions/v{version}"},
    "data_table": {"base": "{org}/{domain}/{env}/{family}/{name}"},
    "artifact":   {"base": "{org}/{team}/{env}/{project}/artifacts/{type}/{name}"}
  },
  "routing": {
    "data+table": {"repo_kind": "data", "path_kind": "data_table"},
    "model":      {"repo_kind": "models", "path_kind": "model"},
    "experiment": {"repo_kind": "experiments", "path_kind": "experiment"},
    "artifact+prompt": {"repo_kind": "artifacts", "path_kind": "artifact"},
    "artifact+card":   {"repo_kind": "artifacts", "path_kind": "artifact"},
    "artifact+dataset-card": {"repo_kind": "artifacts", "path_kind": "artifact"}
  }
}

client.put(f"{PREFIX}/repos", json.dumps(cfg["repos"]))
client.put(f"{PREFIX}/paths", json.dumps(cfg["paths"]))
client.put(f"{PREFIX}/routing", json.dumps(cfg["routing"]))

print("Wrote etcd config under prefix:", PREFIX)
print("Try CLI with:  python -m provcat.provc --cfg etcd://%s:%d%s resolve data+table://acme/shared/prod/agreements/qa_outputs" % (HOST, PORT, PREFIX))
