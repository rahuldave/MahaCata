"""A small FastAPI server that starts a run and sends its live feed to a
browser, as server-sent events. Run it from the top folder of the repository:

    PROVRUN_TELEMETRY_POLICY=packages/provrun/examples/live-policy.yaml \\
    PROVRUN_LIVE_RUN_FILE=packages/provrun/examples/file-run.yaml \\
        uv run uvicorn --app-dir packages/provrun/examples live_server:app --port 8001

Then open http://127.0.0.1:8001/ and press Start. The server runs only the
run file it was given: a browser cannot name a file on the server. The
policy's live viewer decides what the browser sees; with none, only the end.

    POST   /runs              start a run; gives {"key": ...}
    GET    /runs/{key}/events the run's feed, one event per item; a browser
                              that reconnects sends Last-Event-ID and goes on
    DELETE /runs/{key}        cancel the run

It is an example, not a service to put on a network: any client can start a
run, and it keeps every run in memory until the process ends.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from provrun import LiveRun, RunFile, start_run

PAGE = """<!doctype html>
<meta charset="utf-8"><title>A provrun run, live</title>
<button id="start">Start</button> <button id="cancel" disabled>Cancel</button>
<pre id="out"></pre>
<script>
const out = document.getElementById("out");
let key = null;
document.getElementById("start").onclick = async () => {
  key = (await (await fetch("runs", {method: "POST"})).json()).key;
  document.getElementById("cancel").disabled = false;
  const feed = new EventSource(`runs/${key}/events`);
  feed.onmessage = (m) => {
    const item = JSON.parse(m.data);
    out.textContent += item.type === "text" ? item.text : `\\n${item.n} ${item.event || item.status || item.type}`;
    if (item.type === "done" || item.type === "dropped") feed.close();
  };
};
document.getElementById("cancel").onclick = () => fetch(`runs/${key}`, {method: "DELETE"});
</script>
"""


def make_app(run_file: str | Path, **run_kw) -> FastAPI:
    """The app, for one run file. run_kw goes to start_run, such as catalog=
    and runs_root=; with no catalog, the provcat configuration is read."""
    app = FastAPI()
    runs: dict[str, LiveRun] = {}  # in this process only; a second worker would not see them

    def kw() -> dict:
        if "catalog" not in run_kw:
            from provcat import Catalog

            run_kw["catalog"] = Catalog.open()
        return run_kw

    @app.get("/", response_class=HTMLResponse)
    async def page() -> str:
        return PAGE

    @app.post("/runs")
    async def start() -> dict:
        key = uuid.uuid4().hex
        runs[key] = start_run(RunFile.load(run_file), **kw())
        return {"key": key}

    @app.get("/runs/{key}/events")
    async def events(key: str, last_event_id: int = Header(0, ge=0)) -> StreamingResponse:
        live = _find(runs, key)

        async def stream():
            async for item in live.follow(after=last_event_id):
                # The item's number is the event's ID, so a browser that reconnects goes on after it.
                head = f"id: {item['n']}\n" if "n" in item else ""
                yield f"{head}data: {json.dumps(item)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.delete("/runs/{key}")
    async def cancel(key: str) -> dict:
        _find(runs, key).cancel()
        return {"cancelled": key}

    return app


def _find(runs: dict[str, LiveRun], key: str) -> LiveRun:
    if key not in runs:
        raise HTTPException(404, "no such run")
    return runs[key]


app = make_app(os.environ["PROVRUN_LIVE_RUN_FILE"]) if "PROVRUN_LIVE_RUN_FILE" in os.environ else None
