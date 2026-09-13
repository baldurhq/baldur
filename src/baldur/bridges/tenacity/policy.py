"""
TenacityBridgePolicy - integrates tenacity.Retrying into Baldur's
ResiliencePolicy[T] Protocol.

A fresh ``tenacity.Retrying`` instance is built per ``execute()`` call
(tenacity stores per-call state on the instance, so reuse would race under
concurrent callers). The policy injects Baldur's budget, rate-limit, and
event-emission callbacks via the standard ``before`` / ``after`` /
``before_sleep`` / ``retry_error_callback`` extension points so the user's
``stop`` / ``wait`` / ``retry`` strategy keeps full control.

Reference:
    451 - D5, D9, D10
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from baldur.bridges.tenacity.callbacks import (
    BridgeCallbackContext,
    chain,
    make_after_callback,
    make_async_after_callback,
    make_async_before_callback,
    make_before_callback,
    make_before_sleep_callback,
    make_retry_error_callback,
    observe_bridge_outcome,
)
from baldur.core.exceptions import RateLimitDeferredError
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

if TYPE_CHECKING:
    import tenacity

    from baldur.bridges.tenacity.callbacks import BridgeCallbackContext
    from baldur.services.backoff_calculator.budget import AdaptiveRetryBudget
    from baldur.services.rate_limit_coordinator.coordinator import (
        RateLimitCoordinator,
    )

logger = structlog.get_logger()

T = TypeVar("T")


__all__ = ["TenacityBridgePolicy", "AsyncTenacityBridgePolicy"]


# Marker attribute set on every Retrying instance built by this policy so
# Level-1 ``instrument_tenacity`` can detect and skip explicit-policy
# instances (prevents double-emit when both levels are active).
_BRIDGE_EXPLICIT_MARKER = "__baldur_bridge_explicit__"


def _pure_rate_limit_verdict(outcome: Any) -> bool:
    """Whether ``outcome`` reads as a 429, with no side effect. Fail-open.

    Used where the bridge needs the verdict without observing the outcome
    again (it was already marked on the scope), and by the async path to
    decide whether a classification must leave the event loop.
    """
    from baldur.services.retry_handler.rate_limit_detection import (
        detect_rate_limit,
    )

    try:
        return detect_rate_limit(outcome)[0]
    except Exception:
        return False


class TenacityBridgePolicy(ResiliencePolicy[T]):
    """Wrap a user-supplied tenacity retry config into ``ResiliencePolicy[T]``.

    Constructor parameters mirror the inputs you would pass to
    ``tenacity.Retrying(...)``. Optional collaborators inject Baldur's
    Self-DDoS protection.

    Args:
        stop: tenacity stop strategy (e.g. ``stop_after_attempt(3)``).
        wait: tenacity wait strategy (e.g. ``wait_exponential()``).
        retry: tenacity predicate (e.g. ``retry_if_exception_type(IOError)``).
        domain: Logical domain name (event metadata and the retry-pressure
            series label). It is never a coordination key: the bridge
            coordinates only under an explicit ``rate_limit_key``.
        retry_budget: ``AdaptiveRetryBudget`` instance shared with native
            ``RetryPolicy`` for global retry-ratio enforcement. ``None``
            disables the budget guard (vanilla tenacity behavior).
        rate_limit_coordinator: ``RateLimitCoordinator`` instance. When
            ``None`` and ``rate_limit_key`` is provided, the policy resolves
            the singleton via ``RateLimitCoordinator.get_instance()``.
        rate_limit_key: Key passed to ``wait_if_needed`` / ``on_rate_limited``.
            ``None`` or an empty string disables rate-limit integration — an
            empty override is not an identity, and coordinating on it would
            share one cooldown record across unrelated downstreams.
        rate_limit_max_wait: Maximum seconds an attempt may block on an active
            429 cooldown. ``None`` uses the coordinator's configured
            ``max_delay``. A cooldown longer than the bound stops the loop and
            yields a FAILURE ``PolicyResult`` with ``rate_limit_deferred`` /
            ``not_before`` metadata instead of blocking the worker.
        before: Optional user ``before(retry_state)`` callback. Runs BEFORE
            Baldur's hook on every attempt.
        after: Optional user ``after(retry_state)`` callback.
        before_sleep: Optional user ``before_sleep(retry_state)`` callback.
        retry_error_callback: Optional user callback that runs when all
            attempts have failed. May return a fallback value.
        retrying_kwargs: Extra kwargs forwarded to ``tenacity.Retrying``
            (e.g. ``reraise=True``). Reserved for advanced uses.

    A ``RateLimitDeferredError`` raised *inside* the wrapped function (by an
    inner ``rate_limit_aware`` client) is an ordinary exception to the user's
    ``retry`` predicate — the bridge does not govern that predicate. The
    bridge's own deferral is decided before an attempt and never enters it.
    """

    def __init__(
        self,
        *,
        stop: Any | None = None,
        wait: Any | None = None,
        retry: Any | None = None,
        domain: str = "default",
        retry_budget: AdaptiveRetryBudget | None = None,
        rate_limit_coordinator: RateLimitCoordinator | None = None,
        rate_limit_key: str | None = None,
        rate_limit_max_wait: float | None = None,
        before: Callable[[Any], None] | None = None,
        after: Callable[[Any], None] | None = None,
        before_sleep: Callable[[Any], None] | None = None,
        retry_error_callback: Callable[[Any], Any] | None = None,
        retrying_kwargs: dict[str, Any] | None = None,
    ) -> None:
        from baldur.bridges.tenacity import _TENACITY_AVAILABLE

        if not _TENACITY_AVAILABLE:
            raise ImportError(
                "baldur-framework[tenacity] extra required — pip install baldur-framework[tenacity]"
            )

        self._stop = stop
        self._wait = wait
        self._retry = retry
        self._domain = domain
        self._retry_budget = retry_budget
        self._rate_limit_key = rate_limit_key
        self._rate_limit_max_wait = rate_limit_max_wait
        self._user_before = before
        self._user_after = after
        self._user_before_sleep = before_sleep
        self._user_retry_error_callback = retry_error_callback
        self._retrying_kwargs = dict(retrying_kwargs) if retrying_kwargs else {}

        # Resolve coordinator lazily — only if a (non-empty) key is provided.
        if rate_limit_coordinator is not None:
            self._rate_limit_coordinator: RateLimitCoordinator | None = (
                rate_limit_coordinator
            )
        elif rate_limit_key:
            from baldur.services.rate_limit_coordinator.coordinator import (
                RateLimitCoordinator,
            )

            self._rate_limit_coordinator = RateLimitCoordinator.get_instance()
        else:
            self._rate_limit_coordinator = None

    # ------------------------------------------------------------------
    # Class-method factory: from_existing
    # ------------------------------------------------------------------

    @classmethod
    def from_existing(
        cls,
        retrying: tenacity.Retrying,
        *,
        domain: str = "default",
        retry_budget: AdaptiveRetryBudget | None = None,
        rate_limit_coordinator: RateLimitCoordinator | None = None,
        rate_limit_key: str | None = None,
        rate_limit_max_wait: float | None = None,
    ) -> TenacityBridgePolicy[T]:
        """Build a bridge from an existing ``tenacity.Retrying`` instance.

        Extracts ``stop`` / ``wait`` / ``retry`` and any user-defined
        callbacks (``before`` / ``after`` / ``before_sleep`` /
        ``retry_error_callback``) from public attributes (stable since
        tenacity 4.x). Each ``execute()`` constructs a fresh internal
        Retrying with these strategies plus Baldur callback chaining.
        """
        return cls(
            stop=getattr(retrying, "stop", None),
            wait=getattr(retrying, "wait", None),
            retry=getattr(retrying, "retry", None),
            domain=domain,
            retry_budget=retry_budget,
            rate_limit_coordinator=rate_limit_coordinator,
            rate_limit_key=rate_limit_key,
            rate_limit_max_wait=rate_limit_max_wait,
            before=getattr(retrying, "before", None),
            after=getattr(retrying, "after", None),
            before_sleep=getattr(retrying, "before_sleep", None),
            retry_error_callback=getattr(retrying, "retry_error_callback", None),
        )

    # ------------------------------------------------------------------
    # ResiliencePolicy[T] Protocol
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "tenacity_bridge"

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Run ``func`` under the wrapped tenacity loop.

        Translates tenacity outcomes into ``PolicyResult[T]``:
        - successful attempt → SUCCESS with ``total_attempts`` from the
          tenacity ``RetryCallState``.
        - all attempts failed (no user callback) → FAILURE with the last
          exception.
        - all attempts failed (user callback returns fallback) → FAILURE
          with ``value=user_fallback`` and ``metadata.user_callback_fallback``.
        - budget-exhausted abort → FAILURE with the prior exception.
        - cooldown-deferred abort → FAILURE with ``rate_limit_deferred``
          metadata; the attempt was never made.
        """
        import tenacity as _t

        from baldur.bridges.tenacity.callbacks import (
            _BudgetExhaustedAbort,
            _CooldownDeferredAbort,
        )

        ctx, retrying_kwargs = self._build_ctx_and_kwargs()

        # When Level-1 instrument is active, pass the marker as a kwarg so
        # the patched ``__init__`` can pop it and skip Baldur callback
        # chaining (impl 451 D7). Vanilla ``tenacity.Retrying.__init__``
        # rejects unknown kwargs, so we only inject when the patch is live.
        from baldur.bridges.tenacity.instrument import is_instrumented

        if is_instrumented():
            retrying_kwargs[_BRIDGE_EXPLICIT_MARKER] = True

        retrying = _t.Retrying(**retrying_kwargs)
        # Defensive instance marker — observable even when Level-1 instrument
        # is not active. ``instrument_tenacity()`` reads the kwarg in that
        # path; this attribute keeps the contract consistent for callers that
        # introspect the Retrying directly.
        setattr(retrying, _BRIDGE_EXPLICIT_MARKER, True)

        start = time.perf_counter()
        try:
            value = retrying(func, *args, **kwargs)
        except _BudgetExhaustedAbort:
            return self._budget_abort_result(ctx, start)
        except _CooldownDeferredAbort as abort:
            return self._cooldown_deferred_result(abort, ctx, start)
        except _t.RetryError as exc:
            return self._retry_error_result(exc, ctx, start)
        except Exception as exc:  # propagated by reraise=True or non-retryable
            return self._generic_exception_result(exc, ctx, retrying, start)

        return self._success_or_fallback_result(value, ctx, retrying, start)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ctx_and_kwargs(
        self, *, use_async_callbacks: bool = False
    ) -> tuple[BridgeCallbackContext, dict[str, Any]]:
        """Build the per-call ``BridgeCallbackContext`` + ``Retrying`` kwargs.

        Shared by the sync and async execute paths — stop/wait/retry strategies
        plus Baldur's chained before/after/before_sleep/retry_error callbacks.
        The async path installs the coroutine ``before`` / ``after`` pair, which
        ``tenacity.AsyncRetrying`` awaits natively. Does NOT set the
        ``_BRIDGE_EXPLICIT_MARKER`` kwarg; that is the sync path's
        Level-1-instrument concern (the async path never injects it).
        """
        # Outbound 429 observation scope, resolved once per call and carried on
        # the context so the per-attempt callbacks read it without re-resolving.
        # A bridge that will drive the coordinator — a coordinator AND a real
        # key — carries its own coordination decision, so it claims the call:
        # the breaker stage above must not install a second cooldown for the
        # same 429. An injected coordinator with no key coordinates nothing
        # and leaves the claim to the breaker stage.
        scope = self._observation_scope()
        if (
            scope is not None
            and self._rate_limit_coordinator is not None
            and self._rate_limit_key
        ):
            scope.claim_coordination()

        ctx = BridgeCallbackContext(
            domain=self._domain,
            rate_limit_key=self._rate_limit_key,
            rate_limit_coordinator=self._rate_limit_coordinator,
            rate_limit_max_wait=self._rate_limit_max_wait,
            retry_budget=self._retry_budget,
            scope=scope,
        )

        if use_async_callbacks:
            before_cb = chain(self._user_before, make_async_before_callback(ctx))
            after_cb = chain(self._user_after, make_async_after_callback(ctx))
        else:
            before_cb = chain(self._user_before, make_before_callback(ctx))
            after_cb = chain(self._user_after, make_after_callback(ctx))
        before_sleep_cb = chain(
            self._user_before_sleep, make_before_sleep_callback(ctx)
        )
        retry_error_cb = make_retry_error_callback(ctx, self._user_retry_error_callback)

        retrying_kwargs: dict[str, Any] = dict(self._retrying_kwargs)
        if self._stop is not None:
            retrying_kwargs.setdefault("stop", self._stop)
        if self._wait is not None:
            retrying_kwargs.setdefault("wait", self._wait)
        if self._retry is not None:
            retrying_kwargs.setdefault("retry", self._retry)
        # A ``before`` supplied through ``retrying_kwargs`` is chained, not
        # dropped — same treatment the constructor's ``before=`` parameter
        # already gets. The user's callback runs first and Baldur's follows,
        # so the attempt-start record cannot be displaced by either spelling.
        retrying_kwargs["before"] = chain(retrying_kwargs.get("before"), before_cb)
        retrying_kwargs["after"] = after_cb
        retrying_kwargs["before_sleep"] = before_sleep_cb
        retrying_kwargs["retry_error_callback"] = retry_error_cb
        return ctx, retrying_kwargs

    @staticmethod
    def _observation_scope() -> Any:
        """The breaker stage's per-call observation scope, or ``None``.

        Lazy import: the breaker package stays out of this module's
        import-time graph, matching how the coordinator is deferred here.
        """
        from baldur.services.circuit_breaker.rate_limit_observation import (
            current_scope,
        )

        return current_scope()

    def _classify_unseen_final_outcome(
        self, outcome: Any, ctx: BridgeCallbackContext, retrying: Any
    ) -> bool:
        """Observe the final outcome when ``after`` never ran for it; report a 429.

        tenacity returns an accepted value, and re-raises an exception its
        retry predicate declines, without invoking ``after`` — so for those two
        exits the per-attempt callback observed nothing. Without this, a keyed
        bridge whose predicate declines a 429 would install no cooldown at all,
        and the breaker stage would be withheld by this bridge's own claim.

        The attempt comparison errs toward "not yet seen": a missing statistics
        entry or an unset ``last_attempt`` classifies here. The scope's identity
        mark is what makes erring that way safe, so it is consulted first — an
        outcome ``after`` really did classify is a no-op here rather than a
        second 429 in the cascade. The returned verdict is what gates the
        success reset: an accepted value that is itself a 429 earns none.
        """
        if ctx.scope is not None and ctx.scope.was_classified(outcome):
            return _pure_rate_limit_verdict(outcome)
        if ctx.last_attempt is not None and ctx.last_attempt == (
            self._statistics_attempts(retrying)
        ):
            return _pure_rate_limit_verdict(outcome)
        return observe_bridge_outcome(ctx, outcome)

    @staticmethod
    def _owes_success_reset(ctx: BridgeCallbackContext, final_is_429: bool) -> bool:
        """Whether the accepted outcome earns the coordinator a ladder reset.

        Only for a loop that ended in an accepted success — never one the
        exhaustion callback saw (a user fallback is not a success), never
        without a rate-limit signal on this call (the reset costs a storage
        read), never when the accepted value is itself a 429, and only when
        this bridge drives a coordinator under a real key.
        """
        return (
            ctx.snapshot is None
            and ctx.rate_limit_signal
            and not final_is_429
            and ctx.rate_limit_coordinator is not None
            and bool(ctx.rate_limit_key)
        )

    @staticmethod
    def _notify_success(ctx: BridgeCallbackContext) -> None:
        """Reset the consecutive-429 ladder after an accepted success. Fail-open."""
        try:
            ctx.rate_limit_coordinator.on_success(ctx.rate_limit_key)  # type: ignore[union-attr, arg-type]
        except Exception as e:
            logger.warning(
                "bridge.tenacity_rate_limit_success_notify_failed",
                error=str(e),
                key=ctx.rate_limit_key,
            )

    def _budget_abort_result(
        self, ctx: BridgeCallbackContext, start: float
    ) -> PolicyResult[T]:
        """Translate a budget-exhausted abort into a FAILURE PolicyResult."""
        duration_ms = (time.perf_counter() - start) * 1000.0
        snapshot = ctx.snapshot
        attempts = snapshot.attempt_number if snapshot else 1
        last_error = snapshot.last_error if snapshot else None
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=last_error if isinstance(last_error, Exception) else None,
            total_attempts=attempts,
            total_duration_ms=duration_ms,
            executed_policies=[self.name],
            metadata={
                "domain": self._domain,
                "budget_exhausted": True,
            },
        )

    def _cooldown_deferred_result(
        self, abort: Any, ctx: BridgeCallbackContext, start: float
    ) -> PolicyResult[T]:
        """Translate a cooldown-deferred abort into a FAILURE PolicyResult.

        Carries the defer vocabulary (``rate_limit_deferred`` / ``not_before``)
        so a requeue-capable caller can reschedule rather than treat this as a
        failed attempt — the deferred attempt never called ``func``.
        """
        duration_ms = (time.perf_counter() - start) * 1000.0
        # ``ctx.snapshot`` is written only by the exhaustion callback, which a
        # ``before``-raised abort never reaches — reading it here reported
        # ``error=None`` for *every* deferral, which the composer then turned
        # into a rejection the breaker counted as a real failure for a call that
        # was never made. Mirror the native loop's synthesis rule instead: the
        # real last error propagates when one exists, and the deferral class is
        # synthesised only when none does.
        last_error: Exception = (
            ctx.last_error
            if isinstance(ctx.last_error, Exception)
            else RateLimitDeferredError(key=abort.key, not_before=abort.not_before)
        )
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=last_error,
            total_attempts=ctx.last_attempt or 1,
            total_duration_ms=duration_ms,
            executed_policies=[self.name],
            metadata={
                "domain": self._domain,
                "rate_limit_deferred": True,
                "not_before": abort.not_before,
            },
        )

    def _retry_error_result(
        self, exc: Exception, ctx: BridgeCallbackContext, start: float
    ) -> PolicyResult[T]:
        """Translate a tenacity ``RetryError`` into a FAILURE PolicyResult."""
        duration_ms = (time.perf_counter() - start) * 1000.0
        attempts = self._extract_attempts(exc, ctx)
        last_error = self._extract_last_error(exc, ctx)
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=last_error if isinstance(last_error, Exception) else None,
            total_attempts=attempts,
            total_duration_ms=duration_ms,
            executed_policies=[self.name],
            metadata={
                "domain": self._domain,
                "tenacity_retry_error": type(exc).__name__,
            },
        )

    def _generic_exception_result(
        self,
        exc: Exception,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """Translate a propagated (reraise/non-retryable) exception into FAILURE."""
        self._classify_unseen_final_outcome(exc, ctx, retrying)
        return self._translate_propagated_exception(exc, ctx, retrying, start)

    def _translate_propagated_exception(
        self,
        exc: Exception,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """The pure half of :meth:`_generic_exception_result` (no I/O)."""
        duration_ms = (time.perf_counter() - start) * 1000.0
        snapshot = ctx.snapshot
        attempts = (
            snapshot.attempt_number if snapshot else self._statistics_attempts(retrying)
        )
        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=exc,
            total_attempts=attempts,
            total_duration_ms=duration_ms,
            executed_policies=[self.name],
            metadata={"domain": self._domain},
        )

    def _success_or_fallback_result(
        self,
        value: Any,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """Translate a completed tenacity loop into SUCCESS (or user-fallback FAILURE).

        The accepted outcome is classified here (tenacity runs no ``after``
        for it), and — when this call observed a rate-limit signal and the
        accepted value is not itself a 429 — the coordinator's consecutive-429
        ladder is reset, the way the native loops reset it on the success
        that ends theirs. The reset never fires for a user fallback: the
        exhaustion callback ran, so the loop did not end in a success.
        """
        final_is_429 = self._classify_unseen_final_outcome(value, ctx, retrying)
        if self._owes_success_reset(ctx, final_is_429):
            self._notify_success(ctx)
        return self._translate_completed_loop(value, ctx, retrying, start)

    def _translate_completed_loop(
        self,
        value: Any,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """The pure half of :meth:`_success_or_fallback_result` (no I/O)."""
        duration_ms = (time.perf_counter() - start) * 1000.0
        snapshot = ctx.snapshot

        # Successful tenacity loop, but the user's retry_error_callback may
        # have produced a fallback value (i.e. all attempts failed but
        # tenacity returned the user's fallback). Detect via the snapshot,
        # which the exhaustion callback writes on every exhaustion — a
        # rejected-result exhaustion has no exception behind it, so keying on
        # ``last_error`` would read that fallback as a success.
        if snapshot is not None:
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.FAILURE,
                error=(
                    snapshot.last_error
                    if isinstance(snapshot.last_error, Exception)
                    else None
                ),
                total_attempts=snapshot.attempt_number,
                total_duration_ms=duration_ms,
                executed_policies=[self.name],
                metadata={
                    "domain": self._domain,
                    "user_callback_fallback": True,
                },
            )

        attempts = self._statistics_attempts(retrying)
        return PolicyResult(
            value=value,
            outcome=PolicyOutcome.SUCCESS,
            total_attempts=attempts,
            total_duration_ms=duration_ms,
            executed_policies=[self.name],
            metadata={"domain": self._domain},
        )

    @staticmethod
    def _statistics_attempts(retrying: tenacity.Retrying) -> int:
        """Read ``attempt_number`` from a Retrying's statistics dict.

        tenacity's ``Retrying.statistics`` exposes ``attempt_number``,
        ``idle_for``, ``delay_since_first_attempt`` and is stable since
        tenacity 5.x.
        """
        stats = getattr(retrying, "statistics", None) or {}
        attempt = stats.get("attempt_number", 1) if isinstance(stats, dict) else 1
        try:
            return int(attempt)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _extract_attempts(retry_error: Exception, ctx: BridgeCallbackContext) -> int:
        if ctx.snapshot is not None:
            return ctx.snapshot.attempt_number
        last_attempt = getattr(retry_error, "last_attempt", None)
        if last_attempt is not None:
            attempt_number = getattr(last_attempt, "attempt_number", None)
            if isinstance(attempt_number, int):
                return attempt_number
        return 1

    @staticmethod
    def _extract_last_error(
        retry_error: Exception, ctx: BridgeCallbackContext
    ) -> BaseException | None:
        if ctx.snapshot is not None and ctx.snapshot.last_error is not None:
            return ctx.snapshot.last_error
        last_attempt = getattr(retry_error, "last_attempt", None)
        if last_attempt is not None:
            try:
                return last_attempt.exception()
            except Exception:
                return None
        return None


class AsyncTenacityBridgePolicy(TenacityBridgePolicy[T]):
    """Async counterpart of :class:`TenacityBridgePolicy` (``AsyncResiliencePolicy``).

    Runs ``func`` under ``tenacity.AsyncRetrying`` — ``AsyncRetrying.__call__``
    is a coroutine, so the loop is driven with ``await``. Reuses the sync
    bridge's constructor, collaborators (budget / rate-limit) and the
    result-translation helpers, but installs the **coroutine** ``before`` /
    ``after`` pair: the cooldown wait is an ``asyncio.sleep`` and every
    cooldown write runs on a worker thread, so a shared cooldown costs this
    call its latency and never stalls the event loop. ``AsyncRetrying`` awaits
    a coroutine action natively (tenacity 8.3+, the ``tenacity`` extra's
    floor).

    Marker handling differs from the sync bridge: ``AsyncRetrying`` is NOT a
    subclass of ``Retrying`` (MRO ``[AsyncRetrying, BaseRetrying, ABC]``) and
    Level-1 ``instrument_tenacity()`` patches only ``Retrying.__init__``, so
    ``AsyncRetrying`` is never Level-1-instrumented and vanilla
    ``AsyncRetrying.__init__`` REJECTS the ``_BRIDGE_EXPLICIT_MARKER`` kwarg.
    Therefore this bridge sets only the **instance-attribute** marker and does
    NOT inject the kwarg (no ``is_instrumented()``-gated injection).

    Facade wiring auto-converts a sync ``TenacityBridgePolicy`` passed as
    ``retry=`` into this class via :meth:`from_sync`, so a user builds one sync
    bridge object and it works on either the sync or the async path.
    """

    @classmethod
    def from_sync(cls, bridge: TenacityBridgePolicy[T]) -> AsyncTenacityBridgePolicy[T]:
        """Build an async bridge from an existing sync :class:`TenacityBridgePolicy`.

        Copies the same stop/wait/retry strategies and collaborators
        (domain, budget, rate-limit coordinator/key, user callbacks,
        retrying_kwargs) so the two paths behave identically off one
        user-built object.
        """
        return cls(
            stop=bridge._stop,
            wait=bridge._wait,
            retry=bridge._retry,
            domain=bridge._domain,
            retry_budget=bridge._retry_budget,
            rate_limit_coordinator=bridge._rate_limit_coordinator,
            rate_limit_key=bridge._rate_limit_key,
            rate_limit_max_wait=bridge._rate_limit_max_wait,
            before=bridge._user_before,
            after=bridge._user_after,
            before_sleep=bridge._user_before_sleep,
            retry_error_callback=bridge._user_retry_error_callback,
            retrying_kwargs=bridge._retrying_kwargs,
        )

    async def execute(  # type: ignore[override]
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Run ``func`` under ``tenacity.AsyncRetrying`` and translate the outcome.

        Same ``PolicyResult`` translation as the sync bridge (shared helpers);
        only the loop driver (``await retrying(...)``) and the marker handling
        (instance attribute only, no kwarg) differ.
        """
        import tenacity as _t

        from baldur.bridges.tenacity.callbacks import (
            _BudgetExhaustedAbort,
            _CooldownDeferredAbort,
        )

        ctx, retrying_kwargs = self._build_ctx_and_kwargs(use_async_callbacks=True)

        # AsyncRetrying is never Level-1-instrumented and vanilla __init__
        # rejects the marker kwarg — set ONLY the instance attribute.
        retrying = _t.AsyncRetrying(**retrying_kwargs)
        setattr(retrying, _BRIDGE_EXPLICIT_MARKER, True)

        start = time.perf_counter()
        try:
            value = await retrying(func, *args, **kwargs)
        except _BudgetExhaustedAbort:
            return self._budget_abort_result(ctx, start)
        except _CooldownDeferredAbort as abort:
            return self._cooldown_deferred_result(abort, ctx, start)
        except _t.RetryError as exc:
            return self._retry_error_result(exc, ctx, start)
        except Exception as exc:  # propagated by reraise=True or non-retryable
            return await self._ageneric_exception_result(exc, ctx, retrying, start)

        return await self._asuccess_or_fallback_result(value, ctx, retrying, start)

    async def _ageneric_exception_result(
        self,
        exc: Exception,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """Async twin of :meth:`_generic_exception_result`.

        The final-outcome classification installs a cooldown for an unseen
        429, which writes the shared store and publishes on the event bus, so
        it runs on a worker thread when the outcome looks rate-limited; every
        other outcome classifies inline (a pure attribute scan).
        """
        if _pure_rate_limit_verdict(exc):
            await asyncio.to_thread(
                self._classify_unseen_final_outcome, exc, ctx, retrying
            )
        else:
            self._classify_unseen_final_outcome(exc, ctx, retrying)
        return self._translate_propagated_exception(exc, ctx, retrying, start)

    async def _asuccess_or_fallback_result(
        self,
        value: Any,
        ctx: BridgeCallbackContext,
        retrying: Any,
        start: float,
    ) -> PolicyResult[T]:
        """Async twin of :meth:`_success_or_fallback_result`.

        Same hop rule as the exception twin for the classification; the
        success reset is a store read plus a conditional write, hopped too.
        """
        if _pure_rate_limit_verdict(value):
            final_is_429 = await asyncio.to_thread(
                self._classify_unseen_final_outcome, value, ctx, retrying
            )
        else:
            final_is_429 = self._classify_unseen_final_outcome(value, ctx, retrying)
        if self._owes_success_reset(ctx, final_is_429):
            await asyncio.to_thread(self._notify_success, ctx)
        return self._translate_completed_loop(value, ctx, retrying, start)
