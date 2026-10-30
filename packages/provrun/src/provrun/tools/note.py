"""add_note: let the agent add a note or a citation to the evidence."""
from __future__ import annotations

from pydantic_ai import RunContext

from . import RunDeps


async def add_note(ctx: RunContext[RunDeps], text: str, refs: list[str] | None = None) -> str:
    """Add a note to the record of this run, for example a reason or a citation.

    Args:
        text: The note.
        refs: Paths or URIs that the note is about, for example /input/report.txt.
    """
    ctx.deps.run.recorder.emit("note_added", "agent", refs=refs or [], data={"text": text})
    return "noted"
