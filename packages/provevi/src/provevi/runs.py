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


def default_runs_root() -> Path:
    env = os.environ.get("PROVRUN_RUNS")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "provrun" / "runs"


def find_run(run: str | Path, root: str | Path | None = None, *, ids_only: bool = False) -> Path:
    """The folder of a run, as an absolute path. run is a run ID under root
    (the default runs root when None), or else a path to a run folder. With
    ids_only, run must be a run ID, so a name from a user cannot reach
    another folder. A run ID that names a link under root is refused.
    Raises RunNotFound."""
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


def list_runs(root: str | Path | None = None) -> list[str]:
    """The run IDs under root, oldest first. A run ID starts with its UTC time.
    Links are left out, as find_run refuses them."""
    root = Path(root) if root is not None else default_runs_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if not p.is_symlink() and (p / "evidence").is_dir())
