"""Stagers: make granted snapshots visible to the agent.

``InProcessStager`` copies each snapshot into the input folder of the run,
with a copy-on-write clone where the disk has one, and keeps every file
read-only. A clone shares no inode with the snapshot, so nothing a run does to
its inputs reaches the snapshot that other runs use. The PydanticAI backend mounts that folder read-only at /input. The
Docker stager will bind-mount the same folder; a Foundry stager will upload
the files instead, so integrity rests on the digests, which ``verify_inputs``
checks after the run.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from provcat.cache import copy_readonly, tree_digest

from .errors import ProvrunError
from .spec import RunSpec, StagedInput

if TYPE_CHECKING:
    from .context import RunContext
    from .resolve import Snapshot


class InputCheckError(ProvrunError):
    """A staged input changed during the run."""


class Stager(ABC):
    @abstractmethod
    def stage(self, ctx: RunContext, inputs: list[tuple[str, Snapshot]]) -> RunSpec:
        """Stage (name, snapshot) pairs for one run."""

    def verify_inputs(self, ctx: RunContext, run: RunSpec) -> None:
        """Hash each staged input again, write input_verified, and raise
        InputCheckError if one changed."""
        changed = []
        for i in run.inputs:
            with ctx.recorder.step("input_verified", self.__class__.__name__, refs=[i.uri, i.pinned]) as step:
                now = tree_digest(i.host_path) if i.host_path.exists() else None
                step.data.update(name=i.name, sha256=i.digest, unchanged=now == i.digest)
                if now != i.digest:
                    changed.append(i.name)
        if changed:
            raise InputCheckError(f"inputs changed during the run: {changed}")


class InProcessStager(Stager):
    def stage(self, ctx: RunContext, inputs: list[tuple[str, Snapshot]]) -> RunSpec:
        input_dir = ctx.folder / "input"
        names = [name for name, _ in inputs]
        for name in names:
            if names.count(name) > 1 or "/" in name or "\\" in name or name in ("", ".", ".."):
                raise ValueError(f"input name {name!r} is empty, repeated or not a single name")
        staged = []
        for name, snap in inputs:
            with ctx.recorder.step("input_staged", self.__class__.__name__, refs=[snap.uri, snap.pinned]) as step:
                copy_readonly(snap.path, input_dir / name)
                staged_digest = tree_digest(input_dir / name)
                step.data.update(name=name, sha256=staged_digest, kind=snap.kind, agent_path=f"/input/{name}")
                if staged_digest != snap.digest:
                    raise InputCheckError(f"input {name}: the staged copy has SHA-256 {staged_digest}, "
                                          f"and the snapshot was {snap.digest}")
            staged.append(StagedInput(name, snap.uri, snap.pinned, snap.digest, snap.kind,
                                      input_dir / name, f"/input/{name}"))
        input_dir.chmod(0o555)
        return RunSpec(ctx.run_id, staged, input_dir, ctx.folder / "output", ctx.folder / "scratch")
