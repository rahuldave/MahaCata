"""The outputter: record what a run hands back.

It hashes each file in the output folder and writes one
``output_artifact_created`` event for each, with the URI the file will have in
the bundle. A link or any other special file in the output folder fails the
run, because the bundle holds plain files only.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from provcat.cache import tree_digest

if TYPE_CHECKING:
    from .context import RunContext
    from .spec import RunSpec

__all__ = ["Artifact", "Outputter"]


@dataclass(frozen=True)
class Artifact:
    """One file that a run handed back in its output folder."""

    path: Path  # Where the file is in the run folder
    uri: str  # Its URI in the bundle, <run URI>/output/<path in the output folder>
    sha256: str  # The SHA-256 of the file
    size: int  # Its size in bytes


class Outputter:
    """Records each file that a run leaves in its output folder."""

    def collect(
        self,
        ctx: RunContext,  # The run; its recorder takes one output_artifact_created for each file
        run: RunSpec,  # The run spec, whose output folder is read
    ) -> list[Artifact]:  # One artifact for each file, in path order
        """Hash each file in the output folder and write its event.

        Raises ValueError for a link or any other file that is not plain."""
        artifacts = []
        for f in sorted(run.output_dir.rglob("*")):
            if f.is_symlink() or not (f.is_file() or f.is_dir()):
                raise ValueError(f"{f} is not a plain file; the output folder holds plain files only")
            if not f.is_file():
                continue
            rel = f.relative_to(run.output_dir).as_posix()
            uri = f"{ctx.run_uri}/output/{rel}"
            with ctx.recorder.step("output_artifact_created", self.__class__.__name__, refs=[uri]) as step:
                digest = tree_digest(f)
                size = f.stat().st_size
                step.data.update(path=f"{run.agent_output}/{rel}", sha256=digest, size=size)
            artifacts.append(Artifact(f, uri, digest, size))
        return artifacts
