"""
Tenacity callback adapters - bridge Baldur side-effects into tenacity's
``before`` / ``after`` / ``before_sleep`` / ``retry_error_callback`` hooks.

Each adapter is a closure factory that produces a one-arg callable
``(retry_state) -> None`` (or returns a fallback value in the
``retry_error_callback`` case). The closures consult collaborators that the
caller injected into ``TenacityBridgePolicy`` — when a collaborator is
``None``, the callback is a graceful no-op (vanilla tenacity behavior).

Callback chaining helper ``chain()`` is shared with ``instrument.py`` so the
Level-1 monkey-patch and the Level-3 explicit Policy use identical wrapping
semantics — user-supplied callbacks always run first, Baldur callbacks
follow.

Reference:
    451 - D5 (callback mapping)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from baldur.services.retry_handler.observability import record_retry_attempt_started
from baldur.services.retry_handler.rate_limit_detection import detect_rate_limit

if TYPE_CHECKING:
    from baldur.services.backoff_calculator.budget import AdaptiveRetryBudget
    from baldur.services.circuit_breaker.rate_limit_observation import (
        OutboundObservationScope,
    )
    from baldur.services.rate_limit_coordinator.coordinator import (
        RateLimitCoordinator,
    )

logger = structlog.get_logger()


__all__ = [
    "BridgeCallbackContext",
    "chain",
    "make_before_callback",
    "make_after_callback",
    "make_before_sleep_callback",
    "make_retry_error_callback",
    "observe_bridge_outcome",
    "RetryExhaustedSnapshot",
]


# =============================================================================
# Callback chaining helper
# =============================================================================


def chain(
    original: Callable[..., Any] | None,
    baldur_callback: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap ``baldur_callback`` so it runs after ``original`` (if any).

    User-supplied callbacks are preserved verbatim — Baldur never replaces
    them. Original callback exceptions propagate so the user sees them; if
    the original swallows its error, Baldur still runs.
    """
    if original is None:
        return baldur_callback

    def chained(retry_state: Any) -> Any:
        original(retry_state)
        return baldur_callback(retry_state)

    return chained


# =============================================================================
# Snapshot type (returned from retry_error_callback for caller introspection)
# =============================================================================


class RetryExhaustedSnapshot:
    """Frozen view of the final ``RetryCallState`` for the bridge's caller.

    Captured BEFORE delegating to the user's ``retry_error_callback`` so a
    user fallback that suppresses the exception cannot erase the underlying
    failure record. ``TenacityBridgePolicy`` reads ``last_error`` /
    ``attempt_number`` from this snapshot when populating ``PolicyResult``.
    """

    __slots__ = ("attempt_number", "last_error", "user_fallback_value")

    def __init__(
        self,
        attempt_number: int,
        last_error: BaseException | None,
        user_fallback_value: Any = None,
    ) -> None:
        self.attempt_number = attempt_number
        self.last_error = last_error
        self.user_fallback_value = user_fallback_value


# =============================================================================
# Context container — what each callback closure may need
# =============================================================================


class BridgeCallbackContext:
    """Bundle of collaborators referenced by every callback closure.

    Instantiated once per ``TenacityBridgePolicy.execute()`` call. ``None``
    fields disable the corresponding side-effect (callback turns into a
    no-op for that responsibility).
    """

    __slots__ = (
        "domain",
        "last_attempt",
        "last_error",
        "rate_limit_key",
        "rate_limit_coordinator",
        "rate_limit_max_wait",
        "retry_budget",
        "scope",
        "snapshot",
    )

    def __init__(
        self,
        *,
        domain: str,
        rate_limit_key: str | None,
        rate_limit_coordinator: RateLimitCoordinator | None,
        retry_budget: AdaptiveRetryBudget | None,
        rate_limit_max_wait: float | None = None,
        scope: OutboundObservationScope | None = None,
    ) -> None:
        self.domain = domain
        self.rate_limit_key = rate_limit_key
        self.rate_limit_coordinator = rate_limit_coordinator
        self.rate_limit_max_wait = rate_limit_max_wait
        self.retry_budget = retry_budget
        self.scope = scope
        self.snapshot: RetryExhaustedSnapshot | None = None
        # What ``after`` last saw. tenacity runs ``after`` only for an attempt
        # its retry predicate retries or ``stop`` exhausts, so both stay None
        # on a loop whose first attempt was accepted or declined — which is
        # exactly how the execute-level translation tells "already classified"
        # from "never seen", and how a cooldown deferral recovers the real last
        # error instead of reporting a failure for a call that never ran.
        self.last_error: Exception | None = None
        self.last_attempt: int | None = None


