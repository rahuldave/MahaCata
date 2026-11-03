"""The telemetry policy: reading, checking and narrowing it, and what each
destination receives."""
from __future__ import annotations

import json

import re

import pytest

from provevi import EvidenceEvent, FileStore, ProveviError, sha256_bytes
from provevi.cli import main
from provevi.telemetry import (BLOCKED, PROBLEM, TelemetryPolicy, attributes_for, blocked_data, level_of,
                               normal_endpoint, otlp_traces_target, split_body)

POLICY = {
    "version": 1,
    "libraries": {
        "pydantic_ai": {"versions": ">=2.64,<2.65", "telemetry": ["logfire"]},
        "litellm": {"versions": "==1.83.0"},
    },
    "destinations": {
        "logfire": {"kind": "logfire", "receives": "usage", "endpoint": "https://logfire-us.pydantic.dev"},
        "collector": {"kind": "otlp", "receives": "bodies", "endpoint": "https://otel.example.com/v1/traces"},
    },
    "hosts": ["api.anthropic.com", ".amazonaws.com", "logfire-us.pydantic.dev", "otel.example.com"],
}


def policy() -> TelemetryPolicy:
    return TelemetryPolicy.from_dict(json.loads(json.dumps(POLICY)))


def test_policy_reads_and_names_libraries_canonically():
    p = policy()
    assert set(p.libraries) == {"pydantic-ai", "litellm"}
    assert p.library("Pydantic_AI").telemetry == ("logfire",)
    assert [d.name for d in p.destinations_for("pydantic-ai")] == ["logfire"]
    assert p.destinations_for("litellm") == [] and p.destinations_for("openai") == []


def test_policy_round_trips_and_its_digest_is_stable():
    p = policy()
    again = TelemetryPolicy.from_dict(p.to_dict())
    assert again.to_dict() == p.to_dict() and again.digest() == p.digest()
    assert TelemetryPolicy.deny_all().digest() != p.digest()


def test_policy_reports_every_problem_at_once():
    bad = {
        "version": 2, "extra": 1,
        "libraries": {"x": {"versions": "not a spec"}, "y": {"versions": ">=1", "telemetry": ["nowhere"]},
                      "z": "no"},
        "destinations": {"a": {"kind": "kafka", "receives": "all"}, "b": {"kind": "otlp", "receives": "names"}},
        "hosts": ["http://x.com", "a.com:443", "*.b.com", ".", "..com", ".com", "a\tb.com", "a_b.com"],
    }
    with pytest.raises(ProveviError) as e:
        TelemetryPolicy.from_dict(bad, source="p.yaml")
    msg = str(e.value)
    for part in ("p.yaml", "version is 2", "unknown key 'extra'", "'x' needs versions", "'nowhere', which is not",
                 "'z' is a mapping", "kind 'kafka'", "receives 'all'", "needs an http or https endpoint",
                 "'http://x.com' is not a host", "'a.com:443'", "'*.b.com'", "'.' is not", "'..com'", "'.com'",
                 "'a\\tb.com'", "'a_b.com'"):
        assert part in msg


@pytest.mark.parametrize("doc", [None, [], "x", {"version": 1, "libraries": []}, {"version": 1, "hosts": "a.com"}])
def test_policy_refuses_documents_of_the_wrong_shape(doc):
    with pytest.raises(ProveviError):
        TelemetryPolicy.from_dict(doc)


def test_a_library_named_twice_is_refused():
    with pytest.raises(ProveviError, match="named twice"):
        TelemetryPolicy.from_dict({"version": 1, "libraries": {"pydantic-ai": {"versions": ">=1"},
                                                               "pydantic_ai": {"versions": ">=1"}}})


