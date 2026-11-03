"""The live feed of a run: start_run, follow and result, with no network and no keys."""
import asyncio

import pytest

from provrun import RunFile
from provrun import live as live_module
from provrun.evidence import read_events, verify
from provrun.live import LiveFeed, start_run
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec

RUN = """
run: {org: acme, domain: benefits, env: dev, actor: tester}
agent:
  name: q
  backend: litellm
  model: openai/gpt-4o-mini
  prompt: What was revenue?
  inputs: [INPUT]
  stream: true
  settings: {mock_response: Revenue was 42 million.}
"""


@pytest.fixture
def rf(tmp_path, docs):
    f = tmp_path / "run.yaml"
    f.write_text(RUN.replace("INPUT", f"file://{docs}/report.txt"))
    return RunFile.load(f)


def kw(catalog, runs):
    return {"catalog": catalog, "runs_root": runs}


async def collect(it):
    return [item async for item in it]


def test_a_viewer_sees_each_event_in_order_and_then_the_end(rf, catalog, runs):
    async def go():
        live = start_run(rf, **kw(catalog, runs))
        items = await collect(live.follow())
        return items, await live.result()

    items, out = asyncio.run(go())
    assert out.status == "completed", out.error
    assert [i["n"] for i in items] == list(range(1, len(items) + 1))
    events = [i for i in items if i["type"] == "event"]
    assert [i["event"] for i in events] == [e.type for e in read_events(out.folder / "evidence")]
    assert [i["seq"] for i in events] == list(range(len(events)))  # steps in threads keep their order
    assert items[-1] == {"n": len(items), "type": "done", "status": "completed", "run_id": out.run_id}
    # The names level: no text, and no data.
    assert not [i for i in items if i["type"] == "text"]
    assert all(set(i) <= {"n", "type", "event", "seq", "time", "component"} for i in events)


def test_a_late_viewer_gets_the_same_items_and_can_start_after_one(rf, catalog, runs):
    async def go():
        live = start_run(rf, **kw(catalog, runs))
        first = await collect(live.follow())
        later = await collect(live.follow())
        after = await collect(live.follow(after=3))
        return first, later, after

    first, later, after = asyncio.run(go())
    assert later == first and after == first[3:]


def test_two_viewers_at_once_get_the_same_items(rf, catalog, runs):
    async def go():
        live = start_run(rf, **kw(catalog, runs))
        return await asyncio.gather(collect(live.follow()), collect(live.follow()))

    a, b = asyncio.run(go())
    assert a == b and a[-1]["type"] == "done"


def test_a_viewer_that_stops_does_not_stop_the_run(rf, catalog, runs):
    async def go():
        live = start_run(rf, **kw(catalog, runs))
        async for _ in live.follow():
            break  # as a browser that closes
        return await live.result()

    out = asyncio.run(go())
    assert out.status == "completed" and verify(out.folder / "evidence") == []


def test_a_viewer_that_falls_behind_is_dropped_and_the_run_goes_on(rf, catalog, runs, monkeypatch):
    monkeypatch.setattr(live_module, "QUEUE", 2)

    async def go():
        live = start_run(rf, **kw(catalog, runs))
        viewer = live.follow()
        first = await viewer.__anext__()  # then it reads no more while the run goes on
        out = await live.result()
        rest = await asyncio.wait_for(collect(viewer), 10)  # a viewer that is never dropped would wait for ever
        return first, rest, out

    first, rest, out = asyncio.run(go())
    assert first["n"] == 1 and rest[-1] == {"type": "dropped"}
    assert out.status == "completed" and verify(out.folder / "evidence") == []


def test_a_dropped_viewer_comes_back_with_no_gap(rf, catalog, runs, monkeypatch):
    monkeypatch.setattr(live_module, "QUEUE", 3)

    async def go():
        live = start_run(rf, **kw(catalog, runs))
        viewer = live.follow()
        got = [await viewer.__anext__()]
        await live.result()  # the viewer read nothing while the run went on
        got += await asyncio.wait_for(collect(viewer), 10)
        assert got[-1] == {"type": "dropped"}
        seen = [i["n"] for i in got[:-1]]
        back = await collect(live.follow(after=seen[-1]))
        return seen + [i["n"] for i in back], len(live.feed.items)

    numbers, total = asyncio.run(go())
    assert numbers == list(range(1, total + 1))  # every item, once, in order