# =============================================================================
# Shared 429 observation — one outcome, classified exactly once
# =============================================================================


def observe_bridge_outcome(ctx: BridgeCallbackContext, outcome: Any) -> None:
    """Classify one attempt outcome once, then fan a 429 out. Fail-open.

    Marking the outcome on the observation scope is what keeps the breaker
    stage above from classifying the same object a second time — the composer
    re-raises and returns by identity, so the object this bridge saw is the one
    that reaches the breaker.

    Detection is INSIDE the wrap, matching the retry loop: it reads attributes
    off a caller-supplied exception or response — ``retry_after`` and
    ``headers`` may be properties that raise — so it is part of this site's
    fault surface, not a safe prelude to it.

    The outcome travels with the cascade record so the enclosing breaker's
    ``ignore_exceptions`` filters it here too; the cooldown below is this
    bridge's own backoff and stays outside that dial.
    """
    if ctx.scope is None and ctx.rate_limit_coordinator is None:
        return

    if ctx.scope is not None:
        ctx.scope.mark_classified(outcome)

    try:
        is_rate_limited, retry_after = detect_rate_limit(outcome)
        if not is_rate_limited:
            return

        if ctx.scope is not None:
            ctx.scope.note_429(retry_after, outcome)

        if ctx.rate_limit_coordinator is None or ctx.rate_limit_key is None:
            return

        cooldown = ctx.rate_limit_coordinator.on_rate_limited(
            key=ctx.rate_limit_key,
            retry_after=retry_after,
        )
    except Exception as e:
        logger.warning(
            "bridge.tenacity_rate_limit_cooldown_notify_failed",
            error=str(e),
            key=ctx.rate_limit_key,
        )
        return

    logger.info(
        "bridge.tenacity_rate_limit_cooldown_set",
        cooldown=cooldown,
        key=ctx.rate_limit_key,
    )


# =============================================================================
# Callback factories — one per tenacity hook
# =============================================================================