def test_load_reads_yaml_and_reports_a_bad_file(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text("version: 1\nhosts: [api.anthropic.com]\n")
    assert TelemetryPolicy.load(f).hosts == ("api.anthropic.com",)
    f.write_text("version: [1\n")
    with pytest.raises(ProveviError, match="cannot read"):
        TelemetryPolicy.load(f)
    with pytest.raises(ProveviError, match="cannot read"):
        TelemetryPolicy.load(tmp_path / "missing.yaml")


def test_hosts_match_exactly_or_under_a_dotted_domain():
    p = policy()
    assert p.allows_host("api.anthropic.com") and p.allows_host("API.Anthropic.com.")
    assert p.allows_host("s3.us-east-1.amazonaws.com")
    assert not p.allows_host("amazonaws.com")  # the dot means hosts under it, not the domain
    assert not p.allows_host("evilamazonaws.com")
    assert not p.allows_host("anthropic.com") and not p.allows_host("api.anthropic.com.evil.org")
    assert not TelemetryPolicy.deny_all().allows_host("localhost")


def test_check_installed_reports_an_untested_version():
    p = policy()
    assert p.check_installed({"pydantic-ai": "2.64.3", "litellm": "1.83.0"}) == []
    problems = p.check_installed({"Pydantic_AI": "2.65.0", "litellm": "garbage"})
    assert any("pydantic-ai 2.65.0" in x for x in problems) and any("litellm garbage" in x for x in problems)
    assert p.check_installed({}) == []  # a library that is not installed is no problem


def test_check_installed_reads_this_python_by_default():
    p = TelemetryPolicy.from_dict({"version": 1, "libraries": {"pyyaml": {"versions": "<1"}}})
    assert len(p.check_installed()) == 1


def test_narrowing_keeps_what_the_section_leaves_out():
    p = policy()
    assert p.narrow(None) is p
    n = p.narrow({})
    assert n.to_dict() == p.to_dict() and "narrowed" in n.source


def test_narrowing_can_remove_and_lower():
    n = policy().narrow({"destinations": {"logfire": {"receives": "names"}},
                         "libraries": {"pydantic-ai": {}},
                         "hosts": ["api.anthropic.com", "s3.amazonaws.com", "logfire-us.pydantic.dev"]})
    assert n.destinations["logfire"].receives == "names" and "collector" not in n.destinations
    assert set(n.libraries) == {"pydantic-ai"} and n.library("pydantic-ai").telemetry == ("logfire",)
    assert n.allows_host("s3.amazonaws.com") and not n.allows_host("ec2.amazonaws.com")


def test_removing_a_destination_removes_it_from_each_library():
    n = policy().narrow({"destinations": {"collector": None}})
    assert n.destinations_for("pydantic-ai") == []


@pytest.mark.parametrize("section, words", [
    ({"destinations": {"logfire": {"receives": "bodies"}}}, "more than the policy's usage"),
    ({"destinations": {"new": {}}}, "'new' is not in the policy"),
    ({"destinations": {"logfire": {"kind": "otlp"}}}, "unknown key 'kind'"),
    ({"destinations": {"logfire": {"endpoint": "https://elsewhere"}}}, "unknown key 'endpoint'"),
    ({"libraries": {"openai": {}}}, "'openai' is not in the policy"),
    ({"libraries": {"litellm": {"telemetry": ["logfire"]}}}, "may not feed 'logfire'"),
    ({"libraries": {"litellm": {"versions": ">=0"}}}, "unknown key 'versions'"),
    ({"hosts": ["example.org"]}, "'example.org' is not allowed"),
    ({"hosts": [".com"]}, "'.com' is not a host name"),
    ({"hosts": ["amazonaws.com"]}, "'amazonaws.com' is not allowed"),
    ({"version": 1}, "unknown key 'version'"),
    ("all", "is a mapping"),
])
def test_narrowing_never_widens(section, words):
    with pytest.raises(ProveviError, match=words):
        policy().narrow(section)


def test_a_dotted_host_narrows_to_a_deeper_domain_only():
    n = policy().narrow({"hosts": [".s3.amazonaws.com"]})
    assert n.allows_host("b.s3.amazonaws.com") and not n.allows_host("ec2.amazonaws.com")


def test_levels_classify_attributes_and_unknown_keys_are_bodies():
    assert level_of("gen_ai.request.model") == "names"
    assert level_of("gen_ai.usage.input_tokens") == "usage" and level_of("mlflow.llm.cost") == "usage"
    for key in ("gen_ai.input.messages", "gen_ai.tool.call.arguments", "mlflow.spanInputs", "some.new.key"):
        assert level_of(key) == "bodies"


def test_a_destination_receives_only_its_level():
    attrs = {"gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 3, "gen_ai.input.messages": "secret"}
    assert attributes_for(attrs, "names") == {"gen_ai.request.model": "m"}
    assert attributes_for(attrs, "usage") == {"gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 3}
    assert attributes_for(attrs, "bodies") == attrs
    with pytest.raises(ProveviError, match="not a level"):
        attributes_for(attrs, "everything")


def test_split_body_moves_bodies_out():
    kept, body = split_body({"gen_ai.request.model": "m", "gen_ai.usage.output_tokens": 1,
                             "gen_ai.output.messages": "secret"})
    assert kept == {"gen_ai.request.model": "m", "gen_ai.usage.output_tokens": 1}
    assert body == {"gen_ai.output.messages": "secret"}


def test_blocked_data_names_the_path_and_the_library():
    assert blocked_data("litellm.callbacks", library="LiteLLM", detail="mlflow") == {
        "path": "litellm.callbacks", "detail": "mlflow", "library": "litellm"}
    assert blocked_data("network") == {"path": "network", "detail": ""}
    with pytest.raises(ProveviError):
        blocked_data("")


def _run_with_events(root, run_id, rows):
    folder = root / run_id
    w = FileStore(root).open_run(run_id, folder / "evidence")
    prev = None
    for seq, (type_, data) in enumerate(rows):
        e = EvidenceEvent(run_id, seq, "2026-11-01T00:00:00Z", type_, "c", "a", (), data, None, "observed", prev)
        line = e.line()
        w.append(line)
        prev = sha256_bytes(line.encode())
    w.close()
    return folder


def test_prove_telemetry_lists_the_blocked_attempts(tmp_path, capsys):
    run_id = "run-20261101T000000Z-0000aaaa"
    _run_with_events(tmp_path, run_id, [
        ("run_created", {}),
        (BLOCKED, blocked_data("litellm.callbacks", library="litellm", detail="removed mlflow")),
        ("note_added", {}),
        (BLOCKED, blocked_data("network", detail="example.org")),
    ])
    assert main(["--runs", str(tmp_path), "telemetry", run_id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert "litellm.callbacks litellm  removed mlflow" in out[0] and out[1].endswith("network   example.org")


def test_prove_policy_prints_the_policy_and_fails_on_an_untested_version(tmp_path, capsys):
    f = tmp_path / "p.yaml"
    f.write_text(json.dumps(POLICY))
    code = main(["policy", str(f)])
    out = capsys.readouterr()
    assert "sha256 " in out.out and "destination collector (otlp) receives bodies at https://" in out.out
    assert code == (1 if out.err else 0)
    f.write_text(json.dumps({"version": 1, "libraries": {"pyyaml": {"versions": "<1"}}}))
    assert main(["policy", str(f)]) == 1
    assert "pyyaml" in capsys.readouterr().err
    f.write_text("version: 3\n")
    assert main(["policy", str(f)]) == 1
    assert "version is 3" in capsys.readouterr().err


# Each test below is one finding of the adversarial review.

def test_a_names_or_usage_key_with_text_in_it_counts_as_a_body():
    attrs = {"gen_ai.request.model": "SECRET PROMPT TEXT", "error.type": "ValueError: SECRET",
             "gen_ai.agent.name": {"msg": "SECRET"}, "gen_ai.usage.notes": "SECRET",
             "mlflow.chat.tokenUsage": '{"input_tokens": 3, "note": "SECRET"}',
             "gen_ai.response.finish_reasons": ["stop"], "gen_ai.usage.input_tokens": 3,
             "mlflow.llm.cost": '{"total_cost": 0.5}', "server.port": 443}
    usage = attributes_for(attrs, "usage")
    assert usage == {"gen_ai.response.finish_reasons": ["stop"], "gen_ai.usage.input_tokens": 3,
                     "mlflow.llm.cost": '{"total_cost": 0.5}', "server.port": 443}
    kept, body = split_body(attrs)
    assert kept == usage and set(body) == set(attrs) - set(usage)
    assert level_of("gen_ai.usage.input_tokens", True) == "bodies"
    assert level_of("gen_ai.usage.input_tokens", '"3"') == "bodies"


def test_a_key_that_is_not_a_string_is_a_body():
    assert attributes_for({1: "x"}, "bodies") == {1: "x"}
    assert attributes_for({1: "x"}, "usage") == {}
    assert split_body({1: "x"}) == ({}, {1: "x"})


def test_a_key_named_twice_in_the_file_is_refused(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("version: 1\nhosts: [h.example.com]\ndestinations:\n"
                 "  lf: {kind: otlp, receives: names, endpoint: 'https://h.example.com'}\n"
                 "  lf: {kind: otlp, receives: bodies, endpoint: 'https://h.example.com'}\n")
    with pytest.raises(ProveviError, match="'lf' is named twice"):
        TelemetryPolicy.load(f)


@pytest.mark.parametrize("version", [True, "1", 1.0, "on", None])
def test_the_version_must_be_the_integer_1(version):
    with pytest.raises(ProveviError, match="version is"):
        TelemetryPolicy.from_dict({"version": version})


def test_yaml_on_is_not_version_1(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("version: on\n")
    with pytest.raises(ProveviError, match="version is True"):
        TelemetryPolicy.load(f)


def test_a_local_build_needs_a_specifier_that_names_it():
    p = TelemetryPolicy.from_dict({"version": 1, "libraries": {"x": {"versions": "==2.64.0"}},})
    assert p.check_installed({"x": "2.64.0+patched"}) == ["x 2.64.0+patched is installed, but the policy approves ==2.64.0"]
    q = TelemetryPolicy.from_dict({"version": 1, "libraries": {"x": {"versions": "==2.64.0+patched"}}})
    assert q.check_installed({"x": "2.64.0+patched"}) == []


def test_a_library_named_twice_in_a_run_file_is_refused():
    with pytest.raises(ProveviError, match="named twice"):
        policy().narrow({"libraries": {"pydantic-ai": {"telemetry": []}, "Pydantic_AI": {}}})


@pytest.mark.parametrize("endpoint, words", [
    ("https://collector.evil.example/v1", "'collector.evil.example', which is not in hosts"),
    ("ftp://otel.example.com", "needs an http or https endpoint"),
    (None, "needs an http or https endpoint"),
    (3, "needs an http or https endpoint"),
])
def test_an_endpoint_must_be_http_and_in_hosts(endpoint, words):
    doc = {"version": 1, "hosts": ["otel.example.com"],
           "destinations": {"c": {"kind": "logfire", "receives": "names", "endpoint": endpoint}}}
    with pytest.raises(ProveviError, match=words):
        TelemetryPolicy.from_dict(doc)


def test_an_mlflow_destination_may_have_no_endpoint():
    p = TelemetryPolicy.from_dict({"version": 1, "destinations": {"m": {"kind": "mlflow", "receives": "usage"}}})
    assert p.destinations["m"].endpoint is None


def test_prove_telemetry_says_when_nothing_was_blocked(tmp_path, capsys):
    run_id = "run-20261101T000000Z-0000bbbb"
    _run_with_events(tmp_path, run_id, [("run_created", {})])
    assert main(["--runs", str(tmp_path), "telemetry", run_id]) == 0
    assert capsys.readouterr().out == f"{run_id}: no blocked attempts and no problems\n"


def test_prove_telemetry_lists_problems_with_the_blocked_attempts(tmp_path, capsys):
    run_id = "run-20261101T000000Z-0000cccc"
    _run_with_events(tmp_path, run_id, [
        ("run_created", {}),
        (PROBLEM, {"problem": "logfire is configured"}),
        (BLOCKED, blocked_data("destination", detail="https://other.example")),
    ])
    assert main(["--runs", str(tmp_path), "telemetry", run_id]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("problem logfire is configured")
    assert "blocked destination" in out[1] and out[1].endswith("https://other.example")


@pytest.mark.parametrize("a, b", [
    ("https://OTEL.example.com/v1/traces/", "https://otel.example.com:443/v1/traces"),
    ("http://otel.example.com.", "http://otel.example.com:80"),
    ("postgresql+psycopg://u:p@db.example.com/mlflow", "postgresql://db.example.com:5432/mlflow"),
    ("https://xn--bcher-kva.example/x", "https://XN--BCHER-KVA.example/x/"),
    ("mssql+pyodbc://db.example.com:1433/m", "mssql://db.example.com:1433/m"),
    ("http://[::1]:4318/v1/traces", "http://[::1]:4318/v1/traces/"),
])
def test_two_ways_of_writing_one_endpoint_are_equal(a, b):
    assert normal_endpoint(a) == normal_endpoint(b)


def test_an_ipv6_host_keeps_its_brackets():
    assert normal_endpoint("http://[::1]/x") == "http://[::1]:80/x"


@pytest.mark.parametrize("a, b", [
    ("https://otel.example.com/v1/traces", "http://otel.example.com/v1/traces"),
    ("https://otel.example.com:4318/v1/traces", "https://otel.example.com/v1/traces"),
    ("https://otel.example.com/v1/traces", "https://otel.example.com/v1"),
    ("https://otel.example.com", "https://otel.example.com.evil.example"),
])
def test_different_endpoints_stay_different(a, b):
    assert normal_endpoint(a) != normal_endpoint(b)


@pytest.mark.parametrize("url, words", [
    ("ftp://x.example", "not an http"),
    ("databricks", "not an http"),
    ("file:///tmp/x", "not an http"),
    ("https://", "not an http"),
    ("https://x.example:99999", "not a URL"),
    ("sqlite://host/x.db", "absolute path"),
    ("sqlite:///relative.db", "absolute path"),
    # Two parsers could read these as two places.
    ("https://evil.example\\@otel.example.com/v1/traces", "backslash"),
    ("https://otel.example.com\\.evil.example/v1", "backslash"),
    ("https://otel.example.com /v1", "backslash, space"),
    ("https://otel.example.com/v1\n", "backslash, space"),
    ("https://evil.example@otel.example.com/v1/traces", "no user or password"),
    ("https://u:p@otel.example.com", "no user or password"),
    ("postgresql://db.example.com/mlflow?host=evil.example", "no query"),
    ("mysql://db.example.com/m?unix_socket=/tmp/x", "no query"),
    ("postgresql://db.example.com,evil.example/mlflow", "not a host name"),
    ("https://h%6Fst.example/x", "not a host name"),
    ("https://otel.example.com:0/v1", "a port from 1"),
    ("https://otel.example.com:/v1", "a port from 1"),
    ("https://otel.example.com/a/../v1", "no . or .. segments"),
    ("https://otel.example.com//v1", "no //"),
    ("https://otel.example.com/%2e%2e/v1", "no %"),
    # Review findings: SQLAlchemy reads no fragment, so it connects to evil.com.
    ("postgresql://good.example.com:5432/mlflow#@evil.com/mlflow", "control character or #"),
    ("postgresql://good.example.com#@evil.com:5432/mlflow", "control character or #"),
    ("https://otel.example.com/v1/traces#f", "control character or #"),
    ("sqlite:////srv/mlflow.db#x", "control character or #"),
    # IDNA 2003 gives fass.de, IDNA 2008 gives xn--fa-hia.de.
    ("https://faß.de/v1/traces", "in ASCII"),
    ("https://bücher.example/x", "in ASCII"),
    ("https://otel.example.com/otlp?k=1", "no query"),
    ("https://otel.example.com/otlp?", "no query"),
    ("mssql+pyodbc://gooddsn", "a port and a database"),
    ("mssql://db.example.com:1433", "a port and a database"),
    ("https+x://otel.example.com/v1/traces", "not an http"),
])
def test_normal_endpoint_refuses_what_is_not_one_clear_place(url, words):
    with pytest.raises(ProveviError, match=words):
        normal_endpoint(url)


@pytest.mark.parametrize("url", [
    "postgresql://db.example.com/mlflow?sslmode=require",
    "postgresql://u:pw@db.example.com/mlflow?sslmode=verify-full&connect_timeout=10&application_name=provrun",
    "mysql://db.example.com/mlflow?charset=utf8mb4",
])
def test_a_database_url_may_carry_safe_settings(url):
    base = url.split("?")[0].replace("u:pw@", "")
    sslmode = re.search(r"sslmode=[a-z-]+", url)
    assert normal_endpoint(url) == normal_endpoint(base) + (f"?{sslmode[0]}" if sslmode else "")


def test_a_policy_can_ask_for_tls():
    # Review finding: sslmode was dropped, so a URL with sslmode=disable
    # matched a policy that names sslmode=require.
    doc = {"version": 1, "hosts": ["db.example.com"], "destinations": {"pg": {
        "kind": "mlflow", "receives": "names", "endpoint": "postgresql://db.example.com/m?sslmode=verify-full"}}}
    p = TelemetryPolicy.from_dict(doc)
    assert p.match_destination("mlflow", "postgresql://u:pw@db.example.com/m?sslmode=verify-full").name == "pg"
    assert p.match_destination("mlflow", "postgresql://db.example.com/m?sslmode=disable") is None
    assert p.match_destination("mlflow", "postgresql://db.example.com/m") is None


@pytest.mark.parametrize("url", [
    "postgresql://db.example.com/m?sslmode=require&host=evil.example",
    "postgresql://db.example.com/m?sslmode=require&sslmode=disable",
    "postgresql://db.example.com/m?sslmode=bogus",
    "postgresql://db.example.com/m?sslmode",
    "postgresql://db.example.com/m?service=evil",
    "postgresql://db.example.com/m?sslrootcert=/tmp/x",
    "mysql://db.example.com/m?unix_socket=/tmp/x",
    "sqlite:////srv/m.db?mode=ro",
    "https://otel.example.com/v1?sslmode=require",
])
def test_any_other_query_is_refused(url):
    with pytest.raises(ProveviError):
        normal_endpoint(url)


def test_a_sqlite_path_keeps_its_trailing_slash():
    assert normal_endpoint("sqlite:////srv/mlflow.db") != normal_endpoint("sqlite:////srv/mlflow.db/")


def test_a_review_finding_url_matches_nothing():
    doc = {"version": 1, "hosts": ["good.example.com", "fass.de"], "destinations": {
        "pg": {"kind": "mlflow", "receives": "bodies", "endpoint": "postgresql://good.example.com:5432/mlflow"},
        "lite": {"kind": "mlflow", "receives": "bodies", "endpoint": "sqlite:////srv/mlflow.db"},
        "o": {"kind": "otlp", "receives": "names", "endpoint": "https://fass.de/v1/traces"},
        "o2": {"kind": "otlp", "receives": "names", "endpoint": "https://good.example.com/otlp"}}}
    p = TelemetryPolicy.from_dict(doc)
    assert p.match_destination("mlflow", "postgresql://good.example.com:5432/mlflow#@evil.com/mlflow") is None
    assert p.match_destination("mlflow", "sqlite:////srv/mlflow.db#x") is None
    assert p.match_destination("mlflow", "sqlite:////srv/mlflow.db/") is None
    assert p.match_destination("otlp", "https://faß.de/v1/traces") is None
    assert p.match_destination("otlp", "https+x://good.example.com/otlp") is None
    assert p.match_destination("otlp", "https://good.example.com/otlp?k=1") is None


def test_a_url_that_two_parsers_read_differently_matches_nothing():
    p = policy()
    assert p.match_destination("otlp", "https://evil.example\\@otel.example.com/v1/traces") is None
    assert p.match_destination("otlp", "https://evil.example@otel.example.com/v1/traces") is None


def test_a_database_url_with_a_query_matches_nothing():
    doc = {"version": 1, "hosts": ["db.example.com"],
           "destinations": {"pg": {"kind": "mlflow", "receives": "bodies", "endpoint": "postgresql://db.example.com/m"}}}
    p = TelemetryPolicy.from_dict(doc)
    assert p.match_destination("mlflow", "postgresql://u:pw@db.example.com/m").name == "pg"
    assert p.match_destination("mlflow", "postgresql://db.example.com/m?host=evil.example") is None


def test_a_variable_matches_the_destination_of_its_kind_and_endpoint():
    p = policy()
    assert p.match_destination("otlp", "https://OTEL.example.com:443/v1/traces/").name == "collector"
    assert p.match_destination("logfire", "https://otel.example.com/v1/traces") is None
    assert p.match_destination("otlp", "https://otel.example.com/v1/logs") is None
    assert p.match_destination("otlp", "not a url") is None
    assert p.match_destination("otlp", None) is None


def test_an_mlflow_destination_may_name_a_tracking_server_or_a_database():
    doc = {"version": 1, "hosts": ["mlflow.example.com", "db.example.com"], "destinations": {
        "server": {"kind": "mlflow", "receives": "usage", "endpoint": "https://mlflow.example.com"},
        "pg": {"kind": "mlflow", "receives": "bodies", "endpoint": "postgresql://db.example.com/mlflow"},
        "lite": {"kind": "mlflow", "receives": "bodies", "endpoint": "sqlite:////srv/mlflow.db"},
        "own": {"kind": "mlflow", "receives": "names"},
    }}
    p = TelemetryPolicy.from_dict(doc)
    assert p.match_destination("mlflow", "postgresql+psycopg2://u:pw@db.example.com:5432/mlflow").name == "pg"
    assert p.match_destination("mlflow", "sqlite:////srv/mlflow.db").name == "lite"
    assert p.match_destination("mlflow", "https://mlflow.example.com/").name == "server"
    assert p.match_destination("mlflow", None).name == "own"


@pytest.mark.parametrize("kind, endpoint, words", [
    ("otlp", "postgresql://db.example.com/x", "needs an http or https endpoint"),
    ("mlflow", "postgresql://db.evil.example/x", "'db.evil.example', which is not in hosts"),
    ("mlflow", "postgresql://u:pw@db.example.com/x", "no user or password"),
    ("otlp", "https://token@otel.example.com", "no user or password"),
    ("mlflow", "ftp://db.example.com", "needs an http, https or database endpoint"),
    ("mlflow", "sqlite://db.example.com/x.db", "absolute path"),
    ("mlflow", "postgresql://db.example.com/x?host=db.example.com", "no query"),
    ("otlp", "https://otel.example.com:0", "a port from 1"),
])
def test_a_policy_endpoint_is_checked(kind, endpoint, words):
    doc = {"version": 1, "hosts": ["db.example.com", "otel.example.com"],
           "destinations": {"d": {"kind": kind, "receives": "names", "endpoint": endpoint}}}
    with pytest.raises(ProveviError, match=words):
        TelemetryPolicy.from_dict(doc)


@pytest.mark.parametrize("env, want", [
    ({}, None),
    ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": ""}, None),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com"}, ("http/protobuf", "https://otel.example.com/v1/traces")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com/"}, ("http/protobuf", "https://otel.example.com/v1/traces")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com", "OTEL_EXPORTER_OTLP_PROTOCOL": "GRPC"},
     ("grpc", "https://otel.example.com")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
      "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/json"}, ("http/json", "https://otel.example.com/v1/traces")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://a.example", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://b.example/t"},
     ("http/protobuf", "https://b.example/t")),
    ({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://a.example"},
     ("http/protobuf", "https://a.example/v1/traces")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "otel.example.com:4317", "OTEL_TRACES_EXPORTER": "otlp_proto_grpc"},
     ("grpc", "https://otel.example.com:4317")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "localhost:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
      "OTEL_EXPORTER_OTLP_INSECURE": "true"}, ("grpc", "http://localhost:4317")),
    ({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://a.example", "OTEL_TRACES_EXPORTER": "otlp_proto_http"},
     ("http/protobuf", "https://a.example/v1/traces")),
])
def test_the_traces_target_follows_the_otel_rules(env, want):
    assert otlp_traces_target(env) == want


def test_an_unknown_otlp_protocol_is_refused():
    with pytest.raises(ProveviError, match="not one of"):
        otlp_traces_target({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://a.example", "OTEL_EXPORTER_OTLP_PROTOCOL": "udp"})


@pytest.mark.parametrize("value, plain", [("openai/gpt-4o-mini", True), ("claude-sonnet@20240620", True),
                                         ("bedrock/arn:aws:bedrock:us-east-1:1:x", True), ("ollama/llama3:8b", True), ("proj/u:p@h", False),
                                         ("proj/user@host:pw", False),
                                         ("sk-abcdef0123456789", False), ("sk-ant-api03-abcdefghijklmnop", False),
                                         ("prefix-sk-proj-abcdefghijklmnop", False), ("ghp_" + "a" * 36, False),
                                         ("x/AKIAIOSFODNN7EXAMPLE", False), ("hf_" + "a" * 34, False),
                                         ("sk_live_" + "a" * 24, False), ("eyJhbGciOiJIUzI1NiJ9.e30.x", False),
                                         ("xapp-1-abcdefghijk", False), ("task-sk-1", True), ("risk-model", True),
                                         ("hf_transfer", True), ("huggingface/hf_xet", True), ("sk-learn", True),
                                         ("AKIAK", True), ("ASIAN-model", True), (["a", "b"], True), (3, True),
                                         ("https://u:secret@h.example/v1", False), ("u:secret@h.example", False),
                                         ("ns:model@tag", False), ("token=abc", False), ("/home/someone/x", False),
                                         ("user@host:pw", False), ("../etc/passwd", False), ("a/./b", False),
                                         ("gpt-4.1", True), ("tmp/a/b", False), ("a//b", False),
                                         ("org/model", True), ("openrouter/meta-llama/llama-3-70b", False),
                                         (["a", "https://h.example/x"], False), ("two words", False), ({}, False), ([], False), (True, True)])
def test_a_plain_name_holds_no_url_password_setting_or_path(value, plain):
    from provevi import plain_name

    assert plain_name(value) is plain


def test_a_declared_name_may_have_more_than_one_slash_and_nothing_else():
    from provevi import plain_name

    model = "openrouter/meta-llama/llama-3-70b"
    assert plain_name(model, declared=[model]) is True
    assert plain_name([model, "tmp/a/b"], declared=[model]) is False
    assert plain_name("https://h.example/a/b", declared=["https://h.example/a/b"]) is False
    assert plain_name("a/../b/c", declared=["a/../b/c"]) is False
    with pytest.raises(TypeError):
        plain_name("r/meta-llama/l", declared=model)


@pytest.mark.parametrize("key, value, plain", [
    ("gen_ai.request.model", "openrouter/meta-llama/llama-3-70b", True),
    ("gen_ai.response.model", "accounts/fireworks/models/llama-v3p1-70b-instruct", True),
    ("model", "bedrock/arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.x", True),
    ("mlflow.llm.model", "projects/p/locations/us/publishers/google/models/gemini", True),
    ("gen_ai.request.model", "https://h.example/a/b", False), ("gen_ai.request.model", "token=x", False),
    ("gen_ai.system", "tmp/a/b", False), ("gen_ai.tool.name", "org/tool", True)])
def test_a_model_s_name_may_have_more_than_one_slash(key, value, plain):
    from provevi import plain_attribute

    assert plain_attribute(key, value) is plain


@pytest.mark.parametrize("name, plain", [("agent run", True), ("chat gpt-4o", True), ("one", True),
                                         ("three words here", False), ("host:token@x", False),
                                         ("chat https://h", False), ("", False), (None, False)])
def test_a_plain_span_name_is_one_or_two_plain_words(name, plain):
    from provevi import plain_span_name

    assert plain_span_name(name) is plain


def test_narrowing_hosts_drops_each_destination_on_a_host_it_cut():
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["api.openai.com", "mlflow.example"],
                                        "libraries": {"pydantic-ai": {"versions": ">=1", "telemetry": ["srv", "own"]}},
                                        "destinations": {"srv": {"kind": "mlflow", "receives": "usage",
                                                                 "endpoint": "https://mlflow.example"},
                                                         "own": {"kind": "mlflow", "receives": "usage"}}})
    narrowed = policy.narrow({"hosts": ["api.openai.com"]})
    assert set(narrowed.destinations) == {"own"}
    assert narrowed.match_destination("mlflow", "https://mlflow.example") is None
    assert [d.name for d in narrowed.destinations_for("pydantic-ai")] == ["own"]
    assert TelemetryPolicy.from_dict(narrowed.to_dict()).digest() == narrowed.digest()



@pytest.mark.parametrize("section", [{"hosts": ["api.openai.com"], "destinations": {"srv": None}},
                                     {"hosts": ["api.openai.com"], "libraries": {"pydantic-ai": {"telemetry": ["srv"]}}}])
def test_narrowing_refuses_a_destination_it_names_on_a_host_it_cuts(section):
    policy = TelemetryPolicy.from_dict({"version": 1, "hosts": ["api.openai.com", "mlflow.example"],
                                        "libraries": {"pydantic-ai": {"versions": ">=1", "telemetry": ["srv"]}},
                                        "destinations": {"srv": {"kind": "mlflow", "receives": "usage",
                                                                 "endpoint": "https://mlflow.example"}}})
    with pytest.raises(ProveviError, match="'srv' sends to a host that the section cuts"):
        policy.narrow(section)


@pytest.mark.parametrize("scope, names", [("pydantic-ai", ("pydantic-ai", "pydantic-ai-slim")),
                                          ("opentelemetry.sdk.trace", ("opentelemetry-sdk",)),
                                          ("litellm", ("litellm",)), ("", ())])
def test_a_span_scope_names_the_libraries_that_installed_it(scope, names):
    from provevi import span_libraries

    assert span_libraries(scope) == names


def test_a_scope_that_copies_a_name_counts_only_if_that_library_is_installed():
    # Gap 1: any code can call its scope litellm.
    from provevi import span_libraries

    assert span_libraries("not-installed-anywhere") == ()


def test_a_namespace_scope_that_no_distribution_ships_names_no_library():
    # Gap 2: distribution_of guesses the first opentelemetry distribution; the policy must not.
    from provevi import distribution_of, span_libraries

    assert distribution_of("opentelemetry.instrumentation.nothere") is not None
    assert span_libraries("opentelemetry.instrumentation.nothere") == ()


def test_no_scope_names_a_library_when_the_installed_packages_cannot_be_read(monkeypatch):
    # Gap 3: distribution_of falls back to the first part of the name.
    from importlib import metadata

    from provevi import span_libraries

    def broken():
        raise OSError("unreadable")

    from provevi import telemetry

    monkeypatch.setattr(metadata, "packages_distributions", broken)
    for cached in (span_libraries, telemetry._shipped):
        cached.cache_clear()
    try:
        assert span_libraries("litellm") == ()
    finally:
        for cached in (span_libraries, telemetry._shipped):
            cached.cache_clear()


def test_a_process_reads_the_installed_packages_once(monkeypatch):
    from importlib import metadata

    from provevi import telemetry

    calls = []
    real = metadata.packages_distributions
    monkeypatch.setattr(metadata, "packages_distributions", lambda: calls.append(1) or real())
    telemetry.span_libraries.cache_clear()
    telemetry._shipped.cache_clear()
    try:
        for scope in ("litellm", "pydantic-ai", "litellm", "opentelemetry.sdk.trace"):
            telemetry.span_libraries(scope)
        assert calls == [1]
    finally:
        telemetry.span_libraries.cache_clear()
        telemetry._shipped.cache_clear()



@pytest.mark.parametrize("scope", ["pydantic_ai.evil", "mlflow.evil.x", "pydantic.ai", "opentelemetry.sdk.evil"])
def test_a_scope_whose_module_no_library_holds_names_no_library(scope):
    from provevi import span_libraries

    assert span_libraries(scope) == ()
