"""OpenTelemetry spans for evidence steps.

The span names and attributes follow the OpenTelemetry semantic conventions
for generative AI, version 1.37. Those conventions still have the status
Development, so provrun keeps their names here, in one place. When no tracer
provider is set, the spans record nothing and cost little.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator

from opentelemetry import trace

SEMCONV_VERSION = "1.37.0"

_tracer = trace.get_tracer("provrun", schema_url=f"https://opentelemetry.io/schemas/{SEMCONV_VERSION}")

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