def test_an_error_in_the_feed_never_fails_a_run(catalog, runs, docs):
    # A strict run fails when a sink fails; the feed keeps its own errors.
    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-4o-mini", prompt="p", stream=True,
                      settings={"mock_response": "42 million"}, inputs=[InputSpec(f"file://{docs}/report.txt")])
    closed = asyncio.new_event_loop()
    closed.close()
    feed = LiveFeed(closed, level="bodies")
    out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="b", env="dev",
                                mode="strict", actor="t", feed=feed))
    assert out.status == "completed", out.error


def test_follow_takes_no_negative_number():
    async def go():
        with pytest.raises(ValueError):
            await LiveFeed(asyncio.get_running_loop()).follow(after=-2).__anext__()

    asyncio.run(go())


def test_a_run_cancelled_before_it_opens_ends_its_feed_with_no_run_id(rf, catalog, runs):
    async def go():
        live = start_run(rf, **kw(catalog, runs))
        live.cancel()
        return await collect(live.follow())

    items = asyncio.run(go())
    assert items == [{"n": 1, "type": "done", "status": "cancelled", "run_id": None}]
    assert not runs.exists() or not list(runs.iterdir())


def test_a_cancelled_run_ends_its_feed_and_is_sealed(rf, catalog, runs, docs, monkeypatch):
    from provrun.resolve import ProvcatResolver

    def slow(self, ctx, grant):
        import time
        time.sleep(0.3)
        return original(self, ctx, grant)

    original = ProvcatResolver.snapshot
    monkeypatch.setattr(ProvcatResolver, "snapshot", slow)

    async def go():
        live = start_run(rf, **kw(catalog, runs))
        viewer = live.follow()
        await viewer.__anext__()
        live.cancel()
        items = await collect(viewer)
        with pytest.raises(asyncio.CancelledError):
            await live.result()
        return items

    items = asyncio.run(go())
    [folder] = list(runs.iterdir())
    assert items[-1]["type"] == "done" and items[-1]["status"] == "cancelled"
    assert items[-1]["run_id"] == folder.name
    assert read_events(folder / "evidence")[-1].type == "run_failed"


def test_at_the_bodies_level_the_feed_carries_the_pieces_in_order(catalog, runs, docs):
    agent = AgentSpec(name="q", backend="litellm", model="openai/gpt-4o-mini", prompt="p", stream=True,
                      settings={"mock_response": "Revenue was 42 million."},
                      inputs=[InputSpec(f"file://{docs}/report.txt")])

    async def go():
        feed = LiveFeed(asyncio.get_running_loop(), level="bodies")
        out = await run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="b", env="dev",
                              actor="t", feed=feed)
        await asyncio.sleep(0)  # let the last items reach the loop
        return feed.items, out

    items, out = asyncio.run(go())
    texts = [i["n"] for i in items if i["type"] == "text"]
    assert len(texts) > 1 and "".join(i["text"] for i in items if i["type"] == "text") == "Revenue was 42 million."
    called = next(i["n"] for i in items if i.get("event") == "model_called")
    started = next(i["n"] for i in items if i.get("event") == "prompt_rendered")
    assert started < min(texts) and max(texts) < called  # the pieces come while the model is called


def test_a_feed_takes_only_a_known_level():
    with pytest.raises(ValueError):
        LiveFeed(asyncio.new_event_loop(), level="everything")


def test_items_that_come_while_a_viewer_reads_the_earlier_ones_are_not_missed():
    async def go():
        feed = LiveFeed(asyncio.get_running_loop())
        for i in range(3):
            feed._push({"type": "event", "event": f"e{i}"})
        viewer = feed.follow()
        got = [await viewer.__anext__()]  # the viewer is still among the earlier items
        feed._push({"type": "event", "event": "e3"})
        feed._push({"type": "done", "status": "completed", "run_id": "r"})
        got += await collect(viewer)
        return [i["n"] for i in got]

    assert asyncio.run(go()) == [1, 2, 3, 4, 5]