def make_before_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], None]:
    """``before(retry_state)`` — runs at the start of every attempt.

    Mirrors native ``RetryPolicy``'s top-of-loop logic: record the request
    against ``AdaptiveRetryBudget``, record the attempt to the timely
    retry-pressure series, and wait on ``RateLimitCoordinator`` if a global
    cooldown is active. The wait is bounded by ``ctx.rate_limit_max_wait``; a
    cooldown that exceeds it raises ``_CooldownDeferredAbort`` to stop the
    loop instead of blocking on it.

    The attempt-start record is this bridge's only metric surface: a
    tenacity-driven sequence writes no terminal series, so without it
    bridge-managed retries would be absent from retry pressure while the
    native policies are present in it.
    """

    def _before(retry_state: Any) -> None:
        attempt_number = getattr(retry_state, "attempt_number", 1)
        if ctx.retry_budget is not None:
            ctx.retry_budget.record_request(is_retry=(attempt_number > 1))

        # Before the cooldown wait below, for the same reason the native loops
        # record before theirs: an attempt about to sleep out an honored
        # Retry-After must already be counted.
        record_retry_attempt_started(ctx.domain, attempt_number)

        if ctx.rate_limit_coordinator is not None and ctx.rate_limit_key is not None:
            # Fail-open on a coordinator fault — a coordinator that is down must
            # not break the user's tenacity loop. The deferral below is NOT a
            # fault: it is read off the returned result, outside this wrap.
            try:
                result = ctx.rate_limit_coordinator.wait_if_needed(
                    ctx.rate_limit_key, max_wait=ctx.rate_limit_max_wait
                )
            except Exception as e:
                logger.warning(
                    "bridge.tenacity_rate_limit_wait_failed",
                    error=str(e),
                    key=ctx.rate_limit_key,
                )
                result = None

            if result is not None:
                if result.deferred:
                    raise _CooldownDeferredAbort(
                        key=ctx.rate_limit_key,
                        not_before=result.not_before,
                    )

                if result.waited:
                    logger.debug(
                        "bridge.tenacity_rate_limit_cooldown_waited",
                        wait_time=result.wait_time,
                        key=ctx.rate_limit_key,
                    )

        # Counted last, after the deferral above has had its chance to abort:
        # a deferred attempt never called the dependency, so it belongs in
        # neither side of the cascade rate. tenacity runs ``before`` outside the
        # attempt's own try, so raising there leaves the loop with no further
        # callback — which is why this line cannot be moved above it.
        if ctx.scope is not None:
            ctx.scope.note_attempt()

    return _before


def make_after_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], None]:
    """``after(retry_state)`` — runs after an attempt tenacity retries or exhausts.

    NOT after every attempt: tenacity skips it for an accepted value and for an
    exception the retry predicate declines, both of which leave the loop at
    once. The outcomes it never sees are classified by the bridge's own
    execute-level translation instead.

    On success: notifies ``RateLimitCoordinator.on_success(key)``.
    On failure with a 429-like exception: records the cascade observation and
    requests an ``on_rate_limited`` cooldown so subsequent workers wait.

    Both notifications are fail-open. tenacity invokes ``after`` un-guarded and
    ``Retrying.__call__`` has no try/except, so an escaping coordinator fault
    would leave the loop and be translated into a FAILURE carrying the storage
    error — replacing the business outcome, and on the ``on_rate_limited`` path
    truncating the retry loop before the next attempt ever runs.
    """

    def _after(retry_state: Any) -> None:
        outcome = getattr(retry_state, "outcome", None)
        if outcome is None:
            return

        # Stashed for the execute-level translation: which attempt this was,
        # and the exception it carried. A cooldown deferral reports the real
        # last error from here rather than a failure for a call never made.
        ctx.last_attempt = getattr(retry_state, "attempt_number", None)

        # tenacity's outcome is a Future-like: .failed bool + .exception()
        if not getattr(outcome, "failed", False):
            ctx.last_error = None
            if ctx.rate_limit_coordinator is None or ctx.rate_limit_key is None:
                return
            try:
                ctx.rate_limit_coordinator.on_success(ctx.rate_limit_key)
            except Exception as e:
                logger.warning(
                    "bridge.tenacity_rate_limit_success_notify_failed",
                    error=str(e),
                    key=ctx.rate_limit_key,
                )
            return

        try:
            exc = outcome.exception()
        except Exception:
            return
        if exc is None or not isinstance(exc, BaseException):
            return

        ctx.last_error = exc if isinstance(exc, Exception) else None
        observe_bridge_outcome(ctx, exc)

    return _after


class _BudgetExhaustedAbort(Exception):
    """Internal signal raised in ``before_sleep`` to abort tenacity's loop.

    Caught by ``TenacityBridgePolicy.execute()`` and translated into a
    FAILURE ``PolicyResult`` with the prior exception. Never propagates to
    the user.
    """


