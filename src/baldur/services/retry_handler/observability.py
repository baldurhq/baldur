"""
Retry observability — shared emission helpers for both retry policies.

The synchronous ``RetryPolicy`` (this package) and the asynchronous
``AsyncRetryPolicy`` (``resilience/policies/async_retry.py``) both terminate on
the same set of outcomes, and each terminal must land in the same two
observability channels: the EventBus ``RETRY_EXHAUSTED`` event and the canonical
Prometheus retry series. Hosting the emitters here — rather than on either policy
— keeps a single source for the payload shape, the reason->outcome vocabulary,
and the fail-open wrapping, so a future field or label change cannot drift
between the sync and async copies.

Two recording moments, deliberately separate:

* ``record_retry_attempt_started`` fires once per attempt, at admission —
  before the call runs and before any backoff or cooldown wait, so retry
  pressure is readable *while* a storm is in flight.
* ``record_retry_outcome`` fires once per sequence, at the terminal — the
  distribution view of how many attempts resolved calls took.

The tenacity bridge joins on the attempt-start half; only the two policies
record terminals.

All helpers are plain synchronous functions. The sync policy calls them
directly. The async policy offloads the *bus* emit through
``asyncio.to_thread`` (``EventBus.publish`` is synchronous and blocking, so a
bare call from a coroutine would park the event loop) and calls the *metric*
recorders directly (a bounded in-process counter increment, cheaper than a
thread hop). See ``AsyncRetryPolicy.execute`` for the call convention.

Bus and metrics imports are deliberately lazy (def-body) so source-module test
patches on ``baldur.services.event_bus.get_event_bus`` and
``baldur.services.metrics.recorders.record_retry_resolution`` /
``…record_retry_attempt_started`` keep intercepting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from baldur.interfaces.resilience_policy import PolicyContext

logger = structlog.get_logger()

# Exhaustion-reason -> Prometheus outcome-label value. ``max_attempts`` keeps
# the historical ``"exhausted"`` value for dashboard/alert continuity; every
# other cause gets its own additive value so ``"exhausted"`` no longer conflates
# non-retryable aborts and budget/deadline breaks with genuine attempt exhaustion.
REASON_TO_OUTCOME: dict[str, str] = {
    "max_attempts": "exhausted",
    "non_retryable": "non_retryable",
    "retry_budget": "retry_budget",
    "max_elapsed": "max_elapsed",
    "deadline": "deadline",
    "rate_limit_deferred": "rate_limit_deferred",
}


def emit_retry_exhausted_event(
    *,
    domain: str,
    max_attempts: int,
    last_error: Exception | None,
    attempts: int,
    retry_history_length: int,
    reason: str,
    elapsed: float | None = None,
    budget: float | None = None,
    context: PolicyContext | None = None,
) -> None:
    """Emit the retry.exhausted event to EventBus. Fail-open.

    ``reason`` disambiguates the exit cause (max_attempts / retry_budget /
    non_retryable / max_elapsed / deadline / rate_limit_deferred); ``elapsed``
    and ``budget`` are additive fields carried for the wall-clock exits.

    Fail-open per CROSS_SERVICE_STANDARDS: a missing EventBus (ImportError) or
    any emission fault is swallowed so the caller's PolicyResult is never
    altered by an observability side effect.
    """
    try:
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.event_types import EventType

        event_data: dict = {
            "domain": domain,
            "max_attempts": max_attempts,
            "final_error_type": type(last_error).__name__ if last_error else None,
            "attempts": attempts,
            "retry_history_length": retry_history_length,
            "reason": reason,
        }
        if elapsed is not None:
            event_data["elapsed"] = elapsed
        if budget is not None:
            event_data["budget"] = budget
        if context is not None:
            if context.order_id:
                event_data["order_id"] = context.order_id
            if context.user_id:
                event_data["user_id"] = context.user_id
            if context.trace_id:
                event_data["trace_id"] = context.trace_id

        bus = get_event_bus()
        bus.emit(
            event_type=EventType.RETRY_EXHAUSTED,
            data=event_data,
            source="retry_policy",
        )
    except ImportError:
        pass  # fail-open: EventBus unavailable
    except Exception as e:
        logger.warning("retry.event_emission_failed", error=str(e))


def record_retry_outcome(domain: str, attempt: int, outcome: str) -> None:
    """Record the terminal retry outcome to the Prometheus retry series. Fail-open.

    Delegates to the canonical ``record_retry_resolution`` facade, which
    resolves the ``domain`` and ``is_synthetic`` labels internally and performs
    both the attempts-histogram observe and the outcomes-counter increment in
    one call. Mirrors the fail-open wrapping of ``emit_retry_exhausted_event``:
    a recorder fault must never change the returned value or the propagated
    exception.
    """
    try:
        from baldur.services.metrics.recorders import record_retry_resolution

        record_retry_resolution(domain, attempt, outcome)
    except Exception as e:
        logger.warning("retry.metric_recording_failed", error=str(e))


def record_retry_attempt_started(domain: str, attempt: int) -> None:
    """Record an attempt start to the timely retry-pressure series. Fail-open.

    Called at attempt admission by both retry policies and by the tenacity
    bridge's ``before`` hook, so the retry share moves while sequences are
    still asleep in backoff or in a rate-limit cooldown wait — the terminal
    series cannot move until they resolve.

    ``attempt`` is 1-based; anything past the first is a retry. The recording
    path deliberately consults no retry budget and reads no policy state: it
    describes what the loop did, it never influences what it does.
    """
    try:
        from baldur.services.metrics.recorders import (
            record_retry_attempt_started as _facade,
        )

        _facade(domain, is_retry=attempt > 1)
    except Exception as e:
        logger.warning("retry.attempt_start_recording_failed", error=str(e))


__all__ = [
    "REASON_TO_OUTCOME",
    "emit_retry_exhausted_event",
    "record_retry_attempt_started",
    "record_retry_outcome",
]
