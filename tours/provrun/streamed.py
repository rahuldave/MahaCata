"""Stream a real PydanticAI agent that carries provrun's evidence capability,
and print the model_called and tool_called events it wrote.
    streamed.py FOLDER
FOLDER must be new or empty, as a second run there would add to the first.
PROVRUN_TOUR_MODEL names another model, such as test, which needs no key."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # before PydanticAI loads

from pydantic_ai import Agent  # noqa: E402

from provrun.backends.pydantic_ai import evidence_capability  # noqa: E402
from provrun.evidence import EvidenceRecorder, read_events  # noqa: E402

folder = Path(sys.argv[1])
if folder.exists() and any(folder.iterdir()):
    sys.exit(f"streamed.py: {folder} is not empty")
ctx = SimpleNamespace(recorder=EvidenceRecorder("streamed", "you@example.com", folder))


def revenue(year: int) -> str:
    """The revenue of a year."""
    return "42 million" if year == 2025 else "unknown"


model = os.environ.get("PROVRUN_TOUR_MODEL", "anthropic:claude-haiku-5-5")
agent = Agent(model, tools=[revenue], capabilities=[evidence_capability(ctx)],
              instructions="Use the revenue tool. Answer in one short sentence.")


async def main() -> None:
    async with agent.run_stream("What was revenue in 2025?", model_settings={"max_tokens": 200}) as run:
        async for text in run.stream_text(delta=True):
            print(text, end="", flush=True)
    print()


try:
    asyncio.run(main())
finally:
    ctx.recorder.close()
for e in read_events(folder):
    if e.type == "model_called":
        d = e.data
        print(e.type, d["model"], "streamed" if d["streamed"] else "whole",
              d["input_tokens"], d["output_tokens"], d["finish_reason"])
    elif e.type == "tool_called":
        print(e.type, e.data["tool"])
