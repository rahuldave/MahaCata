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

__all__ = ["AccessDenied", "Broker", "Grant", "RunScope", "StubBroker"]

MODES = ("mounted_snapshot", "pinned_read", "mounted_readwrite", "retrieval", "api")
GRANTED_BY_STUB = ("mounted_snapshot", "pinned_read")


class AccessDenied(ProvrunError):
    """A broker refused a request. The error holds the deny, as a grant."""

    def __init__(
        self,
        grant: Grant,  # The deny: a grant whose decision is "deny", with its reason
    ):
        super().__init__(f"access to {grant.uri} denied: {grant.reason}")
        self.grant = grant


@dataclass(frozen=True)
class Grant:
    """A broker's decision on one request: a URI in one mode.

    It names the decision, not a credential."""

    id: str  # The grant's ID, such as grant-0a1b2c3d4e5f, which the evidence records
    uri: str  # The URI asked for
    mode: str  # The access mode asked for, one of the modes the module lists
    decision: str  # "grant" or "deny"
    reason: str  # Why the broker decided so
    expires_at: str | None  # When a grant ends, as an ISO 8601 time; None for a deny, or a grant with no end
    policy: str  # The policy that decided, such as "stub"

    @property
    def granted(self) -> bool:
        return self.decision == "grant"

    def as_dict(
        self,
    ) -> dict:  # The grant's ID, mode, decision, reason, expiry and policy, under the names the evidence uses
        "The grant as the data of an access_granted or access_denied event: every field but the URI."
        return {"grant_id": self.id, "mode": self.mode, "decision": self.decision, "reason": self.reason,
                "expires_at": self.expires_at, "policy": self.policy}


@dataclass(frozen=True)
class RunScope:
    """Who asks, for a request decided before the run opens.

    It has the fields of a RunContext that a broker reads."""
    run_id: str  # The ID that the run will have
    org: str  # The run's org
    domain: str  # The run's domain
    env: str  # The run's environment, such as dev or prod
    actor: str  # Who runs it


class Broker(ABC):
    """Decides whether a run may use one URI in one mode.

    A broker defines ``decide``. ``request`` and ``record`` write the
    evidence around it; ``decide_early`` writes nothing. All three raise
    AccessDenied on a deny."""

    def request(
        self,
        ctx: RunContext,  # The run that asks
        uri: str,  # The URI it asks for
        mode: str,  # The access mode, one of the modes the module lists; another raises ValueError
        purpose: str = "",  # Why, such as the agent's name; access_requested records it
    ) -> Grant:  # The grant
        """Decide one request and write its evidence.

        It writes access_requested, then access_granted or access_denied.
        Raises AccessDenied on a deny."""
        _check_mode(mode)
        name = self.__class__.__name__
        ctx.recorder.emit("access_requested", name, refs=[uri], data={"mode": mode, "purpose": purpose})
        grant = self.decide(ctx, uri, mode, purpose)
        ctx.recorder.emit("access_granted" if grant.granted else "access_denied", name,
                          refs=[uri], data=grant.as_dict())
        if not grant.granted:
            raise AccessDenied(grant)
        return grant

    def decide_early(
        self,
        scope: RunScope,  # Who asks, as the run will be once it opens
        uri: str,  # The URI asked for, such as the run's experiment
        mode: str,  # The access mode, one of the modes the module lists; another raises ValueError
        purpose: str = "",  # Why, such as "experiment"
    ) -> Grant:  # The grant
        """Decide one request before the run opens, and write nothing.

        Raises AccessDenied on a deny, and ValueError for a grant whose expiry
        is not an ISO 8601 time. The run writes the grant with ``record``. It
        calls decide with scope, so a broker's decide must read only the
        fields that a RunScope has; and it does not go through request, so a
        broker that changes request must change these two methods too."""
        _check_mode(mode)
        grant = self.decide(scope, uri, mode, purpose)  # type: ignore[arg-type]  # a RunScope has the fields read
        if not grant.granted:
            raise AccessDenied(grant)
        expiry(grant)  # a time that does not parse fails here, before the run opens
        return grant

    def record(
        self,
        ctx: RunContext,  # The run, now open
        grant: Grant,  # The decision that decide_early made: its grant, or the deny in its AccessDenied
        purpose: str = "",  # Why, as given to decide_early; access_requested records it
    ) -> None:  # Nothing
        """Write the evidence of a decision made before the run opened.

        The decision is a grant that decide_early gave, or the deny in the
        AccessDenied that it raised. A deny, and a grant that has expired since, are written as
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
    def decide(
        self,
        ctx: RunContext | RunScope,  # The run's context, or a RunScope before the run opens
        uri: str,  # The URI asked for
        mode: str,  # The access mode asked for
        purpose: str,  # Why, as the caller gave it
    ) -> Grant:  # The decision, a grant or a deny; it writes nothing and raises nothing for a deny
        """Make the decision for one request, and write nothing."""


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
    """A broker that checks an allow list and the environment.

    It follows the rules the module lists, and grants only mounted_snapshot
    and pinned_read."""

    def __init__(
        self,
        allow: Iterable[str],  # The URIs, and the folders, ending in /, that a run may read
        *,
        physical_schemes: Iterable[str],  # The schemes that name a store of the catalog, such as catalog.stores.keys()
        ttl: timedelta = timedelta(hours=1),  # How long a grant lasts
    ):
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

    def decide(
        self,
        ctx: RunContext | RunScope,  # The run's context, or a RunScope before the run opens; the stub reads env and org
        uri: str,  # The URI asked for
        mode: str,  # The access mode asked for; the stub grants only mounted_snapshot and pinned_read
        purpose: str,  # Why, as the caller gave it; the stub does not read it
    ) -> Grant:  # A grant with policy "stub", which ends after ttl, or a deny that says why
        "Decide one request by the stub's rules, which the module lists."

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
