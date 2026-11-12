"""grep_file: search one file in the sandbox with ripgrep."""
from __future__ import annotations

import asyncio
import shutil

from pydantic_ai import RunContext

from . import RunDeps

__all__ = ["grep_file"]


async def grep_file(
    ctx: RunContext[RunDeps],  # PydanticAI's run context; ctx.deps holds the sandbox
    path: str,  # The path of the file in the sandbox, for example /input/report.txt
    pattern: str,  # The text to find; a literal unless regex is true
    context_lines: int = 2,  # The number of lines to show before and after each match
    regex: bool = False,  # Treat the pattern as a regular expression
) -> str:  # The matching lines with their numbers, or a sentence that says why there are none
    """Search a file for lines that match a pattern, without case.

    Args:
        path: The path of the file in the sandbox, for example /input/report.txt.
        pattern: The text to find. It is a literal unless regex is true.
        context_lines: The number of lines to show before and after each match.
        regex: Treat the pattern as a regular expression.
    """
    sandbox = ctx.deps.sandbox
    if not sandbox.can_read(path):
        return f"Cannot read {path}"
    resolved = sandbox.resolve(path)
    if not resolved.is_file():
        return f"File not found: {path}"
    rg = shutil.which("rg")
    if rg is None:
        return "ripgrep (rg) is not installed."
    cmd = [rg, "--no-filename", "--line-number", "--ignore-case", f"--context={int(context_lines)}"]
    if not regex:
        cmd.append("--fixed-strings")
    cmd += ["--", pattern, str(resolved)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 10)
    except TimeoutError:
        proc.kill()
        return f"Search timed out for {pattern!r} in {path}"
    if proc.returncode == 1:
        return f"No matches for {pattern!r} in {path}"
    if proc.returncode != 0:
        return f"Search error: {err.decode(errors='replace').strip()}"
    return out.decode(errors="replace").rstrip("\n")
