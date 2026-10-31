"""OpenTelemetry spans for evidence steps.

The span names and attributes come from the OpenTelemetry semantic
conventions for generative AI, as they stood in release 1.37.0 of the
semantic conventions. Those conventions have the status Development, and they
have moved to their own repository, open-telemetry/semantic-conventions-genai,
which has no release and no schema URL yet. So provrun claims no schema URL,
and keeps the names here, in one place, to change them together. When no
tracer provider is set, the spans record nothing and cost little.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator

from opentelemetry import trace

# The release of the semantic conventions whose GenAI names provrun uses. It is
# a note for readers, not a schema URL: the GenAI names have no release now.
NAMES_FROM = "1.37.0"

_tracer = trace.get_tracer("provrun")

# The GenAI operation for each evidence step that has one.
_OPERATION = {"model_called": "chat", "tool_called": "execute_tool", "sandbox_started": "invoke_agent"}
# Step data copied onto the span, with the attribute name of the conventions.
_ATTRIBUTES = {
    "model": "gen_ai.request.model",
    "tool": "gen_ai.tool.name",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "agent": "gen_ai.agent.name",
}


@contextlib.contextmanager
def span(step: str, run_id: str, component: str) -> Iterator[trace.Span]:
    attributes = {"provrun.run_id": run_id, "provrun.step": step, "provrun.component": component}
    if step in _OPERATION:
        attributes["gen_ai.operation.name"] = _OPERATION[step]
    with _tracer.start_as_current_span(step, attributes=attributes) as s:
        yield s


def annotate(s: trace.Span, data: dict) -> None:
    for key, name in _ATTRIBUTES.items():
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)):
            s.set_attribute(name, value)


def ids(s: trace.Span) -> dict:
    """The trace and span IDs, so that an event links to its span; empty when
    nothing records spans."""
    ctx = s.get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
