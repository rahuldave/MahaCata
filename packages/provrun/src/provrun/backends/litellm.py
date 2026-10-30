"""One model call through LiteLLM, with the inputs in the message.

A text input goes into the user message as text. A PDF or an image goes in as
a content block, as a base64 data URI. A folder is refused, and so is a tool:
an agent that reads folders or calls tools needs the PydanticAI backend.

The answer is written to /output/response.txt, so every backend leaves its
output in the same place.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from typing import TYPE_CHECKING, Any

from . import InProcessBackend, RunResult

if TYPE_CHECKING:
    from ..context import RunContext
    from ..spec import AgentSpec, RunSpec, StagedInput

# Model settings that are passed on to litellm.acompletion.
SETTINGS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop",
    "presence_penalty", "frequency_penalty", "reasoning_effort", "response_format", "thinking",
    "mock_response",  # litellm answers with this text and calls no model; for examples and tests
})
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/yaml")


def _mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or ("text/markdown" if name.endswith(".md") else "application/octet-stream")


def content_block(i: StagedInput) -> dict:
    """The content block of one binary input."""
    mime = _mime(i.name)
    data = f"data:{mime};base64,{base64.b64encode(i.host_path.read_bytes()).decode()}"
    if mime.startswith("image/"):
        return {"type": "image_url", "image_url": {"url": data}}
    return {"type": "file", "file": {"file_data": data, "filename": i.name}}


def build_messages(agent: AgentSpec, run: RunSpec) -> list[dict]:
    messages: list[dict] = []
    if agent.instructions:
        messages.append({"role": "system", "content": agent.instructions})
    blocks: list[dict] = []
    for i in run.inputs:
        if i.kind != "file":
            raise ValueError(f"input {i.name} is a folder; the litellm backend takes files only")
        if _mime(i.name).startswith(TEXT_TYPES):
            text = i.host_path.read_text(encoding="utf-8")
            blocks.append({"type": "text", "text": f"[Document: {i.agent_path}]\n{text}"})
        else:
            blocks.append(content_block(i))
    if not blocks:
        messages.append({"role": "user", "content": agent.prompt})
    else:
        messages.append({"role": "user", "content": [*blocks, {"type": "text", "text": agent.prompt}]})
    return messages


def _redact_data_uris(messages: list[dict]) -> list[dict]:
    """The messages with each data URI replaced by its length, for the payload;
    the inputs themselves are in the evidence through their digests."""
    def fix(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("data:"):
            return f"<data URI, {len(v)} characters>"
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v
    return fix(messages)


class LiteLLMBackend(InProcessBackend):
    name = "litellm"

    def __init__(self, completion=None):
        self._completion = completion  # for tests: an async function like litellm.acompletion

    def _acompletion(self):
        if self._completion is not None:
            return self._completion
        try:
            from litellm import acompletion
        except ImportError as e:
            raise ImportError("the litellm backend needs litellm: install provrun[litellm]") from e
        return acompletion

    async def execute(self, ctx: RunContext, agent: AgentSpec, run: RunSpec) -> RunResult:
        if agent.tools:
            raise ValueError("the litellm backend has no tools; use the pydantic-ai backend")
        unknown = set(agent.settings) - SETTINGS
        if unknown:
            raise ValueError(f"settings that litellm does not take here: {sorted(unknown)}")
        messages = build_messages(agent, run)
        shown = _redact_data_uris(messages)
        ctx.recorder.emit("prompt_rendered", self.__class__.__name__,
                          refs=[i.uri for i in run.inputs], payload=shown,
                          data={"messages": len(messages)})
        call = self._acompletion()
        with ctx.recorder.step("model_called", self.__class__.__name__,
                               data={"model": agent.model, "settings": agent.settings}) as step:
            response = await call(model=agent.model, messages=messages, **agent.settings)
            text = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            tokens = {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                      "output_tokens": getattr(usage, "completion_tokens", 0) or 0}
            step.data.update(tokens, finish_reason=getattr(response.choices[0], "finish_reason", None))
            step.payload = json.dumps({"request": shown, "response": text}, ensure_ascii=False)
        (run.output_dir / "response.txt").write_text(text, encoding="utf-8")
        return RunResult(text, 0.0, usage=tokens)
