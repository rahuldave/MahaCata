"""Resolution: from a granted URI to a read-only snapshot.

``ProvcatResolver`` pins the URI through provcat, fetches that version and
copies it into a snapshot that no later fetch changes. It writes
``context_resolved`` and ``snapshot_materialized``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from provcat import Catalog

    from .access import Grant
    from .context import RunContext


@dataclass(frozen=True)
class Snapshot:
    uri: str  # as asked for
    pinned: str  # the physical URI at a fixed version, where the store has versions
    path: Path  # a read-only file or folder
    digest: str  # SHA-256 of the file, or of the folder as provcat.cache.tree_digest defines it
    kind: str  # "file" or "folder"


class Resolver(ABC):
    @abstractmethod
    def snapshot(self, ctx: RunContext, grant: Grant) -> Snapshot: ...


class ProvcatResolver(Resolver):
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def snapshot(self, ctx: RunContext, grant: Grant) -> Snapshot:
        if not grant.granted:
            raise ValueError(f"{grant.uri}: the grant is a deny")
        name = self.__class__.__name__
        with ctx.recorder.step("context_resolved", name, refs=[grant.uri],
                               data={"grant_id": grant.id}) as step:
            physical = self.catalog.resolve(grant.uri).physical
            step.data["physical"] = str(physical)
        with ctx.recorder.step("snapshot_materialized", name, refs=[grant.uri]) as step:
            snap = self.catalog.snapshot(grant.uri)
            step.refs.append(str(snap.pinned))
            step.data.update(pinned=str(snap.pinned), sha256=snap.digest, kind=snap.kind)
        return Snapshot(grant.uri, str(snap.pinned), snap.path, snap.digest, snap.kind)
