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

The async bridge installs the coroutine pair (``make_async_before_callback``
/ ``make_async_after_callback``): ``tenacity.AsyncRetrying`` awaits a
coroutine action natively, so the cooldown wait is an ``asyncio.sleep`` and
the cooldown write runs on a worker thread — neither blocks the event loop.

Reference:
    451 - D5 (callback mapping)
"""

from __future__ import annotations

import asyncio
import inspect
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
    "make_async_before_callback",
    "make_async_after_callback",
    "make_before_sleep_callback",
    "make_retry_error_callback",
    "observe_bridge_outcome",
    "RetryExhaustedSnapshot",
]


# =============================================================================
# Callback chaining helper
# =============================================================================


def _is_coroutine_callable(fn: Callable[..., Any]) -> bool:
    """Whether calling ``fn`` returns a coroutine (a function or a callable object)."""
    if inspect.iscoroutinefunction(fn):
        return True
    return callable(fn) and inspect.iscoroutinefunction(type(fn).__call__)


async def _call_awaiting(fn: Callable[..., Any], retry_state: Any) -> Any:
    """Call ``fn`` and await its result when it is awaitable."""
    result = fn(retry_state)
    if inspect.isawaitable(result):
        return await result
    return result


def chain(
    original: Callable[..., Any] | None,
    baldur_callback: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap ``baldur_callback`` so it runs after ``original`` (if any).

    User-supplied callbacks are preserved verbatim — Baldur never replaces
    them. Original callback exceptions propagate so the user sees them; if
    the original swallows its error, Baldur still runs.

    Coroutine-aware: when either member is a coroutine function the chained
    callback is one too — ``tenacity.AsyncRetrying`` awaits it — and each
    member is awaited or called as its own kind, so a user's synchronous
    ``before`` keeps running first, unchanged, ahead of Baldur's awaited one.
    """
    if original is None:
        return baldur_callback

    if _is_coroutine_callable(original) or _is_coroutine_callable(baldur_callback):

        async def chained_async(retry_state: Any) -> Any:
            await _call_awaiting(original, retry_state)
            return await _call_awaiting(baldur_callback, retry_state)

        return chained_async

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
        "rate_limit_signal",
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
        # Whether this call observed a rate-limit signal — an honored cooldown
        # wait, a coordinator that reported one, or a detected 429. The
        # execute-level translation owes the coordinator a success reset only
        # when it did: a reset costs a storage read on every accepted outcome
        # otherwise.
        self.rate_limit_signal = False


def _coordinates(ctx: BridgeCallbackContext) -> bool:
    """Whether this context drives the shared coordinator at all.

    An empty-string key is not an identity (the same rule the native retry
    stage applies to ``rate_limit_key``): coordinating on it would share one
    cooldown record across unrelated downstreams.
    """
    return ctx.rate_limit_coordinator is not None and bool(ctx.rate_limit_key)


# =============================================================================
# Shared 429 observation — one outcome, classified exactly once
# =============================================================================


