"""What a viewer of a run's live feed sees, and the feed of a finished run."""
import json
import sqlite3
from pathlib import Path

import pytest

from provevi import EvidenceEvent, FileStore, ProveviError, SqliteStore, TelemetryPolicy, replay, sha256_bytes, \
    shown_at

RUN = "run-20261103T000000Z-00000001"
DOC = "data://acme/demo/dev/reports/report.txt"
BUNDLE = "run://acme/demo/dev/" + RUN
LITELLM = {"request": [{"role": "user", "content": "the prompt"}], "response": "Revenue was 42 million."}
PYDANTIC = {"request": [], "response": [{"kind": "response", "parts": [
    {"part_kind": "thinking", "content": "the hidden reasoning"},
    {"part_kind": "text", "content": "Revenue was "}, {"part_kind": "tool-call", "tool_name": "read"},
    {"part_kind": "text", "content": "42 million."}]}]}


def events_of(answer=LITELLM, ending="run_completed"):
    """A run as provrun writes it: (type, component, refs, data, payload)."""
    return [
        ("run_created", "RunContext", ["run://acme/demo/dev/" + RUN],
         {"org": "acme", "agent": "q", "spec": {"model": "openai/gpt-4o-mini", "prompt": "the secret prompt"}}, None),
        ("input_staged", "Stager", [DOC], {"name": "report.txt", "sha256": "ab" * 32, "agent_path": "/input/report.txt"},
         None),
        ("model_called", "Backend", [], {"model": "openrouter/meta-llama/llama-3-70b", "input_tokens": 10,
                                         "output_tokens": 20, "first_token_ms": 312, "finish_reason": "stop",
                                         "streamed": True, "settings": {"temperature": 0}},
         json.dumps(answer).encode()),
        ("span_recorded", "telemetry", [], {"name": "agent run", "library": "pydantic-ai", "attributes": {
            "gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 7, "gen_ai.prompt": "the secret prompt",
            "server.address": "tmp/a/b"}}, None),
        ("run_failed" if ending == "run_failed" else ending, "RunContext", [], {"error": "a long free text"}, None),
    ]


def write_run(store, root: Path, events=None, *, seal=True):
    folder = root / RUN
    (folder / "output").mkdir(parents=True)
    writer = store.open_run(RUN, folder / "evidence")
    prev = None
    events = events_of() if events is None else events
    for seq, (t, component, refs, data, payload) in enumerate(events):
        ref = writer.put_payload(payload) if payload is not None else None
        line = EvidenceEvent(RUN, seq, f"2026-11-03T00:00:{seq:02}.000000Z", t, component, "you@example.com",
                             tuple(refs), data, ref, "observed", prev).line()
        writer.append(line)
        prev = sha256_bytes(line.encode())
    writer.close()
    if seal:
        writer.seal({"run_id": RUN, "events": len(events), "last": prev, "payloads": writer.payload_names(),
                     "complete": True, "problems": []})
    (folder / "bundle.json").write_text(json.dumps({"uri": BUNDLE, "pinned": None, "note": ""}))
    writer.record_bundle(BUNDLE, None)
    return folder


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "evidence.db")
    write_run(s, tmp_path / "runs")
    return s


def items(store, level, **kw):
    return list(replay(store, RUN, level, **kw))


def test_at_names_an_event_shows_its_names_and_no_number_body_or_ref(store):
    created, staged, model, span, end = [i for i in items(store, "names") if i["type"] == "event"]
    assert created["event"] == "run_created" and created["data"] == {"org": "acme", "agent": "q"}
    assert created["actor"] == "you@example.com" and "refs" not in created and "payload" not in created
    assert staged["data"] == {"sha256": "ab" * 32}
    assert model["data"] == {"model": "openrouter/meta-llama/llama-3-70b", "finish_reason": "stop",
                             "streamed": True}
    assert span["data"] == {"name": "agent run", "library": "pydantic-ai",
                            "attributes": {"gen_ai.request.model": "gpt-4o"}}
    assert end["data"] == {}


