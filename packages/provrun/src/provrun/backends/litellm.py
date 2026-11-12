"""One model call through LiteLLM, with the inputs in the message.

A text input goes into the user message as text. A PDF or an image goes in as
a content block, as a base64 data URI. A folder is refused, and so is a tool:
an agent that reads folders or calls tools needs the PydanticAI backend.

The answer is written to /output/response.txt, so every backend leaves its
output in the same place.

LiteLLM's OpenTelemetry loggers ("otel", and others built on it, such as
"logfire", "arize" and "langfuse_otel") make no span of their own when a span
is active. They write their attributes, prompts included, and an OK status
onto the active span, which is provrun's step, often after that span ended.
So before each call, provrun sets USE_OTEL_LITELLM_REQUEST_SPAN=true, and
those loggers make their own span. The setting does nothing when no such
logger is on. It stays set for the rest of the process, because LiteLLM's
loggers run after the call returns. A value that someone set already is
kept, with a warning if it is not "true".
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import warnings
from typing import TYPE_CHECKING, Any

from .. import telemetry
from . import InProcessBackend, Pieces, RunResult

if TYPE_CHECKING:
    from ..context import RunContext
    from ..spec import AgentSpec, RunSpec, StagedInput

__all__ = ["LiteLLMBackend"]

# Model settings that are passed on to litellm.acompletion.
SETTINGS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "stop",
    "presence_penalty", "frequency_penalty", "reasoning_effort", "response_format", "thinking",
    "mock_response",  # litellm answers with this text and calls no model; for examples and tests
})
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/yaml")


async def _streamed(ctx: RunContext, call, agent: AgentSpec, messages: list,
                    pieces: Pieces) -> tuple[str, Any, Any]:
    """Ask for the reply in pieces, hand each piece to the run as it comes,
    and give back the whole text, the usage and why the model stopped. The
    usage comes in the last chunk."""
    stream = await call(model=agent.model, messages=messages, stream=True,
                        stream_options={"include_usage": True}, **agent.settings)
    usage, finish = None, None
    async for chunk in stream:
        usage = getattr(chunk, "usage", None) or usage
        for choice in getattr(chunk, "choices", None) or []:
            piece = getattr(getattr(choice, "delta", None), "content", None)
            if piece:
                pieces.add(piece)
                ctx.text(piece)
            finish = getattr(choice, "finish_reason", None) or finish
    return "".join(pieces.text), usage, finish


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


OTEL_SPAN_SETTING = "USE_OTEL_LITELLM_REQUEST_SPAN"


def guard_otel_span() -> None:
    """Make LiteLLM's OpenTelemetry loggers write their own span, not provrun's."""
    value = os.environ.setdefault(OTEL_SPAN_SETTING, "true")
    if value.lower() != "true":
        warnings.warn(f"{OTEL_SPAN_SETTING}={value}: an OpenTelemetry logger of LiteLLM will write "
                      f"onto provrun's model_called span", stacklevel=2)


class LiteLLMBackend(InProcessBackend):
    """One model call through litellm.acompletion, with the inputs in the message.

    It needs provrun[litellm]."""

    name = "litellm"

    def __init__(
        self,
        completion: Any = None,  # An async function like litellm.acompletion to call instead, for tests; None is LiteLLM's
    ):
        self._completion = completion  # for tests: an async function like litellm.acompletion

    def _acompletion(self):
        if self._completion is not None:
            return self._completion
        try:
            import litellm
        except ImportError as e:
            raise ImportError("the litellm backend needs litellm: install provrun[litellm]") from e
        # LiteLLM prints notes such as its provider list on stdout, which is the
        # caller's: provr prints its JSON there.
        litellm.suppress_debug_info = True
        return litellm.acompletion

    async def execute(
        self,
        ctx: RunContext,  # The run; its recorder takes prompt_rendered and model_called
        agent: AgentSpec,  # The agent to run
        run: RunSpec,  # The staged inputs, each a file, and the output folder
    ) -> RunResult:  # The reply's text and its token counts
        """Call the model once with the inputs in the message, and write the reply to response.txt.

        The call is streamed if the spec asks. Raises ValueError for a tool, a
        model setting not in SETTINGS, or a folder input."""
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
        guard_otel_span()
        telemetry.recheck(getattr(ctx, "telemetry", None))  # LiteLLM may have been loaded just now, with callbacks set
        with ctx.recorder.step("model_called", self.__class__.__name__,
                               data={"model": agent.model, "settings": agent.settings,
                                     "streamed": agent.stream}) as step:
            pieces = Pieces()
            try:
                if agent.stream:
                    try:
                        text, usage, finish = await _streamed(ctx, call, agent, messages, pieces)
                    except BaseException:
                        pieces.failed(step)
                        raise
                    pieces.record(step.data)
                else:
                    response = await call(model=agent.model, messages=messages, **agent.settings)
                    text = response.choices[0].message.content or ""
                    usage = getattr(response, "usage", None)
                    finish = getattr(response.choices[0], "finish_reason", None)
            finally:
                telemetry.recheck(getattr(ctx, "telemetry", None), fail=False)  # a call can leave a callback in the lists
            if getattr(ctx, "telemetry", None) is not None:
                getattr(ctx, "telemetry", None).fail_if_found()
            tokens = {"input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                      "output_tokens": getattr(usage, "completion_tokens", 0) or 0}
            step.data.update(tokens, finish_reason=finish)
            step.payload = json.dumps({"request": shown, "response": text}, ensure_ascii=False)
        (run.output_dir / "response.txt").write_text(text, encoding="utf-8")
        return RunResult(text, 0.0, usage=tokens)
