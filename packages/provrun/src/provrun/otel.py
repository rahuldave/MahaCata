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
from opentelemetry.context import Context

__all__: list[str] = []

# The release of the semantic conventions whose GenAI names provrun uses. It is
# a note for readers, not a schema URL: the GenAI names have no release now.
NAMES_FROM = "1.37.0"

#: The status of a span whose run failed.
ERROR = trace.StatusCode.ERROR

_tracer = trace.get_tracer("provrun")

# The span IDs of the spans that provrun made and that have not ended. Another
# library can name its tracer provrun, so a span is provrun's own only if its
# ID is here; the span router takes each ID out when the span ends.
OURS: set[int] = set()

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


def use(provider: trace.TracerProvider) -> None:
    """Make provrun's spans with provider's tracer, not the global one."""
    global _tracer
    _tracer = provider.get_tracer("provrun")


@contextlib.contextmanager
def span(step: str, run_id: str, component: str, parent: trace.Span | None = None) -> Iterator[trace.Span]:
    """A span for one step. It is a child of ``parent`` when one is given, else
    of the current span."""
    attributes = {"provrun.run_id": run_id, "provrun.step": step, "provrun.component": component}
    if step in _OPERATION:
        attributes["gen_ai.operation.name"] = _OPERATION[step]
    context = trace.set_span_in_context(parent) if parent is not None else None
    with _tracer.start_as_current_span(step, context=context, attributes=attributes) as s:
        if s.is_recording():
            OURS.add(s.get_span_context().span_id)
        yield s


@contextlib.contextmanager
def run(run_id: str, agent: str) -> Iterator[trace.Span]:
    """The root span of one run, current while the run goes on, so that each
    top-level step is its child. It starts a new trace, even inside a span of
    the caller's, so that a run is one trace of its own."""
    attributes = {"provrun.run_id": run_id, "provrun.step": "run", "provrun.component": "runner",
                  "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent}
    with _tracer.start_as_current_span("run", context=Context(), attributes=attributes) as s:
        if s.is_recording():
            OURS.add(s.get_span_context().span_id)
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