def test_at_usage_an_event_adds_its_numbers(store):
    model = [i for i in items(store, "usage") if i.get("event") == "model_called"][0]
    assert model["data"]["input_tokens"] == 10 and model["data"]["first_token_ms"] == 312
    span = [i for i in items(store, "usage") if i.get("event") == "span_recorded"][0]
    assert span["data"]["attributes"] == {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 7}


def test_below_bodies_the_feed_has_no_text(store):
    for level in ("names", "usage"):
        got = items(store, level)
        assert not [i for i in got if i["type"] == "text"]
        assert got[-1] == {"n": len(got), "type": "done", "status": "completed", "run_id": RUN}
        assert "the secret prompt" not in json.dumps(got) and "a long free text" not in json.dumps(got)


def test_at_bodies_the_feed_has_everything_and_the_answer(store):
    got = items(store, "bodies")
    assert [i["type"] for i in got] == ["event", "event", "event", "text", "event", "event", "done"]
    model = got[2]
    assert model["data"]["settings"] == {"temperature": 0} and model["payload"] == LITELLM
    assert got[0]["refs"] == ["run://acme/demo/dev/" + RUN] and got[0]["data"]["spec"]["prompt"] == "the secret prompt"
    assert got[3] == {"n": 4, "type": "text", "text": "Revenue was 42 million."}
    assert [i["n"] for i in got] == list(range(1, len(got) + 1))
    assert got[-1] == {"n": 7, "type": "done", "status": "completed", "run_id": RUN}


