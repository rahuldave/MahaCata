"""A live feed of a run, for a program that shows the run as it goes, such as
a FastAPI app that sends it to a browser:

    live = start_run(RunFile.load("run.yaml"), catalog=Catalog.open())
    async for item in live.follow():
        ...                       # send each item on
    outcome = await live.result()

The feed gives numbered items, in the order the run made them:

- ``{"n": 1, "type": "event", "event": "run_created", "seq": 0, ...}`` for each
  evidence event, as provevi's ``shown_at`` cuts it;
- ``{"n": 7, "type": "text", "text": "Revenue "}`` for each piece of a
  streamed reply, at the ``bodies`` level only;
- ``{"n": 12, "type": "done", "status": "completed", "run_id": ...}`` last. The
  status is ``completed``, ``failed`` or ``cancelled``, as the run's last event
  says, so a run cancelled while it was sealed shows as it was sealed. A run
  that ended before it opened has no events; its status is that of its task,
  and its run ID is None.

The run's telemetry policy sets the level, as the level of its destination of
kind ``live``, which a run file's ``telemetry`` section can lower. A policy
with no such destination shows a viewer no event and no text, only the end.
The event items are those that provevi's ``replay`` gives once the run has
ended, so a viewer that comes late can read the evidence instead. A text item
comes from each piece of a streamed reply; a reply that does not stream
comes as one text item after its ``model_called`` event, as replay gives it.
At ``bodies`` the feed keeps only the name of each payload, and reads the
payload from the run's evidence as it hands the item to a viewer, so a large
payload is not held in memory. The feed keeps every other item, with the
pieces of each streamed reply, as long as the program runs.

A viewer that falls behind by more than ``QUEUE`` items is dropped: it gets
each item it was given, then ``{"type": "dropped"}``, and the run never waits
for it. It can come back with ``follow(after=n)``, n the last item it got. A viewer that stops following only stops: the run goes on, and its
evidence is sealed as always. ``follow(after=n)`` starts after item n, so a
viewer that comes back gets what it missed. The evidence is the lasting
record; the feed lives only as long as the program.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from provevi import LEVELS, EvidenceEvent, Sink, answer_of, payload_of, shown_at

from .runner import RunOutcome, run_file
from .spec import RunFile

QUEUE = 1000  # the items a viewer may fall behind by before it is dropped


class LiveFeed(Sink):
    """The items of one run, kept in order, and handed to each viewer. It is
    an evidence sink, so it gets each event as it is written, from any
    thread; each item goes to the event loop, so items keep their order."""

    def __init__(self, loop: asyncio.AbstractEventLoop, level: str | None = None):
        if level is not None and level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS} or None, not {level!r}")
        # The run sets level from its policy as it starts, and writer, the
        # run's evidence writer, which holds the payloads that bodies shows.
        self.loop, self.level = loop, level
        self.writer: Any = None
        self.items: list[dict] = []
        self.ended = False
        self.run_id: str | None = None  # from the first event; a run that failed before it opened has none
        self.ending: str | None = None  # the status that the run's last event gives, once it is written
        self._streamed = False  # whether a piece of a reply came since the last model call
        self._viewers: list[asyncio.Queue] = []

    def append(self, line: str) -> None:
        """An evidence line, from the recorder, in any thread. A viewer must
        never fail a run, so the feed keeps its own errors."""
        try:
            self._event(line)
        except Exception:  # such as a closed event loop
            pass

    def _event(self, line: str) -> None:
        event = EvidenceEvent.from_line(line)
        self.run_id = self.run_id or event.run_id
        if event.type in ("run_completed", "run_failed"):
            self.ending = _status(event)
        item = shown_at(event, self.level)
        if item is None:
            return
        # At bodies, the item holds the payload's name, and follow reads the
        # payload as it hands the item on (see _full).
        if self.level == "bodies" and event.payload:
            item[_PAYLOAD] = event.payload
        self.loop.call_soon_threadsafe(self._push, item)
        if self.level == "bodies" and event.type in ("model_called", "step_failed"):
            # A reply that did not stream comes whole, as replay gives it. Its
            # text is read here only to see if it has any.
            if event.payload and not self._streamed and answer_of(payload_of(_Payloads(self.writer), event.payload)):
                self.loop.call_soon_threadsafe(self._push, {"type": "text", _ANSWER: event.payload})
            self._streamed = False

    def text(self, piece: str) -> None:
        """A piece of a streamed reply, which only the bodies level shows."""
        if self.level == "bodies" and piece:
            self._streamed = True
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
                yield await self._full(item)
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
                yield await self._full(item)
                last = item["n"]
                if item["type"] == "done":
                    return
        finally:
            if queue in self._viewers:
                self._viewers.remove(queue)


    async def _full(self, item: dict) -> dict:
        """The item as a viewer gets it: at bodies, with its payload, or with
        the text of its reply, read from the run's evidence off the loop."""
        if _PAYLOAD not in item and _ANSWER not in item:
            return item
        return await asyncio.to_thread(_read, item, _Payloads(self.writer))


_PAYLOAD, _ANSWER = "_payload", "_answer"  # the payload names that _full reads


def _read(item: dict, record: _Payloads) -> dict:
    out = {k: v for k, v in item.items() if k not in (_PAYLOAD, _ANSWER)}
    if _PAYLOAD in item:
        out["payload"] = payload_of(record, item[_PAYLOAD])
    if _ANSWER in item:
        out["text"] = answer_of(payload_of(record, item[_ANSWER])) or ""
    return out


def _status(ending: EvidenceEvent) -> str:
    # As provevi's replay reads the end of a run: a run that provrun cancelled
    # ends with run_failed, whose error is CancelledError.
    if ending.type == "run_completed":
        return "completed"
    error = ending.data.get("error")
    return "cancelled" if isinstance(error, str) and error.startswith("CancelledError") else "failed"


class _Payloads:
    """The payloads of a run that is going, read as shown_at reads a sealed
    run's. The recorder writes a payload before the event that names it."""

    def __init__(self, writer: Any):
        self.writer = writer

    def payload_bytes(self, name: str) -> bytes | None:
        try:
            return self.writer.get_payload(name)
        except Exception:  # no writer yet, or a store that cannot read back
            return None


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
        # The evidence's end decides the status, as it does for replay, so a
        # run cancelled while it was sealed shows as it was sealed.
        if t.cancelled():
            status = feed.ending or "cancelled"
        elif t.exception() is not None:
            status = feed.ending or "failed"
        else:
            status = feed.ending or t.result().status
        feed.end({"type": "done", "status": status, "run_id": feed.run_id})

    task.add_done_callback(ended)
    return LiveRun(task, feed)

