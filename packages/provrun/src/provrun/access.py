"""Access decisions.

The broker decides whether a run may use one URI in one mode, and returns a
grant. A grant names the decision, not a credential. Later brokers will read
profiles and identities; ``StubBroker`` checks an allow list and the
environment, and grants only read-only snapshots.

The stub's rules:

- The URI, without its selector, must be in the allow list, or under a folder
  in it. An allow entry that ends with ``/`` is a folder.
- A URI with a selector, such as ``?at=branch:prod``, must be in the allow
  list with that same selector. A selector can name another branch, and in a
  logical scheme the branch is often the environment, so a selector that the
  list does not name could reach another environment.
- A logical URI has the form ``scheme://org/domain/env/...``. Its org and its
  environment must be those of the run.
- A physical URI, such as ``file:///...``, names no environment, so the stub
  grants one only in the dev environment.
"""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from provcat import parse

if TYPE_CHECKING:
    from .context import RunContext

MODES = ("mounted_snapshot", "mounted_readwrite", "retrieval", "api")


class AccessDenied(Exception):
    def __init__(self, grant: Grant):
        super().__init__(f"access to {grant.uri} denied: {grant.reason}")
        self.grant = grant


@dataclass(frozen=True)
class Grant:
    id: str
    uri: str
    mode: str
    decision: str  # "grant" or "deny"
    reason: str
    expires_at: str | None
    policy: str

    @property
    def granted(self) -> bool:
        return self.decision == "grant"

    def as_dict(self) -> dict:
        return {"grant_id": self.id, "mode": self.mode, "decision": self.decision, "reason": self.reason,
                "expires_at": self.expires_at, "policy": self.policy}


class Broker(ABC):
    def request(self, ctx: RunContext, uri: str, mode: str, purpose: str = "") -> Grant:
        """Decide one request and write its evidence. Raises AccessDenied on a deny."""
        if mode not in MODES:
            raise ValueError(f"access mode must be one of {MODES}, not {mode!r}")
        name = self.__class__.__name__
        ctx.recorder.emit("access_requested", name, refs=[uri], data={"mode": mode, "purpose": purpose})
        grant = self.decide(ctx, uri, mode, purpose)
        ctx.recorder.emit("access_granted" if grant.granted else "access_denied", name,
                          refs=[uri], data=grant.as_dict())
        if not grant.granted:
            raise AccessDenied(grant)
        return grant

    @abstractmethod
    def decide(self, ctx: RunContext, uri: str, mode: str, purpose: str) -> Grant: ...


class StubBroker(Broker):
    def __init__(self, allow: Iterable[str], *, physical_schemes: Iterable[str] = ("file", "s3", "github", "gs", "az"),
                 ttl: timedelta = timedelta(hours=1)):
        self.allow = [a for a in allow]
        self.physical = set(physical_schemes)
        self.ttl = ttl

    def _allowed(self, plain: str) -> bool:
        for entry in self.allow:
            entry_plain = str(parse(entry).without_selector()) + ("/" if entry.endswith("/") else "")
            if entry_plain.endswith("/") and plain.startswith(entry_plain):
                return True
            if plain == entry_plain:
                return True
        return False

    def decide(self, ctx: RunContext, uri: str, mode: str, purpose: str) -> Grant:
        def answer(ok: bool, reason: str) -> Grant:
            expires = (datetime.now(UTC) + self.ttl).isoformat(timespec="seconds") if ok else None
            return Grant(f"grant-{secrets.token_hex(6)}", uri, mode, "grant" if ok else "deny",
                         reason, expires, "stub")

        u = parse(uri)
        if mode != "mounted_snapshot":
            return answer(False, f"the stub broker grants only mounted_snapshot, not {mode}")
        if u.selector is not None:
            if not any(parse(entry) == u for entry in self.allow if "?at=" in entry):
                return answer(False, f"the selector ?at={u.selector} is not in the allow list; "
                                     "a selector can name another branch or environment")
        elif not self._allowed(str(u.without_selector())):
            return answer(False, "the URI is not in the allow list of the run")
        if u.scheme in self.physical:
            if ctx.env != "dev":
                return answer(False, f"a physical {u.scheme} URI names no environment; "
                                     f"the stub grants one only in dev, and the run is in {ctx.env}")
            return answer(True, "in the allow list; physical URI in dev")
        if len(u.segments) < 2:
            return answer(False, "a logical URI needs the form scheme://org/domain/env/...")
        if u.authority != ctx.org:
            return answer(False, f"the URI is of org {u.authority}, and the run is of org {ctx.org}")
        if u.segments[1] != ctx.env:
            return answer(False, f"the URI is in environment {u.segments[1]}, and the run is in {ctx.env}")
        return answer(True, "in the allow list; org and environment match")
