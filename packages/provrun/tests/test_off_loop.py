"""The blocking steps of a run go in threads, so a server's event loop goes on."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from provrun.backends.litellm import LiteLLMBackend
from provrun.context import RunContext
from provevi import EvidenceError
from provrun.evidence import read_events, verify
from provrun.resolve import ProvcatResolver
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec


async def acompletion(**kw):
    choice = SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


class SlowResolver(ProvcatResolver):
    """Fetches as provcat does, after a blocking wait, as a slow download would."""

    def __init__(self, catalog, seconds):
        super().__init__(catalog)
        self.seconds = seconds
        self.started = False

    def snapshot(self, ctx, grant):
        self.started = True
        time.sleep(self.seconds)
        return super().snapshot(ctx, grant)


def _agent(docs):
    return AgentSpec(name="q", backend="litellm", model="m", prompt="p", inputs=[InputSpec(f"file://{docs}/report.txt")])


def _start(agent, catalog, runs, **kw):
    return asyncio.create_task(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="b", env="dev",
                                         actor="t", backend=LiteLLMBackend(acompletion), **kw))


def test_the_event_loop_goes_on_while_an_input_is_fetched(catalog, runs, docs):
    async def go():
        task = _start(_agent(docs), catalog, runs, resolver=SlowResolver(catalog, 0.5))
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        return await task, ticks

    out, ticks = asyncio.run(go())
    assert out.status == "completed", out.error
    assert ticks > 20  # a blocked loop would tick once or twice in the half second


def test_a_cancel_during_a_fetch_waits_for_it_and_records_the_run(catalog, runs, docs):
    async def go():
        resolver = SlowResolver(catalog, 0.5)
        task = _start(_agent(docs), catalog, runs, resolver=resolver)
        while not resolver.started:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    [folder] = list(runs.iterdir())
    events = [e.type for e in read_events(folder / "evidence")]
    # The fetch ended before the run was sealed, so its events come first, and the chain holds.
    assert "snapshot_materialized" in events and events[-1] == "run_failed"
    assert verify(folder / "evidence") == []


def test_a_cancel_while_sealing_lets_the_seal_end(catalog, runs, docs, monkeypatch):
    finish = RunContext.finish

    def slow_finish(self, error=None):
        time.sleep(0.5)
        return finish(self, error)

    monkeypatch.setattr(RunContext, "finish", slow_finish)

    async def go():
        task = _start(_agent(docs), catalog, runs)
        def collected():  # the outputs are recorded, so sealing comes next
            folders = list(runs.iterdir()) if runs.exists() else []
            try:  # the run opens in a thread, so its folder can come before its first line
                return folders and "output_artifact_created" in [e.type for e in read_events(folders[0] / "evidence")]
            except EvidenceError:
                return False

        while not collected():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    [folder] = list(runs.iterdir())
    assert read_events(folder / "evidence")[-1].type == "run_completed"
    assert verify(folder / "evidence") == []


def test_the_event_loop_goes_on_while_the_last_spans_are_sent(catalog, runs, docs, monkeypatch):
    from provrun.destinations import Routes

    close, closed = Routes.close, []

    def slow_close(self):
        time.sleep(0.5)  # as a collector that is slow to answer
        close(self)
        closed.append(True)

    monkeypatch.setattr(Routes, "close", slow_close)

    async def go():
        task = _start(_agent(docs), catalog, runs)
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        return await task, ticks

    out, ticks = asyncio.run(go())
    assert out.status == "completed", out.error
    assert closed and ticks > 20


def test_a_cancel_while_a_failed_run_is_sealed_reaches_the_caller(catalog, runs, docs, monkeypatch):
    finish, started = RunContext.finish, []

    def slow_finish(self, error=None):
        started.append(True)
        time.sleep(0.5)
        return finish(self, error)

    monkeypatch.setattr(RunContext, "finish", slow_finish)

    async def down(**kw):
        raise RuntimeError("model down")

    async def go():
        task = asyncio.create_task(run_agent(_agent(docs), catalog=catalog, runs_root=runs, org="acme", domain="b",
                                             env="dev", actor="t", backend=LiteLLMBackend(down)))
        while not started:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # A time limit around a run gives its error too. It runs out once
        # sealing has begun, however long the steps before it take.
        started.clear()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(None) as limit:
                async def run_out():
                    while not started:
                        await asyncio.sleep(0.01)
                    limit.reschedule(asyncio.get_running_loop().time())

                timer = asyncio.create_task(run_out())
                await run_agent(_agent(docs), catalog=catalog, runs_root=runs, org="acme", domain="b",
                                env="dev", actor="t", backend=LiteLLMBackend(down))
        await timer

    asyncio.run(go())
    for folder in runs.iterdir():
        last = read_events(folder / "evidence")[-1]
        assert last.type == "run_failed" and "model down" in last.data["error"]  # sealed with its own error
        assert verify(folder / "evidence") == []


_scanning = []  # set when a slow scan starts


def _slow_scan(monkeypatch, at_end, seconds=0.5):
    """The scan of the libraries, after a blocking wait, at a run's start or at its end."""
    from provrun import telemetry

    scan = telemetry._scan_problems
    _scanning.clear()

    def slow(baseline):
        if (baseline is not None) == at_end:
            _scanning.append(True)
            time.sleep(seconds)
        return scan(baseline)

    monkeypatch.setattr(telemetry, "_scan_problems", slow)


