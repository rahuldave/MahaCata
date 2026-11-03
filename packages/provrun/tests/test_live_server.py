"""The FastAPI example of a live feed, packages/provrun/examples/live_server.py, with no network and no keys."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from provrun import read_events

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "live_server.py"
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


def load():
    spec = importlib.util.spec_from_file_location("live_server", EXAMPLE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(tmp_path, docs, catalog, runs):
    f = tmp_path / "run.yaml"
    f.write_text(RUN.replace("INPUT", f"file://{docs}/report.txt"))
    return load().make_app(f, catalog=catalog, runs_root=runs)


def items(text):
    """The items of a server-sent event stream, with their IDs."""
    out = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((fields.get("id"), json.loads(fields["data"])))
    return out


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_a_browser_gets_the_feed_and_comes_back_after_an_id(app, runs):
    async def go():
        async with client(app) as c:
            key = (await c.post("/runs")).json()["key"]
            whole = items((await c.get(f"/runs/{key}/events")).text)
            again = items((await c.get(f"/runs/{key}/events", headers={"Last-Event-ID": "5"})).text)
            return whole, again

    whole, again = asyncio.run(go())
    assert [i for i, _ in whole] == [str(n) for n in range(1, len(whole) + 1)]  # each item's ID is its number
    assert whole[-1][1]["type"] == "done" and whole[-1][1]["status"] == "completed"
    [folder] = list(runs.iterdir())
    assert [x["event"] for _, x in whole if x["type"] == "event"] == [e.type for e in read_events(folder / "evidence")]
    assert again == whole[5:]


def test_a_cancel_ends_the_feed(app):
    async def go():
        async with client(app) as c:
            key = (await c.post("/runs")).json()["key"]
            assert (await c.delete(f"/runs/{key}")).status_code == 200
            return items((await c.get(f"/runs/{key}/events")).text)

    feed = asyncio.run(go())
    assert feed[-1][1]["status"] == "cancelled"


def test_an_unknown_run_or_a_bad_id_is_refused(app):
    async def go():
        async with client(app) as c:
            missing = await c.get("/runs/nope/events")
            key = (await c.post("/runs")).json()["key"]
            bad = await c.get(f"/runs/{key}/events", headers={"Last-Event-ID": "-1"})
            await c.get(f"/runs/{key}/events")  # let the run end
            return missing.status_code, bad.status_code, (await c.get("/")).text

    missing, bad, page = asyncio.run(go())
    assert missing == 404 and bad == 422 and "EventSource" in page


def test_a_browser_that_asks_past_the_end_gets_an_ended_stream(app):
    async def go():
        async with client(app) as c:
            key = (await c.post("/runs")).json()["key"]
            past = await asyncio.wait_for(c.get(f"/runs/{key}/events", headers={"Last-Event-ID": "9999"}), 30)
            return past.status_code, past.text

    assert asyncio.run(go()) == (200, "")
