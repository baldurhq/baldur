"""
Outbound rate-limit observation — the per-call scope and the 429 fan-out.

A dependency's 429 is visible at several depths of one protected call: the
retry ladder sees every attempt, the breaker stage sees only the sequence's
final outcome. This module carries a small per-call record through the chain so
each 429 is counted exactly once, whichever stage happened to see it first.

Two responsibilities, deliberately separate:

- :class:`OutboundObservationScope` — opened by the circuit-breaker stage
  around the business call and published on a ``ContextVar``. Inner stages
  *mutate it in place*; they never ``set`` the variable, because a timeout stage
  runs the inner chain under a copied context where a ``set`` would be invisible
  to the frame that opened the scope.
- :func:`observe_429` — the fan-out for one observed 429: the cascade counter
  and the fleet-wide cooldown, each wrapped independently so neither can take
  the other (or the business call) down with it.

With no scope open — a bare ``@retry``-decorated call, or an inner loop the
user ran on a thread pool without copying the context — the inner stages count
nothing: there is no breaker to trip, so no phantom record is created.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

import structlog

__all__ = [
    "OutboundObservationScope",
    "close_scope",
    "current_scope",
    "observe_429",
    "open_scope",
]

logger = structlog.get_logger()

_current_scope: ContextVar[OutboundObservationScope | None] = ContextVar(
    "baldur_outbound_observation_scope", default=None
)


class OutboundObservationScope:
    """One protected call's 429 bookkeeping, shared with every inner stage.

    Attributes:
        breaker_key: The protected name every write on this call is filed under.
        attempts: Dependency calls an inner stage counted. Read only as
            ``== 0`` ("no inner stage counted anything, so the breaker stage
            owns the one request this call made"), which is why a lost
            increment between concurrent siblings is harmless.
        rate_limited: 429s observed on this call — diagnostics and tests.
        coordination_claimed: Set by any stage that carries its own decision
            about fleet-wide cooldowns, so the breaker stage does not install
            one the operator opted out of.
        classified: The outcome objects already classified or propagated by an
            inner stage, held by identity. The breaker stage classifies the
            final outcome only when it is absent from this list, so an outcome
            an inner stage already saw is never observed twice. A list rather
            than a single slot: concurrent siblings sharing a copied context
            would overwrite one slot, and the first sibling's exception is
            re-raised by identity into the breaker frame.
    """

    __slots__ = (
        "attempts",
        "breaker_key",
        "classified",
        "coordination_claimed",
        "rate_limited",
    )

    def __init__(self, breaker_key: str) -> None:
        self.breaker_key = breaker_key
        self.attempts = 0
        self.rate_limited = 0
        self.coordination_claimed = False
        self.classified: list[Any] = []

    def note_attempt(self) -> None:
        """Count one dependency call and write its request to the tracker.

        The request counter is the cascade rate's denominator. Every observation
        site writes it; the cascade itself never does, so a 429 is never counted
        as two requests.
        """
        self.attempts += 1
        _record_request(self.breaker_key)

    def note_429(self, retry_after: float | None = None) -> None:
        """Record one observed 429 against the cascade (no coordinator notify).

        The stage calling this carries its own coordinator decision — it is
        the cascade half of :func:`observe_429` under another name.
        """
        self.rate_limited += 1
        observe_429(self.breaker_key, retry_after, notify_coordinator=False)

    def mark_classified(self, outcome: Any) -> None:
        """Record that this stage classified (or propagates) ``outcome``."""
        self.classified.append(outcome)

    def was_classified(self, outcome: Any) -> bool:
        """Whether an inner stage already classified ``outcome``, by identity."""
        return any(outcome is seen for seen in self.classified)

    def claim_coordination(self) -> None:
        """Declare that this stage owns the fleet-wide cooldown for this call."""
        self.coordination_claimed = True


def open_scope(breaker_key: str) -> tuple[Token, OutboundObservationScope]:
    """Publish a fresh scope for ``breaker_key`` and return its reset token.

    A nested protected call gets its own scope; resetting the token restores
    the outer one untouched.
    """
    scope = OutboundObservationScope(breaker_key)
    return _current_scope.set(scope), scope


def close_scope(token: Token) -> None:
    """Restore whatever scope was current before the matching :func:`open_scope`."""
    _current_scope.reset(token)


def current_scope() -> OutboundObservationScope | None:
    """The scope this call runs under, or ``None`` when no breaker opened one."""
    return _current_scope.get()


def observe_429(
    key: str,
    retry_after: float | None = None,
    *,
    record_cascade: bool = True,
    notify_coordinator: bool = False,
) -> None:
    """Fan one observed 429 out to the cascade counter and the coordinator.

    The two halves are independent side effects: neither is chained through the
    other, each carries its own fail-open wrap, and neither emits an event of
    its own (the coordinator already emits the canonical, debounced one).

    Args:
        key: The protected name the 429 is filed under.
        retry_after: Provider-stated wait, when the answer carried one.
        record_cascade: False when an inner stage already counted this outcome.
        notify_coordinator: True only when no stage claimed coordination for
            this call.
    """
    if record_cascade:
        _record_cascade_observation(key)
    if notify_coordinator:
        _notify_cooldown(key, retry_after)


def _record_request(key: str) -> None:
    """Write one request to the cascade rate's denominator. Fail-open."""
    try:
        from .rate_limit_tracker import get_rate_limit_tracker

        get_rate_limit_tracker().record_request(key)
    except Exception as error:
        logger.warning(
            "circuit_breaker.rate_limit_observation_failed",
            half="request",
            service_name=key,
            error=str(error),
        )


def _record_cascade_observation(key: str) -> None:
    """Record the 429 against the breaker's cascade detector. Fail-open.

    ``record_rate_limit_response`` is the single writer of the tracker's 429
    counter: every observation site reaches it through here, so no 429 can be
    counted twice by two writers disagreeing about who owns it.
    """
    try:
        from .convenience import get_circuit_breaker_service

        get_circuit_breaker_service().record_rate_limit_response(key)
    except Exception as error:
        logger.warning(
            "circuit_breaker.rate_limit_observation_failed",
            half="cascade",
            service_name=key,
            error=str(error),
        )


def _notify_cooldown(key: str, retry_after: float | None) -> None:
    """Install the fleet-wide cooldown for ``key``. Fail-open.

    Gated by the deployment kill switch and by the same identity rule the retry
    stage applies: an unidentified key would share one cooldown record across
    unrelated downstreams.
    """
    try:
        from baldur.services.retry_handler.rate_limit_detection import (
            UNIDENTIFIED_COORDINATION_KEY,
        )
        from baldur.settings.rate_limit_backoff import get_rate_limit_backoff_settings

        if not get_rate_limit_backoff_settings().coordination_enabled:
            return
        if key == UNIDENTIFIED_COORDINATION_KEY:
            return

        from baldur.services.rate_limit_coordinator import RateLimitCoordinator

        RateLimitCoordinator.get_instance().on_rate_limited(
            key=key, retry_after=retry_after
        )
    except Exception as error:
        logger.warning(
            "circuit_breaker.rate_limit_observation_failed",
            half="coordinator",
            service_name=key,
            error=str(error),
        )
