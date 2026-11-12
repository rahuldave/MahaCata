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

__all__ = ["ProvcatResolver", "Resolver", "Snapshot"]


@dataclass(frozen=True)
class Snapshot:
    """One granted input, copied where no later fetch changes it."""

    uri: str  # The URI as asked for
    pinned: str  # The physical URI at a fixed version, where the store has versions
    path: Path  # A read-only file or folder
    digest: str  # The SHA-256 of the file, or of the folder as provcat.cache.tree_digest defines it
    kind: str  # "file" or "folder"


class Resolver(ABC):
    """Turns a grant into a read-only snapshot of what it grants."""

    @abstractmethod
    def snapshot(
        self,
        ctx: RunContext,  # The run that asks; its recorder takes the evidence
        grant: Grant,  # The broker's grant for one input
    ) -> Snapshot:  # The snapshot of the granted URI
        "Make the snapshot of one granted input, and write its evidence."


class ProvcatResolver(Resolver):
    """Pins each granted URI through provcat and snapshots that version."""

    def __init__(
        self,
        catalog: Catalog,  # The catalog that resolves, pins and fetches each URI
    ):
        self.catalog = catalog

    def snapshot(
        self,
        ctx: RunContext,  # The run that asks; its recorder takes context_resolved and snapshot_materialized
        grant: Grant,  # The broker's grant; a deny raises ValueError
    ) -> Snapshot:  # The pinned, read-only snapshot, with its SHA-256 and kind
        "Resolve the granted URI, then snapshot it through the catalog at a pinned version."
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
