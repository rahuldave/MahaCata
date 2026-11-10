"""A real model, through LiteLLM: the token counts of a streamed reply are
the ones that the provider reports, and match those of a reply in one piece."""
import asyncio
import os

import pytest

from provrun.runner import run_agent
from provrun.spec import AgentSpec, InputSpec

MODEL = os.environ.get("PROVRUN_SYSTEM_MODEL")  # a LiteLLM model name, such as anthropic/claude-haiku-5-5

pytestmark = [pytest.mark.system,
              pytest.mark.skipif(not MODEL, reason="set PROVRUN_SYSTEM_MODEL, and its provider's key, to call a model")]


def test_a_streamed_reply_counts_the_tokens_that_the_provider_reports(catalog, runs, docs, monkeypatch):
    import litellm

    real, reported = litellm.acompletion, []

    async def spy(**kw):
        reply = await real(**kw)
        if not kw.get("stream"):
            reported.append((False, reply.usage.prompt_tokens, reply.usage.completion_tokens))
            return reply

        async def chunks():
            async for chunk in reply:
                usage = getattr(chunk, "usage", None)
                if usage:
                    reported.append((True, usage.prompt_tokens, usage.completion_tokens))
                yield chunk
        return chunks()

    monkeypatch.setattr(litellm, "acompletion", spy)
    tokens = {}
    for stream in (True, False):
        agent = AgentSpec(name="q", backend="litellm", model=MODEL, stream=stream, prompt="Reply with the word yes.",
                          inputs=[InputSpec(f"file://{docs}/report.txt")])
        out = asyncio.run(run_agent(agent, catalog=catalog, runs_root=runs, org="acme", domain="r", env="dev",
                                    actor="t"))
        assert out.status == "completed", out.error
        tokens[stream] = out.result.usage
    print("reported by LiteLLM:", reported)  # with -s, to compare a count that looks wrong
    last = {stream: (i, o) for stream, i, o in reported}
    assert tokens[True] == {"input_tokens": last[True][0], "output_tokens": last[True][1]}
    assert tokens[False] == {"input_tokens": last[False][0], "output_tokens": last[False][1]}
    assert tokens[True]["input_tokens"] == tokens[False]["input_tokens"] > 0
    assert tokens[True]["output_tokens"] > 0