def test_the_answer_of_pydantic_ai_joins_its_text_parts(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", events_of(PYDANTIC))
    assert [i["text"] for i in items(s, "bodies") if i["type"] == "text"] == ["Revenue was 42 million."]


def test_a_failed_run_ends_failed(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", events_of(ending="run_failed"))
    assert items(s, "names")[-1]["status"] == "failed"


def test_the_file_store_replays_from_the_run_folder(tmp_path):
    folder = write_run(FileStore(tmp_path / "runs"), tmp_path / "runs")
    got = list(replay(FileStore(tmp_path / "runs"), RUN, "bodies", folder=folder / "evidence"))
    assert got[3]["text"] == "Revenue was 42 million." and got[-1]["status"] == "completed"


def test_a_run_that_does_not_verify_is_refused_before_the_first_item(store):
    conn = sqlite3.connect(store.path)
    conn.execute("DELETE FROM provevi_events WHERE run_id = ? AND seq = 1", (RUN,))
    conn.commit()
    with pytest.raises(ProveviError, match="does not verify"):
        replay(store, RUN, "names")


def test_a_level_that_is_not_one_is_refused(store):
    with pytest.raises(ProveviError, match="not a level"):
        replay(store, RUN, "everything")
    event = EvidenceEvent(RUN, 0, "2026-11-03T00:00:00.000000Z", "run_created", "RunContext", "you@example.com")
    with pytest.raises(ProveviError, match="not a level"):
        shown_at(event, "everything")


def test_with_no_live_viewer_an_event_shows_nothing():
    event = EvidenceEvent(RUN, 0, "2026-11-03T00:00:00.000000Z", "run_created", "RunContext", "you@example.com")
    assert shown_at(event, None) is None


def test_an_actor_that_is_not_a_name_shows_only_at_bodies():
    event = EvidenceEvent(RUN, 0, "2026-11-03T00:00:00.000000Z", "run_created", "RunContext", "Jo Smith")
    assert "actor" not in shown_at(event, "names") and shown_at(event, "bodies")["actor"] == "Jo Smith"


def live_policy(**live):
    return TelemetryPolicy.from_dict({"version": 1, "destinations": {"browser": {"kind": "live", **live}}})


def test_the_policy_gives_the_live_viewer_s_level():
    assert live_policy(receives="usage").live_level() == "usage"
    assert TelemetryPolicy.deny_all().live_level() is None
    assert TelemetryPolicy.from_dict({"version": 1}).live_level() is None


def test_a_run_file_can_lower_the_live_level_but_not_raise_it():
    policy = live_policy(receives="usage")
    assert policy.narrow({"destinations": {"browser": {"receives": "names"}}}).live_level() == "names"
    assert policy.narrow({"destinations": {}}).live_level() is None
    with pytest.raises(ProveviError, match="more than the policy"):
        policy.narrow({"destinations": {"browser": {"receives": "bodies"}}})


@pytest.mark.parametrize("doc, problem", [
    ({"destinations": {"b": {"kind": "live", "receives": "names", "endpoint": "https://h.example"}}},
     "no endpoint"),
    ({"destinations": {"a": {"kind": "live", "receives": "names"}, "b": {"kind": "live", "receives": "usage"}}},
     "both live viewers"),
    ({"destinations": {"b": {"kind": "live", "receives": "names"}},
      "libraries": {"pydantic-ai": {"versions": ">=1", "telemetry": ["b"]}}}, "a live viewer"),
    ({"destinations": {"b": {"kind": "live"}}}, "receives None"),
])
def test_a_live_viewer_that_is_not_one_is_refused(doc, problem):
    with pytest.raises(ProveviError, match=problem):
        TelemetryPolicy.from_dict({"version": 1, **doc})


def test_an_open_run_is_not_replayed(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", events_of()[:-1], seal=False)
    with pytest.raises(ProveviError):
        replay(s, RUN, "names")


def event(type_, data, component="RunContext"):
    return EvidenceEvent(RUN, 0, "2026-11-03T00:00:00.000000Z", type_, component, "you@example.com", (), data)


@pytest.mark.parametrize("type_, data", [
    ("note_added", {"text": "Patient-JaneDoe-HIV-positive"}),
    ("access_requested", {"purpose": "JaneDoe-review"}),
    ("access_granted", {"reason": "approved-by-JaneDoe"}),
    ("input_staged", {"name": "JaneDoe_medical_record.pdf"}),
    ("context_resolved", {"physical": "bucket/key"}),
    ("telemetry_blocked", {"detail": "api.secret-host.internal:443"}),
    ("run_created", {"Jane Doe salary is 300k": True, "JaneDoe": True}),
    ("run_created", {"token": "hunter2"}),
])
def test_below_bodies_only_listed_keys_show(type_, data):
    for level in ("names", "usage"):
        assert shown_at(event(type_, data), level)["data"] == {}
    assert shown_at(event(type_, data), "bodies")["data"] == data


def test_below_bodies_a_component_that_is_not_a_name_stays_out():
    shown = shown_at(event("tool_called", {}, component="tool:read /etc/passwd?token=abc"), "names")
    assert "component" not in shown
    assert shown_at(event("tool_called", {}, component="tool:read"), "names")["component"] == "tool:read"


def test_below_bodies_a_span_keeps_no_secret_in_a_mapping_or_its_name():
    span = event("span_recorded", {"name": "chat /tmp/x?token=1", "attributes": {
        "mlflow.chat.tokenUsage": {"sk-abcdefghijklmnopqrstuv": 1}, "gen_ai.usage.input_tokens": 3}})
    shown = shown_at(span, "usage")["data"]
    assert "name" not in shown and shown["attributes"] == {"gen_ai.usage.input_tokens": 3}


def test_the_answer_leaves_out_thinking(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    write_run(s, tmp_path / "runs", events_of(PYDANTIC))
    assert "the hidden reasoning" not in json.dumps([i for i in items(s, "bodies") if i["type"] == "text"])


def test_the_file_store_replays_without_a_folder(tmp_path):
    write_run(FileStore(tmp_path / "runs"), tmp_path / "runs")
    assert list(replay(FileStore(tmp_path / "runs"), RUN, "names"))[-1]["status"] == "completed"


def test_a_cancelled_run_ends_cancelled(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    events = events_of(ending="run_failed")
    events[-1] = ("run_failed", "RunContext", [], {"error": "CancelledError: "}, None)
    write_run(s, tmp_path / "runs", events)
    assert items(s, "names")[-1]["status"] == "cancelled"


def test_a_stream_that_failed_part_way_replays_its_text_so_far(tmp_path):
    s = SqliteStore(tmp_path / "e.db")
    events = events_of(ending="run_failed")
    events.insert(-1, ("step_failed", "Backend", [], {"error": "the stream broke", "pieces": 3},
                       json.dumps({"partial_response": "Revenue was 4"}).encode()))
    write_run(s, tmp_path / "runs", events)
    got = items(s, "bodies")
    i = [n for n, x in enumerate(got) if x.get("event") == "step_failed"][0]
    assert got[i + 1] == {"n": i + 2, "type": "text", "text": "Revenue was 4"}
    failed = [x for x in items(s, "usage") if x.get("event") == "step_failed"][0]
    assert failed["data"] == {"pieces": 3}
    assert not [x for x in items(s, "usage") if x["type"] == "text"]


def test_below_bodies_a_run_shows_its_parent_and_its_experiment():
    pin = "experiment://acme/risk/dev/credit/q?at=commit:7311e8221f7268c01fb243ef177028736e0595bf"
    data = {"experiment": "experiment://acme/risk/dev/credit/q", "experiment_pin": pin,
            "experiment_sha256": "ab" * 32, "parent_run": "run-20261109T221005Z-0a1b2c3d", "agent": "q"}
    shown = shown_at(event("run_created", data), "names")["data"]
    assert shown == {"experiment": data["experiment"], "experiment_pin": pin, "parent_run": "run-20261109T221005Z-0a1b2c3d",
                     "agent": "q"}


@pytest.mark.parametrize("data", [
    {"experiment": "experiment://acme/q?at=branch:JaneDoe-review"},
    {"experiment_pin": "experiment://acme.example/q?at=commit:7311e8221f"},
    {"experiment": "experiment://home/alice/.ssh/id_rsa"},
    {"parent_run": "run/home/alice/x"}, {"parent_run": "token=abc"},
    {"experiment": {"uri": "experiment://acme/q"}},
])
def test_below_bodies_a_parent_or_experiment_that_is_not_plain_stays_out(data):
    assert shown_at(event("run_created", data), "names")["data"] == {}
    assert shown_at(event("run_created", data), "bodies")["data"] == data


def test_below_bodies_the_run_s_own_model_may_have_two_slashes_but_a_span_s_plain_model_key_may_not():
    model = "openrouter/meta-llama/llama-3-70b"
    called = event("model_called", {"model": model, "answered_by": model})
    assert shown_at(called, "names")["data"] == {"model": model, "answered_by": model}
    span = event("span_recorded", {"name": "chat", "attributes": {"model": model, "gen_ai.request.model": model}})
    assert shown_at(span, "names")["data"]["attributes"] == {"gen_ai.request.model": model}


def test_the_answer_and_payload_readers_are_public(tmp_path):
    import provevi.live as live
    from provevi import answer_of, payload_of

    assert live._answer is answer_of and live._payload is payload_of  # provrun's earlier imports keep working
    assert answer_of({"response": "4"}) == "4" and answer_of({"partial_response": "fo"}) == "fo"
    assert answer_of("not a payload") is None

    class Record:
        def payload_bytes(self, digest):
            return {"a": b'{"response": "4"}', "b": b"plain"}.get(digest)

    assert payload_of(Record(), "a") == {"response": "4"} and payload_of(Record(), "b") == "plain"
    assert payload_of(Record(), "c") is None


def test_a_payload_nested_too_deep_for_json_is_read_as_text():
    from provevi import payload_of

    class Record:
        def payload_bytes(self, digest):
            return b"[" * 100000

    assert payload_of(Record(), "a") == "[" * 100000
