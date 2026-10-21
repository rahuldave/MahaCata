
# Tests

## Running
```
pip install pytest etcd3 pyyaml omegaconf boto3
ETCD_ENDPOINT=localhost:2379 pytest -q
```

- If `python-etcd3` or etcd is not available, etcd tests are **skipped** automatically.

## What is tested
- **YAML vs etcd resolution equivalence**: given the same `repos/paths/routing`,
  resolving several URIs yields the same lakeFS repo/branch/path keys.
