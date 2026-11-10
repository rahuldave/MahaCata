"""Access decisions.

The broker decides whether a run may use one URI in one mode, and returns a
grant. A grant names the decision, not a credential. Later brokers will read
profiles and identities; ``StubBroker`` checks an allow list and the
environment, and grants only reads: a read-only snapshot of an input, and a
read of one version of the run's experiment.

The modes:

- ``mounted_snapshot``: a read-only snapshot of an input, staged for the agent.
- ``pinned_read``: provrun reads one version of a catalog object itself, as it
  reads the run's experiment. The agent never sees it.
- ``mounted_readwrite``, ``retrieval`` and ``api``: kept for later brokers;
  the stub grants none of them.

A run asks for its experiment before its run folder is made, so the broker
decides it with a ``RunScope``, which has the run's ID, org, domain,
environment and actor, and the run records the grant once it has opened.

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
  grants one only in the dev environment. A scheme is physical if it names a
  store of the catalog, including one that the configuration adds, such as
  ``lakefs-acme`` in the example configuration.
"""
from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from provcat import parse

from .errors import ProvrunError

if TYPE_CHECKING:
    from .context import RunContext

MODES = ("mounted_snapshot", "pinned_read", "mounted_readwrite", "retrieval", "api")
GRANTED_BY_STUB = ("mounted_snapshot", "pinned_read")


class AccessDenied(ProvrunError):
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


@dataclass(frozen=True)
class RunScope:
    """Who asks, for a request decided before the run opens: the fields of a
    RunContext that a broker reads."""
    run_id: str
    org: str
    domain: str
    env: str
    actor: str


class Broker(ABC):
    def request(self, ctx: RunContext, uri: str, mode: str, purpose: str = "") -> Grant:
        """Decide one request and write its evidence. Raises AccessDenied on a deny."""
        _check_mode(mode)
        name = self.__class__.__name__
        ctx.recorder.emit("access_requested", name, refs=[uri], data={"mode": mode, "purpose": purpose})
        grant = self.decide(ctx, uri, mode, purpose)
        ctx.recorder.emit("access_granted" if grant.granted else "access_denied", name,
                          refs=[uri], data=grant.as_dict())
        if not grant.granted:
            raise AccessDenied(grant)
        return grant

    def decide_early(self, scope: RunScope, uri: str, mode: str, purpose: str = "") -> Grant:
        """Decide one request before the run opens, and write nothing. Raises
        AccessDenied on a deny. The run writes the grant with ``record``. It
        calls decide with scope, so a broker's decide must read only the
        fields that a RunScope has; and it does not go through request, so a
        broker that changes request must change these two methods too."""
        _check_mode(mode)
        grant = self.decide(scope, uri, mode, purpose)  # type: ignore[arg-type]  # a RunScope has the fields read
        if not grant.granted:
            raise AccessDenied(grant)
        expiry(grant)  # a time that does not parse fails here, before the run opens
        return grant

    def record(self, ctx: RunContext, grant: Grant, purpose: str = "") -> None:
        """Write the evidence of a decision made before the run opened: a
        grant that decide_early gave, or the deny in the AccessDenied that it
        raised. A deny, and a grant that has expired since, are written as
        access_denied, and raise AccessDenied."""
        name = self.__class__.__name__
        ctx.recorder.emit("access_requested", name, refs=[grant.uri], data={"mode": grant.mode, "purpose": purpose})
        if not grant.granted:
            ctx.recorder.emit("access_denied", name, refs=[grant.uri], data=grant.as_dict())
            raise AccessDenied(grant)
        ends = expiry(grant)
        if ends is not None and ends <= datetime.now(UTC):
            grant = replace(grant, decision="deny", reason=f"the grant expired at {grant.expires_at}, "
                                                          "before the run opened")
            ctx.recorder.emit("access_denied", name, refs=[grant.uri], data=grant.as_dict())
            raise AccessDenied(grant)
        ctx.recorder.emit("access_granted", name, refs=[grant.uri], data=grant.as_dict())

    @abstractmethod
    def decide(self, ctx: RunContext, uri: str, mode: str, purpose: str) -> Grant:
        """The decision for one request. ctx is the run's context, or a
        RunScope for a request decided before the run opens."""


def expiry(grant: Grant) -> datetime | None:
    """When grant expires, or None if it does not. A time with no time zone
    is in UTC. Raises ValueError for one that is not an ISO 8601 time."""
    if grant.expires_at is None:
        return None
    try:
        ends = datetime.fromisoformat(grant.expires_at)
    except (TypeError, ValueError):
        raise ValueError(f"the grant's expires_at {grant.expires_at!r} is not an ISO 8601 time") from None
    return ends if ends.tzinfo is not None else ends.replace(tzinfo=UTC)


def _check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"access mode must be one of {MODES}, not {mode!r}")


class StubBroker(Broker):
    def __init__(self, allow: Iterable[str], *, physical_schemes: Iterable[str],
                 ttl: timedelta = timedelta(hours=1)):
        # physical_schemes has no default: it must be the stores of the catalog, for
        # example catalog.stores.keys(), so that a configured store such as local-s3
        # counts as physical and not as a logical scheme.
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
        if mode not in GRANTED_BY_STUB:
            return answer(False, f"the stub broker grants only {' and '.join(GRANTED_BY_STUB)}, not {mode}")
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
