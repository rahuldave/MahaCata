
from provenance_resolver.impl_yaml import OmegaConfResolver
from provenance_resolver.impl_etcd import EtcdResolver
from provenance_resolver.interface import split_logical_id, parse_selector

SAMPLE = "config/sample.yaml"

def test_parse_selector():
    from provenance_resolver.interface import parse_selector
    s = parse_selector("at=branch:feat/x")
    assert s.kind == "branch" and s.value == "feat/x"

def test_split_logical_id():
    scheme, parts, sel = split_logical_id("data+table://acme/bigbank/dev/proj/qa_outputs?at=snapshot:22")
    assert scheme == "data+table"
    assert parts[:4] == ["acme","bigbank","dev","proj"]
    assert sel.kind == "snapshot" and sel.value == "22"

def test_yaml_resolve_table():
    r = OmegaConfResolver(SAMPLE)
    res = r.resolve("data+table://acme/bigbank/dev/credit-agreements/qa_outputs?at=branch:feat/x")
    assert res.lakefs["repo"] == "agreements-bigbank-dev"
    assert res.lakefs["branch"] == "feat/x"
    assert res.lakefs["paths"]["dataset"].endswith("/qa_outputs.lance")

def test_yaml_resolve_artifact():
    r = OmegaConfResolver(SAMPLE)
    res = r.resolve("artifact+prompt://acme/bigbank/dev/credit-agreements/qa/abcd1234/prompt_v17.txt?at=branch:feat/y")
    assert "mlflow" in res.__dict__
    assert res.lakefs["paths"]["artifact"].endswith("/prompt_v17.txt")

def test_etcd_construct():
    r = EtcdResolver(SAMPLE)
    assert r.prefix.startswith("/")
