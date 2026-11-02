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
def span(step: str, run_id: str, component: str, parent: trace.Span | None = None) -> Iterator[trace.Span]:
    """A span for one step. It is a child of ``parent`` when one is given, else
    of the current span."""
    attributes = {"provrun.run_id": run_id, "provrun.step": step, "provrun.component": component}
    if step in _OPERATION:
        attributes["gen_ai.operation.name"] = _OPERATION[step]
    context = trace.set_span_in_context(parent) if parent is not None else None
    with _tracer.start_as_current_span(step, context=context, attributes=attributes) as s:
        yield s


def annotate(s: trace.Span, data: dict) -> None:
    for key, name in _ATTRIBUTES.items():
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)):
            s.set_attribute(name, value)


def current() -> trace.Span:
    """The current span, to pass as ``parent`` to a later span."""
    return trace.get_current_span()


def ids(s: trace.Span) -> dict:
    """The trace, span and parent span IDs, so that an event links to its span;
    empty when nothing records the span. The parent span ID is None at the root."""
    ctx = s.get_span_context()
    # A span that records nothing, such as one the sampler dropped, or the
    # caller's own span handed back when no provider is set, has no parent to
    # tell; its IDs would name a span that is not this step's.
    if not ctx.is_valid or not s.is_recording():
        return {}
    parent = s.parent
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
            "parent_span_id": format(parent.span_id, "016x") if parent is not None and parent.is_valid else None}