class _CooldownDeferredAbort(Exception):
    """Internal signal raised in ``before`` when a 429 cooldown outlasts the bound.

    Same contract as ``_BudgetExhaustedAbort``: caught by the bridge's sync and
    async ``execute()`` and translated into a FAILURE ``PolicyResult`` carrying
    ``rate_limit_deferred`` / ``not_before`` metadata. The attempt was never
    made, so the call is safe to retry at ``not_before``.
    """

    def __init__(self, *, key: str, not_before: float | None) -> None:
        super().__init__(f"Rate limit cooldown deferred: key={key!r}")
        self.key = key
        self.not_before = not_before


def make_before_sleep_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], None]:
    """``before_sleep(retry_state)`` — runs before tenacity's sleep between
    attempts.

    Consults ``AdaptiveRetryBudget.should_allow_retry()``; if exhausted,
    raises ``_BudgetExhaustedAbort`` so the loop short-circuits before
    consuming another attempt.
    """

    def _before_sleep(retry_state: Any) -> None:
        if ctx.retry_budget is None:
            return
        if ctx.retry_budget.should_allow_retry():
            return
        logger.warning(
            "retry.budget_exhausted",
            stats=ctx.retry_budget.get_stats(),
            source="tenacity_bridge",
        )
        raise _BudgetExhaustedAbort("AdaptiveRetryBudget rejected retry")

    return _before_sleep


def make_retry_error_callback(
    ctx: BridgeCallbackContext,
    user_callback: Callable[[Any], Any] | None,
) -> Callable[[Any], Any]:
    """``retry_error_callback(retry_state)`` — final hook when all attempts
    fail.

    Captures a ``RetryExhaustedSnapshot`` BEFORE delegating to the user's
    callback so an exception-suppressing user fallback cannot erase the
    failure record. Emits ``RETRY_EXHAUSTED`` on the EventBus with
    ``source="tenacity_bridge"``.
    """

    def _retry_error(retry_state: Any) -> Any:
        attempt_number = getattr(retry_state, "attempt_number", 1)
        outcome = getattr(retry_state, "outcome", None)
        last_error: BaseException | None = None
        if outcome is not None and getattr(outcome, "failed", False):
            try:
                last_error = outcome.exception()
            except Exception:
                last_error = None

        snapshot = RetryExhaustedSnapshot(
            attempt_number=attempt_number,
            last_error=last_error,
        )
        ctx.snapshot = snapshot

        _emit_retry_exhausted_event(
            domain=ctx.domain,
            attempts=attempt_number,
            last_error=last_error,
        )

        if user_callback is not None:
            user_value = user_callback(retry_state)
            snapshot.user_fallback_value = user_value
            return user_value

        # Re-raise — vanilla tenacity behavior when no user callback set.
        if last_error is not None:
            raise last_error
        return None

    return _retry_error


# =============================================================================
# Event emission helper — fail-open
# =============================================================================


def _emit_retry_exhausted_event(
    *,
    domain: str,
    attempts: int,
    last_error: BaseException | None,
) -> None:
    """Emit ``RETRY_EXHAUSTED`` via the EventBus. Best-effort.

    Mirrors ``RetryPolicy._emit_exhausted_event`` so downstream handlers
    (DLQ Replay, audit, metrics) treat both retry sources uniformly.
    """
    try:
        from baldur.services.event_bus import get_event_bus
        from baldur.services.event_bus.bus.event_types import EventType

        event_data: dict[str, Any] = {
            "domain": domain,
            "attempts": attempts,
            "final_error_type": (
                type(last_error).__name__ if last_error is not None else None
            ),
            # The bridge honors arbitrary user stop strategies (stop_after_delay,
            # etc.), so it cannot attest ``max_attempts`` — ``stop_condition`` is
            # the honest bridge-only value in the shared reason vocabulary.
            "reason": "stop_condition",
        }
        bus = get_event_bus()
        bus.emit(
            event_type=EventType.RETRY_EXHAUSTED,
            data=event_data,
            source="tenacity_bridge",
        )
    except ImportError:
        return
    except Exception as e:
        logger.warning("bridge.tenacity_event_emission_failed", error=str(e))
