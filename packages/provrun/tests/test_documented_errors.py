"""The errors that run_agent's docstring says it raises, not holds in the outcome, and the
RunContext constructor that its Reference page shows."""
import asyncio
import inspect

import pytest
from provevi import FileRunWriter

from provrun import RunContext
from provrun.backends.litellm import LiteLLMBackend
from provrun.evidence import EvidenceError, read_events
from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec


def agent(docs):
    return AgentSpec(name="q", backend="litellm", model="m", prompt="p", inputs=[InputSpec(f"file://{docs}/report.txt")])


def go(spec, catalog, runs, completion, **kw):
    return asyncio.run(run_agent(spec, catalog=catalog, runs_root=runs, org="acme", domain="benefits", env="dev",
                                 actor="t", backend=LiteLLMBackend(completion), **kw))


async def answer(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="42"), finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def test_evidence_that_cannot_be_sealed_is_raised_even_in_lenient_mode(catalog, runs, docs, monkeypatch):
    def seal(self, manifest):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(FileRunWriter, "seal", seal)
    with pytest.raises(EvidenceError, match="cannot seal the evidence"):
        go(agent(docs), catalog, runs, answer, mode="lenient")


class Stop(BaseException):
    """A BaseException that is not an Exception, as KeyboardInterrupt is."""


def test_a_base_exception_is_raised_once_the_run_is_sealed_with_run_failed(catalog, runs, docs):
    async def stop(**kw):
        raise Stop("stopped")

    with pytest.raises(Stop):
        go(agent(docs), catalog, runs, stop)
    [folder] = list(runs.iterdir())
    events = read_events(folder / "evidence")
    assert events[-1].type == "run_failed" and "Stop: stopped" in events[-1].data["error"]
    assert (folder / "evidence" / "manifest.json").exists()


def test_the_run_context_constructor_has_no_private_field():
    assert "_tokens" not in inspect.signature(RunContext).parameters


def test_a_keyboard_interrupt_under_asyncio_run_seals_the_run_as_a_cancel(catalog, runs, docs):
    async def interrupt(**kw):
        raise KeyboardInterrupt("stopped")

    with pytest.raises(KeyboardInterrupt):
        go(agent(docs), catalog, runs, interrupt)
    [folder] = list(runs.iterdir())
    events = read_events(folder / "evidence")
    assert events[-1].type == "run_failed" and events[-1].data["error"].startswith("CancelledError")


def test_a_system_exit_raised_in_the_agent_seals_the_run_as_a_cancel(catalog, runs, docs):
    async def leave(**kw):
        raise SystemExit(3)

    with pytest.raises(SystemExit):
        go(agent(docs), catalog, runs, leave)
    [folder] = list(runs.iterdir())
    events = read_events(folder / "evidence")
    assert events[-1].type == "run_failed" and events[-1].data["error"].startswith("CancelledError")


def test_an_error_while_the_run_opens_is_raised_and_leaves_its_folder(catalog, runs, docs):
    from provevi import FileStore

    class Store(FileStore):
        def open_run(self, run_id, folder):
            raise OSError("cannot open the store")

    with pytest.raises(OSError, match="cannot open the store"):
        go(agent(docs), catalog, runs, answer, store=Store())
    [folder] = list(runs.iterdir())
    assert not (folder / "evidence" / "events.jsonl").exists()


def test_a_cancel_while_the_broker_decides_the_experiment_leaves_no_run(catalog, runs, docs):
    import threading
    import time

    from provrun.access import StubBroker

    deciding = threading.Event()

    class Slow(StubBroker):
        def decide_early(self, scope, uri, mode, purpose=""):
            deciding.set()
            time.sleep(0.3)
            return super().decide_early(scope, uri, mode, purpose)

    exp = "experiment://acme/benefits/dev/plans/q"
    broker = Slow([f"file://{docs}/report.txt", exp], physical_schemes=catalog.stores.keys())

    async def run():
        task = asyncio.create_task(run_agent(agent(docs), catalog=catalog, runs_root=runs, org="acme",
                                             domain="benefits", env="dev", actor="t", broker=broker,
                                             backend=LiteLLMBackend(answer), experiment=exp))
        while not deciding.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not runs.exists() or not list(runs.iterdir())
