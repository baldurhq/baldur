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

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

from baldur.bridges.tenacity.callbacks import (
    BridgeCallbackContext,
    chain,
    make_after_callback,
    make_before_callback,
    make_before_sleep_callback,
    make_retry_error_callback,
)
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


class TenacityBridgePolicy(ResiliencePolicy[T]):
    """Wrap a user-supplied tenacity retry config into ``ResiliencePolicy[T]``.

    Constructor parameters mirror the inputs you would pass to
    ``tenacity.Retrying(...)``. Optional collaborators inject Baldur's
    Self-DDoS protection.

    Args:
        stop: tenacity stop strategy (e.g. ``stop_after_attempt(3)``).
        wait: tenacity wait strategy (e.g. ``wait_exponential()``).
        retry: tenacity predicate (e.g. ``retry_if_exception_type(IOError)``).
        domain: Logical domain name (used as event metadata and as the
            default ``rate_limit_key`` source if no explicit key is given).
        retry_budget: ``AdaptiveRetryBudget`` instance shared with native
            ``RetryPolicy`` for global retry-ratio enforcement. ``None``
            disables the budget guard (vanilla tenacity behavior).
        rate_limit_coordinator: ``RateLimitCoordinator`` instance. When
            ``None`` and ``rate_limit_key`` is provided, the policy resolves
            the singleton via ``RateLimitCoordinator.get_instance()``.
        rate_limit_key: Key passed to ``wait_if_needed`` / ``on_rate_limited``.
            ``None`` disables rate-limit integration.
        before: Optional user ``before(retry_state)`` callback. Runs BEFORE
            Baldur's hook on every attempt.
        after: Optional user ``after(retry_state)`` callback.
        before_sleep: Optional user ``before_sleep(retry_state)`` callback.
        retry_error_callback: Optional user callback that runs when all
            attempts have failed. May return a fallback value.
        retrying_kwargs: Extra kwargs forwarded to ``tenacity.Retrying``
            (e.g. ``reraise=True``). Reserved for advanced uses.
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
        self._user_before = before
        self._user_after = after
        self._user_before_sleep = before_sleep
        self._user_retry_error_callback = retry_error_callback
        self._retrying_kwargs = dict(retrying_kwargs) if retrying_kwargs else {}

        # Resolve coordinator lazily — only if a key is provided.
        if rate_limit_coordinator is not None:
            self._rate_limit_coordinator: RateLimitCoordinator | None = (
                rate_limit_coordinator
            )
        elif rate_limit_key is not None:
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
        """
        import tenacity as _t

        from baldur.bridges.tenacity.callbacks import _BudgetExhaustedAbort

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
        except _t.RetryError as exc:
            return self._retry_error_result(exc, ctx, start)
        except Exception as exc:  # propagated by reraise=True or non-retryable
            return self._generic_exception_result(exc, ctx, retrying, start)

        return self._success_or_fallback_result(value, ctx, retrying, start)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_ctx_and_kwargs(self) -> tuple[BridgeCallbackContext, dict[str, Any]]:
        """Build the per-call ``BridgeCallbackContext`` + ``Retrying`` kwargs.

        Shared by the sync and async execute paths — stop/wait/retry strategies
        plus Baldur's chained before/after/before_sleep/retry_error callbacks.
        Does NOT set the ``_BRIDGE_EXPLICIT_MARKER`` kwarg; that is the sync
        path's Level-1-instrument concern (the async path never injects it).
        """
        ctx = BridgeCallbackContext(
            domain=self._domain,
            rate_limit_key=self._rate_limit_key,
            rate_limit_coordinator=self._rate_limit_coordinator,
            retry_budget=self._retry_budget,
        )

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
        retrying_kwargs["before"] = before_cb
        retrying_kwargs["after"] = after_cb
        retrying_kwargs["before_sleep"] = before_sleep_cb
        retrying_kwargs["retry_error_callback"] = retry_error_cb
        return ctx, retrying_kwargs

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
        """Translate a completed tenacity loop into SUCCESS (or user-fallback FAILURE)."""
        duration_ms = (time.perf_counter() - start) * 1000.0
        snapshot = ctx.snapshot

        # Successful tenacity loop, but the user's retry_error_callback may
        # have produced a fallback value (i.e. all attempts failed but
        # tenacity returned the user's fallback). Detect via snapshot.
        if snapshot is not None and snapshot.last_error is not None:
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
    bridge's constructor, collaborators (budget / rate-limit), the sync
    before/after/before_sleep/retry_error callbacks (tenacity calls them
    synchronously within the async loop), and the result-translation helpers.

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

        from baldur.bridges.tenacity.callbacks import _BudgetExhaustedAbort

        ctx, retrying_kwargs = self._build_ctx_and_kwargs()

        # AsyncRetrying is never Level-1-instrumented and vanilla __init__
        # rejects the marker kwarg — set ONLY the instance attribute.
        retrying = _t.AsyncRetrying(**retrying_kwargs)
        setattr(retrying, _BRIDGE_EXPLICIT_MARKER, True)

        start = time.perf_counter()
        try:
            value = await retrying(func, *args, **kwargs)
        except _BudgetExhaustedAbort:
            return self._budget_abort_result(ctx, start)
        except _t.RetryError as exc:
            return self._retry_error_result(exc, ctx, start)
        except Exception as exc:  # propagated by reraise=True or non-retryable
            return self._generic_exception_result(exc, ctx, retrying, start)

        return self._success_or_fallback_result(value, ctx, retrying, start)
