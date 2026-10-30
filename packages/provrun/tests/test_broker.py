"""The stub broker: allow list, org, environment and mode."""
import pytest

from provrun.access import AccessDenied, StubBroker
from provrun.context import RunContext
from provrun.evidence import read_events
from provrun.resolve import ProvcatResolver


def ctx(tmp_path, catalog, env="dev", allow=()):
    broker = StubBroker(list(allow), physical_schemes=catalog.stores.keys())
    return RunContext.open(org="acme", domain="benefits", env=env, actor="tester", broker=broker,
                           resolver=ProvcatResolver(catalog), catalog=catalog, runs_root=tmp_path / "runs")


def test_grant_writes_request_and_grant(tmp_path, catalog, docs):
    uri = f"file://{docs}/report.txt"
    c = ctx(tmp_path, catalog, allow=[uri])
    g = c.broker.request(c, uri, "mounted_snapshot")
    assert g.granted and g.expires_at
    types = [e.type for e in read_events(c.folder / "evidence")]
    assert types == ["run_created", "access_requested", "access_granted"]


def test_a_uri_not_allowed_is_denied_and_recorded(tmp_path, catalog, docs):
    c = ctx(tmp_path, catalog, allow=[f"file://{docs}/other.txt"])
    with pytest.raises(AccessDenied, match="allow list"):
        c.broker.request(c, f"file://{docs}/report.txt", "mounted_snapshot")
    assert read_events(c.folder / "evidence")[-1].type == "access_denied"


def test_a_folder_entry_allows_what_is_under_it(tmp_path, catalog, docs):
    c = ctx(tmp_path, catalog, allow=[f"file://{docs}/"])
    assert c.broker.request(c, f"file://{docs}/notes/a.md", "mounted_snapshot").granted
    with pytest.raises(AccessDenied):
        c.broker.request(c, f"file://{docs}x/report.txt", "mounted_snapshot")


def test_a_selector_must_be_allowed_exactly(tmp_path, catalog):
    plain = "data+table://acme/benefits/dev/plans/docs"
    c = ctx(tmp_path, catalog, env="dev", allow=[plain, plain + "?at=commit:abc"])
    assert c.broker.decide(c, plain + "?at=commit:abc", "mounted_snapshot", "").granted
    assert not c.broker.decide(c, plain + "?at=commit:def", "mounted_snapshot", "").granted


def test_a_selector_cannot_take_a_dev_run_to_the_prod_branch(tmp_path, catalog):
    # Finding 1 of the review: the env part says dev, and ?at=branch:prod picks the prod branch.
    plain = "data+table://acme/benefits/dev/plans/docs"
    c = ctx(tmp_path, catalog, env="dev", allow=[plain])
    g = c.broker.decide(c, plain + "?at=branch:prod", "mounted_snapshot", "")
    assert not g.granted and "selector" in g.reason


def test_physical_uris_only_in_dev(tmp_path, catalog, docs):
    uri = f"file://{docs}/report.txt"
    c = ctx(tmp_path, catalog, env="prod", allow=[uri])
    with pytest.raises(AccessDenied, match="only in dev"):
        c.broker.request(c, uri, "mounted_snapshot")


def test_logical_uri_of_another_env_or_org_is_denied(tmp_path, catalog):
    allow = ["data+table://acme/benefits/dev/plans/docs", "data+table://globex/benefits/prod/plans/docs"]
    c = ctx(tmp_path, catalog, env="prod", allow=allow)
    assert "environment dev" in c.broker.decide(c, allow[0], "mounted_snapshot", "").reason
    assert "org globex" in c.broker.decide(c, allow[1], "mounted_snapshot", "").reason


def test_only_snapshot_mode(tmp_path, catalog, docs):
    uri = f"file://{docs}/report.txt"
    c = ctx(tmp_path, catalog, allow=[uri])
    with pytest.raises(AccessDenied, match="mounted_snapshot"):
        c.broker.request(c, uri, "mounted_readwrite")
    with pytest.raises(ValueError):
        c.broker.request(c, uri, "telepathy")
