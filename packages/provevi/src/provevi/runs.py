"""Where runs live, and how to find one.

A run keeps its files in one folder under a runs root::

    <runs root>/<run_id>/evidence/   the evidence
    <runs root>/<run_id>/output/     what the agent wrote
    <runs root>/<run_id>/bundle.json where the bundle was committed, if anywhere

The runs root is PROVRUN_RUNS, or else ``$XDG_DATA_HOME/provrun/runs``, or
else ``~/.local/share/provrun/runs``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import RunNotFound

__all__ = ["default_runs_root", "find_run", "list_runs"]


def default_runs_root() -> Path:  # PROVRUN_RUNS, or `$XDG_DATA_HOME/provrun/runs`, or `~/.local/share/provrun/runs`
    """The folder that holds the runs, from the environment; it need not exist."""
    env = os.environ.get("PROVRUN_RUNS")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "provrun" / "runs"


def find_run(
    run: str | Path,  # A run ID under root, or else a path to a run folder
    root: str | Path | None = None,  # The runs root; `None` for the default runs root
    *,
    ids_only: bool = False,  # Accept a run ID only, so a name from a user cannot reach another folder
) -> Path:  # The run's folder, as an absolute path
    """Find the folder of a run.

    A run ID that names a link under root is refused. Raises RunNotFound."""
    text = str(run)
    root = Path(root) if root is not None else default_runs_root()
    if text and text not in (".", "..") and not re.search(r"[/\\]", text):
        p = root / text
        if (p / "evidence").is_dir():
            if p.is_symlink():  # a link would verify another run under this ID
                raise RunNotFound(f"{p} is a link, not a run folder")
            return p.resolve()
    elif ids_only:
        raise RunNotFound(f"{text!r} is not a run ID")
    p = Path(run)
    if not ids_only and text and p.is_dir() and (p / "evidence").is_dir():
        return p.resolve()
    raise RunNotFound(f"no run {text!r} under {root}")


def list_runs(
    root: str | Path | None = None,  # The runs root; `None` for the default runs root
) -> list[str]:  # The run IDs; empty when root is no folder
    """List the run IDs under a runs root, oldest first.

    A run ID starts with its UTC time. Links are left out, as find_run refuses them."""
    root = Path(root) if root is not None else default_runs_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if not p.is_symlink() and (p / "evidence").is_dir())
