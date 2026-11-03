"""A live feed of a run, for a program that shows the run as it goes, such as
a FastAPI app that sends it to a browser:

    live = start_run(RunFile.load("run.yaml"), catalog=Catalog.open())
    async for item in live.follow():
        ...                       # send each item on
    outcome = await live.result()

The feed gives numbered items, in the order the run made them:

- ``{"n": 1, "type": "event", "event": "run_created", "seq": 0, ...}`` for each
  evidence event;
- ``{"n": 7, "type": "text", "text": "Revenue "}`` for each piece of a
  streamed reply, at the ``bodies`` level only;
- ``{"n": 12, "type": "done", "status": "completed", "run_id": ...}`` last. The
  status is that of the run's task: ``completed``, ``failed`` or ``cancelled``.
  The run ID is None for a run that ended before it opened.

The feed shows only the ``names`` level for now: each event's type, number,
time and component, when the component is a plain name, and no text. The run's telemetry policy will decide the
level, once provevi can name a live viewer in it.

A viewer that falls behind by more than ``QUEUE`` items is dropped: it gets
each item it was given, then ``{"type": "dropped"}``, and the run never waits
for it. It can come back with ``follow(after=n)``, n the last item it got. A viewer that stops following only stops: the run goes on, and its
evidence is sealed as always. ``follow(after=n)`` starts after item n, so a
viewer that comes back gets what it missed. The evidence is the lasting
record; the feed lives only as long as the program.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from provevi import Sink, plain_name

from .runner import RunOutcome, run_file
from .spec import RunFile

QUEUE = 1000  # the items a viewer may fall behind by before it is dropped
LEVELS = ("names", "usage", "bodies")


class LiveFeed(Sink):
    """The items of one run, kept in order, and handed to each viewer. It is
    an evidence sink, so it gets each event as it is written, from any
    thread; each item goes to the event loop, so items keep their order."""

    def __init__(self, loop: asyncio.AbstractEventLoop, level: str = "names"):
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}, not {level!r}")
        self.loop, self.level = loop, level
        self.items: list[dict] = []
        self.ended = False
        self.run_id: str | None = None  # from the first event; a run that failed before it opened has none
        self._viewers: list[asyncio.Queue] = []

    def append(self, line: str) -> None:
        """An evidence line, from the recorder, in any thread. A viewer must
        never fail a run, so the feed keeps its own errors."""
        try:
            self._event(line)
        except Exception:  # such as a closed event loop
            pass

    def _event(self, line: str) -> None:
        event = json.loads(line)
        self.run_id = self.run_id or event["run_id"]
        item = {"type": "event", "event": event["type"], "seq": event["seq"], "time": event["time"]}
        if plain_name(event["component"]):  # a component such as tool:<name> can carry a name that is not plain
            item["component"] = event["component"]
        self.loop.call_soon_threadsafe(self._push, item)

    def text(self, piece: str) -> None:
        """A piece of a streamed reply, which only the bodies level shows."""
        if self.level == "bodies":
            self._send({"type": "text", "text": piece})

    def end(self, item: dict) -> None:
        """The last item, once the run is over."""
        self._send(item)

    def _send(self, item: dict) -> None:
        try:
            self.loop.call_soon_threadsafe(self._push, item)
        except Exception:  # such as a closed event loop; the run goes on
            pass

    def _push(self, item: dict) -> None:
        if self.ended:
            return
        item = {"n": len(self.items) + 1, **item}
        self.items.append(item)
        self.ended = item["type"] == "done"
        for queue in list(self._viewers):
            if queue.qsize() >= QUEUE:  # a slow viewer is dropped; the run does not wait
                # It still gets every item it was given, then the drop, so
                # follow(after=its last n) goes on with no gap.
                self._viewers.remove(queue)
                queue.put_nowait({"type": "dropped"})
            else:
                queue.put_nowait(item)

    async def follow(self, after: int = 0) -> AsyncIterator[dict]:
        """The items after number after, as they come, up to the last."""
        if not isinstance(after, int) or after < 0:
            raise ValueError(f"after must be a number of an item, 0 or more; got {after!r}")
        queue: asyncio.Queue = asyncio.Queue()  # held to QUEUE items by _push
        # Listen first, then take what came before: an item pushed while the
        # earlier ones are given goes to the queue, and none is missed.
        if not self.ended:
            self._viewers.append(queue)
        last = after
        try:
            for item in self.items[after:]:
                yield item
                last = item["n"]
            if self.items and self.items[-1]["type"] == "done" and last >= self.items[-1]["n"]:
                return
            while True:
                item = await queue.get()
                if item["type"] == "dropped":
                    yield item
                    return
                if item["n"] <= last:
                    if item["type"] == "done":
                        return  # the viewer asked to start after the run's end
                    continue  # given from the list already
                yield item
                last = item["n"]
                if item["type"] == "done":
                    return
        finally:
            if queue in self._viewers:
                self._viewers.remove(queue)


class LiveRun:
    """A run that has started, with its feed."""

    def __init__(self, task: asyncio.Task, feed: LiveFeed):
        self.task, self.feed = task, feed

    def follow(self, after: int = 0) -> AsyncIterator[dict]:
        """The run's items after number after, as they come; see the module."""
        return self.feed.follow(after)

    async def result(self) -> RunOutcome:
        """Wait for the run, and give its outcome, as run_file does."""
        return await self.task

    def cancel(self) -> None:
        """Cancel the run. A run that has opened ends with run_failed, unless
        it was being sealed, which ends as it was; result() then raises
        CancelledError. A run cancelled before it opened leaves no evidence."""
        self.task.cancel()


def start_run(rf: RunFile, **kw: Any) -> LiveRun:
    """Start a run file as a task in the running event loop, and give it with
    its feed. The keyword arguments are those of run_file."""
    loop = asyncio.get_running_loop()
    feed = LiveFeed(loop)
    task = loop.create_task(run_file(rf, feed=feed, **kw))

    def ended(t: asyncio.Task) -> None:
        if t.cancelled():
            feed.end({"type": "done", "status": "cancelled", "run_id": feed.run_id})
        elif t.exception() is not None:
            feed.end({"type": "done", "status": "failed", "run_id": feed.run_id})
        else:
            out = t.result()
            feed.end({"type": "done", "status": out.status, "run_id": out.run_id})

    task.add_done_callback(ended)
    return LiveRun(task, feed)

