
import os, json, pytest

YAML_CFG = os.path.join(os.path.dirname(__file__), "..", "config", "resolver.sample.yaml")
ETCD_ENDPOINT = os.getenv("ETCD_ENDPOINT", "localhost:2379")
ETCD_PREFIX = "/provcat_test"

def have_etcd():
    try:
        import etcd3  # noqa
        return True
    except Exception:
        return False

@pytest.mark.skipif(not have_etcd(), reason="python-etcd3 not installed")
def test_yaml_vs_etcd_equivalence():
    import yaml, etcd3
    from provcat.impl_yaml import OmegaConfResolver
    from provcat.impl_etcd import EtcdResolver

    # Load YAML
    y = yaml.safe_load(open(YAML_CFG)) or {}
    repos, paths, routing = y["repos"], y["paths"], y["routing"]

    # Bootstrap etcd
    host, port = ETCD_ENDPOINT.split(":")
    port = int(port)
    cli = etcd3.client(host=host, port=port)
    cli.put(f"{ETCD_PREFIX}/repos", json.dumps(repos))
    cli.put(f"{ETCD_PREFIX}/paths", json.dumps(paths))
    cli.put(f"{ETCD_PREFIX}/routing", json.dumps(routing))

    # Resolvers
    yres = OmegaConfResolver(YAML_CFG)
    eres = EtcdResolver(f"etcd://{ETCD_ENDPOINT}{ETCD_PREFIX}")

    # Compare resolutions
    logical_ids = [
        "data+table://acme/shared/prod/agreements/qa_outputs",
        "artifact+prompt://acme/teams/prod/proj-credit-agreements/qa/default_v1",
        "experiment://acme/teams/prod/proj-credit-agreements/qa",
        "model://acme/teams/prod/proj-credit-agreements/my_model"
    ]
    for lid in logical_ids:
        r1 = yres.resolve(lid, context={}).lakefs
        r2 = eres.resolve(lid, context={}).lakefs
        assert r1["repo"] == r2["repo"]
        assert r1["branch"] == r2["branch"]
        assert set(r1["paths"].keys()) == set(r2["paths"].keys())
