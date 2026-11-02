"""The telemetry policy: reading, checking and narrowing it, and what each
destination receives."""
from __future__ import annotations

import json

import pytest

from provevi import EvidenceEvent, FileStore, ProveviError, sha256_bytes
from provevi.cli import main
from provevi.telemetry import (BLOCKED, TelemetryPolicy, attributes_for, blocked_data, level_of, split_body)

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
                         "libraries": {"pydantic-ai": {}}, "hosts": ["api.anthropic.com", "s3.amazonaws.com"]})
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
    assert capsys.readouterr().out == f"{run_id}: no blocked attempts\n"