_opening = []  # set when a slow open starts


def _slow_open(monkeypatch, seconds=0.5):
    open_run = RunContext.open
    _opening.clear()

    def slow(**kw):
        _opening.append(True)
        time.sleep(seconds)
        return open_run(**kw)

    monkeypatch.setattr(RunContext, "open", staticmethod(slow))


@pytest.mark.parametrize("step", ["open", "start scan", "end scan"])
def test_the_event_loop_goes_on_while_a_run_opens_and_while_its_libraries_are_scanned(catalog, runs, docs,
                                                                                         monkeypatch, step):
    if step == "open":
        _slow_open(monkeypatch)
    else:
        _slow_scan(monkeypatch, at_end=step == "end scan")

    async def go():
        task = _start(_agent(docs), catalog, runs)
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        return await task, ticks

    out, ticks = asyncio.run(go())
    assert out.status == "completed", out.error
    assert ticks > 20


@pytest.mark.parametrize("step", ["open", "end scan", "end scan of a failed run"])
def test_a_cancel_while_a_run_opens_or_its_libraries_are_scanned_still_seals_it(catalog, runs, docs, monkeypatch,
                                                                                step):
    if step == "open":
        _slow_open(monkeypatch)
    else:
        _slow_scan(monkeypatch, at_end=True)

    async def down(**kw):
        raise RuntimeError("model down")

    async def go():
        backend = LiteLLMBackend(down if step == "end scan of a failed run" else acompletion)
        task = asyncio.create_task(run_agent(_agent(docs), catalog=catalog, runs_root=runs, org="acme", domain="b",
                                             env="dev", actor="t", backend=backend))

        def at_step():
            if step == "open":
                return bool(_opening)
            folders = list(runs.iterdir()) if runs.exists() else []
            try:  # the run opens in a thread, so its folder can come before its first line
                types = [e.type for e in read_events(folders[0] / "evidence")] if folders else []
            except EvidenceError:
                return False
            return "model_called" in types or "step_failed" in types

        while not at_step():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    [folder] = list(runs.iterdir())
    events = read_events(folder / "evidence")
    assert events[0].type == "run_created" and events[-1].type == "run_failed"
    if step == "end scan of a failed run":
        assert "model down" in events[-1].data["error"]  # the run keeps its own error
    assert verify(folder / "evidence") == []


def test_a_cancel_during_the_start_scan_puts_the_environment_back_and_opens_no_run(catalog, runs, docs, monkeypatch):
    import os

    from provrun import telemetry

    _slow_scan(monkeypatch, at_end=False)
    before = {k: os.environ.get(k) for k in telemetry.SETTINGS}

    async def go():
        task = _start(_agent(docs), catalog, runs)
        while not _scanning:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert {k: os.environ.get(k) for k in telemetry.SETTINGS} == before
    assert not telemetry._open
    assert not runs.exists() or not list(runs.iterdir())


def test_a_failed_run_whose_outputs_cannot_be_collected_is_still_sealed(catalog, runs, docs, monkeypatch):
    from provrun.outputs import Outputter

    def broken(self, ctx, run):
        raise OSError("disk gone")

    monkeypatch.setattr(Outputter, "collect", broken)
    out = asyncio.run(_start_and_wait(_agent(docs), catalog, runs))
    assert out.status == "failed" and "disk gone" in str(out.error)
    [folder] = list(runs.iterdir())
    assert read_events(folder / "evidence")[-1].type == "run_failed"
    assert verify(folder / "evidence") == []


async def _start_and_wait(agent, catalog, runs):
    return await _start(agent, catalog, runs)