def observe_bridge_outcome(ctx: BridgeCallbackContext, outcome: Any) -> bool:
    """Classify one attempt outcome once, then fan a 429 out. Fail-open.

    Returns whether the outcome was a 429 — the execute-level translation
    reads that verdict to decide whether the accepted outcome earns the
    coordinator a success reset (an accepted 429 response never does).

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
        return False

    # Whoever classified the outcome first owns its cooldown — the rule the
    # native loops apply. A ``rate_limit_aware`` client inside this bridge
    # marks the 429 it raised or returned before tenacity hands it to
    # ``after``; that outcome is detected for the signal only, so one 429
    # never advances the cascade or the consecutive counter twice.
    already_classified = ctx.scope is not None and ctx.scope.was_classified(outcome)
    if ctx.scope is not None and not already_classified:
        ctx.scope.mark_classified(outcome)

    try:
        is_rate_limited, retry_after = detect_rate_limit(outcome)
        if not is_rate_limited:
            return False
        ctx.rate_limit_signal = True
        if already_classified:
            return True

        if ctx.scope is not None:
            ctx.scope.note_429(retry_after, outcome)

        if not _coordinates(ctx):
            return True

        cooldown = ctx.rate_limit_coordinator.on_rate_limited(  # type: ignore[union-attr]
            key=ctx.rate_limit_key,  # type: ignore[arg-type]
            retry_after=retry_after,
        )
    except Exception as e:
        logger.warning(
            "bridge.tenacity_rate_limit_cooldown_notify_failed",
            error=str(e),
            key=ctx.rate_limit_key,
        )
        return True

    logger.info(
        "bridge.tenacity_rate_limit_cooldown_set",
        cooldown=cooldown,
        key=ctx.rate_limit_key,
    )
    return True


# =============================================================================
# Callback factories — one per tenacity hook
# =============================================================================


def _admit_attempt(ctx: BridgeCallbackContext, retry_state: Any) -> None:
    """The top-of-attempt bookkeeping both ``before`` callbacks share.

    Records the request against ``AdaptiveRetryBudget`` and the attempt to
    the timely retry-pressure series — before the cooldown wait, for the same
    reason the native loops record before theirs: an attempt about to sleep
    out an honored Retry-After must already be counted.
    """
    attempt_number = getattr(retry_state, "attempt_number", 1)
    if ctx.retry_budget is not None:
        ctx.retry_budget.record_request(is_retry=(attempt_number > 1))
    record_retry_attempt_started(ctx.domain, attempt_number)


def _apply_wait_result(ctx: BridgeCallbackContext, result: Any) -> None:
    """Act on a cooldown wait's result: abort on a deferral, note a signal.

    The deferral is NOT a coordinator fault: it is read off the returned
    result, outside the fail-open wrap the callers put around the wait.
    """
    if result is None:
        return
    if result.deferred:
        raise _CooldownDeferredAbort(
            key=ctx.rate_limit_key,  # type: ignore[arg-type]
            not_before=result.not_before,
        )
    if result.waited or result.was_rate_limited:
        ctx.rate_limit_signal = True
    if result.waited:
        logger.debug(
            "bridge.tenacity_rate_limit_cooldown_waited",
            wait_time=result.wait_time,
            key=ctx.rate_limit_key,
        )


def _note_attempt(ctx: BridgeCallbackContext) -> None:
    """Count the attempt on the observation scope, after the deferral had its chance.

    A deferred attempt never called the dependency, so it belongs in neither
    side of the cascade rate. tenacity runs ``before`` outside the attempt's
    own try, so raising there leaves the loop with no further callback —
    which is why this cannot run before the wait.
    """
    if ctx.scope is not None:
        ctx.scope.note_attempt()


def _log_wait_failed(ctx: BridgeCallbackContext, error: Exception) -> None:
    logger.warning(
        "bridge.tenacity_rate_limit_wait_failed",
        error=str(error),
        key=ctx.rate_limit_key,
    )


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
        _admit_attempt(ctx, retry_state)

        if _coordinates(ctx):
            # Fail-open on a coordinator fault — a coordinator that is down must
            # not break the user's tenacity loop.
            try:
                result = ctx.rate_limit_coordinator.wait_if_needed(  # type: ignore[union-attr]
                    ctx.rate_limit_key, max_wait=ctx.rate_limit_max_wait
                )
            except Exception as e:
                _log_wait_failed(ctx, e)
                result = None
            _apply_wait_result(ctx, result)

        _note_attempt(ctx)

    return _before


def make_async_before_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], Any]:
    """Coroutine ``before(retry_state)`` for ``tenacity.AsyncRetrying``.

    Identical to :func:`make_before_callback` except that the cooldown wait is
    awaited through ``RateLimitCoordinator.await_if_needed`` — an
    ``asyncio.sleep`` plus worker-thread store reads — so an active cooldown
    costs this call its latency and never stalls the event loop. tenacity
    awaits a coroutine action natively and runs it outside the attempt's
    ``try``, so the deferral abort raised from here leaves the loop exactly as
    the synchronous raise does.
    """

    async def _before(retry_state: Any) -> None:
        _admit_attempt(ctx, retry_state)

        if _coordinates(ctx):
            try:
                result = await ctx.rate_limit_coordinator.await_if_needed(  # type: ignore[union-attr]
                    ctx.rate_limit_key, max_wait=ctx.rate_limit_max_wait
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log_wait_failed(ctx, e)
                result = None
            _apply_wait_result(ctx, result)

        _note_attempt(ctx)

    return _before


_NO_SUBJECT = object()


def _after_subject(ctx: BridgeCallbackContext, retry_state: Any) -> Any:
    """Stash what ``after`` saw and return the outcome object to classify.

    Returns the raised exception for a failed attempt, the returned value for
    a retried one (a result predicate is retrying it — a returned 429 is still
    a 429), or the ``_NO_SUBJECT`` sentinel when there is nothing to classify.
    The stash is what the execute-level translation reads: which attempt this
    was, and the exception it carried, so a cooldown deferral reports the real
    last error rather than a failure for a call never made.
    """
    outcome = getattr(retry_state, "outcome", None)
    if outcome is None:
        return _NO_SUBJECT

    ctx.last_attempt = getattr(retry_state, "attempt_number", None)

    # tenacity's outcome is a Future-like: .failed bool + .exception()/.result()
    if not getattr(outcome, "failed", False):
        ctx.last_error = None
        try:
            return outcome.result()
        except Exception:
            return _NO_SUBJECT

    try:
        exc = outcome.exception()
    except Exception:
        return _NO_SUBJECT
    if exc is None or not isinstance(exc, BaseException):
        return _NO_SUBJECT

    ctx.last_error = exc if isinstance(exc, Exception) else None
    return exc


def make_after_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], None]:
    """``after(retry_state)`` — runs after an attempt tenacity retries or exhausts.

    NOT after every attempt: tenacity skips it for an accepted value and for an
    exception the retry predicate declines, both of which leave the loop at
    once. The outcomes it never sees are classified by the bridge's own
    execute-level translation instead — which is also where the success reset
    lives: an attempt this callback sees is one tenacity is about to retry or
    has exhausted, never the accepted success that would earn a reset.

    A retried outcome is classified whether it was raised or returned: a
    result predicate retrying on a 429 response is the same rate-limit answer
    as a raised one, and installs the same cooldown once.

    The notification is fail-open. tenacity invokes ``after`` un-guarded and
    ``Retrying.__call__`` has no try/except, so an escaping coordinator fault
    would leave the loop and be translated into a FAILURE carrying the storage
    error — replacing the business outcome, and on the ``on_rate_limited`` path
    truncating the retry loop before the next attempt ever runs.
    """

    def _after(retry_state: Any) -> None:
        subject = _after_subject(ctx, retry_state)
        if subject is _NO_SUBJECT:
            return
        observe_bridge_outcome(ctx, subject)

    return _after


def make_async_after_callback(
    ctx: BridgeCallbackContext,
) -> Callable[[Any], Any]:
    """Coroutine ``after(retry_state)`` for ``tenacity.AsyncRetrying``.

    Identical to :func:`make_after_callback` except that the classification
    runs on a worker thread: a detected 429 installs the cooldown through
    ``on_rate_limited``, which writes the shared store and publishes on the
    event bus, and neither may run on the event loop.
    """

    async def _after(retry_state: Any) -> None:
        subject = _after_subject(ctx, retry_state)
        if subject is _NO_SUBJECT:
            return
        await asyncio.to_thread(observe_bridge_outcome, ctx, subject)

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

    An exhaustion on a **rejected result** (a result predicate that never
    accepted) has no exception behind it; a ``MaxRetriesExceededError``
    carrying the last value is synthesised — the native loops' rule — so the
    snapshot always holds a real error: the bridge then reports FAILURE (with
    the user's fallback value when a callback supplied one) instead of a
    success the loop never had.
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

        if last_error is None:
            last_error = _synthesize_result_rejection_exhaustion(
                ctx, outcome, attempt_number
            )

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
        raise last_error

    return _retry_error


def _synthesize_result_rejection_exhaustion(
    ctx: BridgeCallbackContext, outcome: Any, attempt_number: int
) -> Exception:
    """Build the exhaustion error for a loop whose result predicate never accepted."""
    from baldur.services.retry_handler.models import MaxRetriesExceededError

    last_result = None
    if outcome is not None:
        try:
            last_result = outcome.result()
        except Exception:
            last_result = None
    exhausted = MaxRetriesExceededError(
        f"Retry exhausted for domain '{ctx.domain}': "
        f"result rejected by predicate after {attempt_number} attempt(s)",
        retry_count=attempt_number,
        max_retries=attempt_number,
        last_error=None,
        last_result=last_result,
        result_rejected=True,
    )
    if ctx.scope is not None:
        # Synthesised here, already classified behind it: the mark keeps the
        # breaker stage from reading the domain name in its message as a
        # fresh rate-limit answer.
        ctx.scope.mark_classified(exhausted)
    return exhausted


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
