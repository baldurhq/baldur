"""
Async Retry Policy — retry support for async functions.

Implements the AsyncResiliencePolicy Protocol to close the retry gap in the
async policy chain, and hosts the unified ``@retry`` decorator (sync + async
dual-dispatch).

Coexists as a separate class from the synchronous RetryPolicy
(services/retry_handler/policy.py). RetryPolicy carries infra collaborators
(RateLimitCoordinator, AdaptiveRetryBudget); AsyncRetryPolicy handles pure
async retry logic only.

Not changed:
- Circuit Breaker — nanosecond-level in-memory lookups, so async is unnecessary
  (intentional design).
- BackoffStrategy — pure computation, no I/O.
- RetryPolicy (sync) — existing sync users are unaffected.

Jitter strategy:
- Jitter is fully delegated to BackoffStrategy internals.
- async_sleep_with_jitter() is for Thundering Herd prevention and is not used
  here.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

import structlog

from baldur.core.backoff import BackoffStrategy, ExponentialBackoff
from baldur.core.execution_mode import intervention_suppressed
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)

if TYPE_CHECKING:
    from baldur.services.retry_handler.models import RetryPolicyConfig

logger = structlog.get_logger()

T = TypeVar("T")
P = ParamSpec("P")
R = TypeVar("R")


class AsyncRetryPolicy:
    """
    Async retry policy.

    Retries an async function up to ``max_retries`` times. The retry interval is
    computed by BackoffStrategy and applied via ``asyncio.sleep()``. A sync
    function passed in is wrapped with ``asyncio.to_thread()`` for execution.

    Implements the AsyncResiliencePolicy Protocol.

    Note:
        Jitter is handled inside BackoffStrategy (``jitter`` / ``jitter_factor``
        parameters). AsyncRetryPolicy has no separate jitter logic.
        ``async_sleep_with_jitter()`` is for Thundering Herd prevention and is
        not used here.

    Note:
        A sync function runs on the default thread pool via
        ``asyncio.to_thread()``. Repeatedly retrying a heavy synchronous-I/O
        function can exhaust the thread pool. The correct architectural fix is
        to migrate the function to an async I/O client or scale out worker
        nodes.

    DLQ arming:
        When constructed via :meth:`from_policy_config` with a config whose
        ``enable_dlq`` is True, the exhaustion FAILURE result carries
        ``metadata["should_dlq"]=True`` so a composed DLQ sink stores the final
        failure — mirroring the synchronous RetryPolicy. A bare
        ``AsyncRetryPolicy(...)`` defaults ``enable_dlq=False`` (no DLQ arming).
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff: BackoffStrategy | None = None,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
        non_retryable_exceptions: tuple[type[Exception], ...] | None = None,
        enable_dlq: bool = False,
        domain: str = "default",
        retry_on_result: Callable[[Any], bool] | None = None,
        max_elapsed: float | None = None,
    ):
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        # Result predicate must be synchronous: an ``async def`` returns a truthy
        # coroutine object the fail-open guard cannot catch, so every success
        # would be judged a soft failure and retried to exhaustion. I/O inside a
        # result predicate is a footgun (resilience4j/Polly use sync predicates).
        if retry_on_result is not None and asyncio.iscoroutinefunction(retry_on_result):
            raise TypeError(
                "retry_on_result must be a synchronous callable, not a coroutine "
                "function; an async predicate always returns a truthy coroutine "
                "object and cannot be evaluated by the retry loop."
            )
        self._max_retries = max_retries
        self._backoff = backoff or ExponentialBackoff()
        self._retryable_exceptions = retryable_exceptions

        from baldur.core.exceptions import non_retryable_exceptions as _defaults

        self._non_retryable = (
            non_retryable_exceptions
            if non_retryable_exceptions is not None
            else _defaults()
        )
        self._enable_dlq = enable_dlq
        self._domain = domain
        self._retry_on_result = retry_on_result
        self._max_elapsed = max_elapsed

    @classmethod
    def from_policy_config(
        cls,
        cfg: RetryPolicyConfig,
        backoff: BackoffStrategy | None = None,
    ) -> AsyncRetryPolicy:
        """Build an AsyncRetryPolicy from a RetryPolicyConfig.

        Mirrors the synchronous ``RetryPolicy.__init__`` mapping so the async
        and sync retry stages behave identically off the same config:

        - ``max_retries = max(cfg.max_attempts - 1, 0)`` — sync ``max_attempts``
          counts *total* attempts; async ``max_retries`` counts *additional*
          attempts (``range(max_retries + 1)``). The off-by-one is load-bearing.
        - ``backoff`` defaults to an ExponentialBackoff derived from the config
          (base/max delay + jitter), matching the sync policy.
        - ``enable_dlq`` / ``domain`` populate the exhaustion FAILURE metadata so
          a composed DLQ sink fires on async exhaustion.
        - ``retry_on_result`` / ``max_elapsed`` carry the result-predicate and
          cooperative wall-clock budget so async matches sync off the same config.
        """
        return cls(
            max_retries=max(cfg.max_attempts - 1, 0),
            backoff=backoff
            or ExponentialBackoff(
                base_delay=cfg.backoff_base,
                max_delay=cfg.backoff_max,
                jitter_factor=cfg.jitter_percent / 100.0,
            ),
            retryable_exceptions=cfg.retryable_exceptions,
            non_retryable_exceptions=cfg.non_retryable_exceptions,
            enable_dlq=cfg.enable_dlq,
            domain=cfg.domain,
            retry_on_result=cfg.retry_on_result,
            max_elapsed=cfg.max_elapsed,
        )

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "retry"

    async def execute(  # noqa: C901, PLR0912, PLR0915
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Retry ``func`` up to ``max_retries`` times asynchronously.

        Args:
            func: Function to run (async def or sync def).
            *args: Positional arguments.
            context: Policy context (retry attempt/last_error propagated to extra).
            **kwargs: Keyword arguments.

        Returns:
            PolicyResult with value or error.
        """
        _unwrapped = func
        while isinstance(_unwrapped, functools.partial):
            _unwrapped = _unwrapped.func
        is_async = asyncio.iscoroutinefunction(_unwrapped)

        # Observe-only (dry-run / shadow / evaluation): suppress the retry
        # intervention — take the single-attempt path (no re-execution),
        # mirroring the synchronous RetryPolicy dry-run guard. No ``should_dlq``
        # is set on FAILURE, so the downstream DLQ sink also stays observe-only.
        if intervention_suppressed(
            service_name=self._domain,
            action="retry",
            max_attempts=self._max_retries + 1,
        ):
            return await self._single_attempt(func, is_async, *args, **kwargs)

        last_error: Exception | None = None
        last_result: Any = None
        result_rejected = False
        retry_history: list[dict[str, Any]] = []
        reason = "max_attempts"
        func_name = getattr(func, "__qualname__", None) or getattr(
            func, "__name__", "unknown"
        )

        # Cooperative wall-clock budget (seconds) + its attribution reason,
        # resolved once at entry (min-of-two over the knob and the ContextVar
        # deadline, which propagates natively in asyncio). None -> unbounded.
        start = time.monotonic()
        budget, budget_reason = self._resolve_effective_budget()

        attempt = 0
        for attempt in range(self._max_retries + 1):
            # (i) Cooperative budget check — 2nd iteration onward; the first
            # attempt always runs (parity with the sync policy).
            if (
                attempt > 0
                and budget is not None
                and (time.monotonic() - start) >= budget
            ):
                reason = budget_reason
                break

            try:
                if is_async:
                    result = await func(*args, **kwargs)  # type: ignore[misc]
                else:
                    result = await asyncio.to_thread(func, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = e
                last_result = None
                result_rejected = False
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "error_type": type(e).__name__,
                        "error_message": str(e)[:500],
                    }
                )

                # Non-retryable check first (CB-open, etc.). The attempts bound
                # is hoisted to the shared tail so an out-of-attempts stop is
                # attributed to ``max_attempts``, not ``non_retryable``.
                if isinstance(e, self._non_retryable):
                    reason = "non_retryable"
                    break
                if not isinstance(e, self._retryable_exceptions):
                    reason = "non_retryable"
                    break

                if context is not None:
                    context = context.with_updates(
                        extra={
                            **context.extra,
                            "retry_attempt": attempt + 1,
                            "retry_last_error": str(e),
                        }
                    )
            else:
                # Function returned — evaluate the result predicate (fail-open).
                if not self._evaluate_result_rejected(result):
                    return PolicyResult(
                        value=result,
                        outcome=PolicyOutcome.SUCCESS,
                        total_attempts=attempt + 1,
                        executed_policies=["retry"],
                    )
                # Soft failure: treat the rejected value like a retryable
                # exception; no exception is raised, so last_error stays None and
                # exhaustion synthesizes a MaxRetriesExceededError.
                last_result = result
                last_error = None
                result_rejected = True
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "result_rejected": True,
                        "result_type": type(result).__name__,
                    }
                )

            # --- Shared failure tail: retryable exception OR rejected result ---
            if attempt >= self._max_retries:
                reason = "max_attempts"
                break

            # calculate() is 1-indexed (attempt=1 -> base_delay); this loop is
            # 0-indexed, so pass attempt+1 to honor the configured base_delay on
            # the first retry (not base_delay/multiplier).
            delay = self._backoff.calculate(attempt + 1, context=context)

            # (ii) Cooperative budget check — never start a sleep+attempt that
            # would overrun the budget.
            if budget is not None and (time.monotonic() - start) + delay > budget:
                reason = budget_reason
                break

            logger.debug(
                "retry.async_attempt_failed",
                func=func_name,
                attempt=attempt + 1,
                max_retries=self._max_retries,
                delay=delay,
                error=str(last_error),
            )

            await asyncio.sleep(delay)

        # Result-rejection exits leave last_error=None; synthesize a first-class
        # exhaustion error so DLQ / @retry have a real exception and the composer
        # does not misclassify FAILURE(error=None) as REJECTED.
        if last_error is None and result_rejected:
            from baldur.services.retry_handler.models import MaxRetriesExceededError

            last_error = MaxRetriesExceededError(
                f"Retry exhausted for domain '{self._domain}': "
                f"result rejected by predicate after {attempt + 1} attempt(s)",
                retry_count=attempt + 1,
                max_retries=self._max_retries + 1,
                last_error=None,
                last_result=last_result,
                result_rejected=True,
            )

        logger.warning(
            "retry.async_exhausted",
            func=func_name,
            max_retries=self._max_retries,
            error=str(last_error),
            reason=reason,
        )

        return PolicyResult(
            value=last_result if result_rejected else None,
            outcome=PolicyOutcome.FAILURE,
            error=last_error,
            total_attempts=attempt + 1,
            executed_policies=["retry"],
            metadata={
                "should_dlq": self._enable_dlq,
                "domain": self._domain,
                "max_attempts": self._max_retries + 1,
                "retry_history": retry_history,
                "reason": reason,
            },
        )

    def _resolve_effective_budget(self) -> tuple[float | None, str]:
        """Resolve the cooperative wall-clock budget (seconds) and its reason.

        min-of-two over the policy knob (``max_elapsed``) and the request-scoped
        deadline (``deadline_context.get_remaining_ms``, a ContextVar that
        propagates natively in asyncio). Each side optional; both absent ->
        ``(None, ...)`` = unbounded. Tighter bound wins; exact tie -> knob.
        Fail-open on the deadline lookup.
        """
        knob = self._max_elapsed
        deadline_s: float | None = None
        try:
            from baldur.scaling.deadline_context import get_remaining_ms

            remaining_ms = get_remaining_ms()
            if remaining_ms is not None:
                deadline_s = remaining_ms / 1000.0
        except Exception:
            deadline_s = None

        if knob is None and deadline_s is None:
            return None, "max_elapsed"
        if knob is None:
            return deadline_s, "deadline"
        if deadline_s is None:
            return knob, "max_elapsed"
        if deadline_s < knob:
            return deadline_s, "deadline"
        return knob, "max_elapsed"

    def _evaluate_result_rejected(self, result: Any) -> bool:
        """Return True if the result predicate rejects ``result`` (soft failure).

        Fail-open: a predicate that raises is logged and treated as *not*
        rejected (accept the result as success). ``retry_on_result=None`` never
        rejects. The predicate is synchronous (async predicates are rejected at
        construction) so it is called directly, never awaited.
        """
        if self._retry_on_result is None:
            return False
        try:
            return bool(self._retry_on_result(result))
        except Exception as e:
            logger.warning("retry.result_predicate_failed", error=str(e))
            return False

    async def _single_attempt(
        self,
        func: Callable[..., T],
        is_async: bool,
        *args: Any,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Run the function once with no retry, swallowing into a PolicyResult.

        Used by the observe-only path — executes the business call exactly once
        and never re-executes. Mirrors the synchronous
        ``RetryPolicy._single_attempt``: the FAILURE result carries no
        ``should_dlq``, so the downstream DLQ sink stays observe-only.
        """
        try:
            if is_async:
                result = await func(*args, **kwargs)  # type: ignore[misc]
            else:
                result = await asyncio.to_thread(func, *args, **kwargs)
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
                executed_policies=["retry"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return PolicyResult(
                outcome=PolicyOutcome.FAILURE,
                error=e,
                total_attempts=1,
                executed_policies=["retry"],
            )


def async_retry_policy(
    max_retries: int = 3,
    backoff: BackoffStrategy | None = None,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    non_retryable_exceptions: tuple[type[Exception], ...] | None = None,
) -> AsyncRetryPolicy:
    """AsyncRetryPolicy factory function."""
    return AsyncRetryPolicy(
        max_retries=max_retries,
        backoff=backoff,
        retryable_exceptions=retryable_exceptions,
        non_retryable_exceptions=non_retryable_exceptions,
    )


def _unwrap_or_raise(result: PolicyResult, func_name: str, max_attempts: int) -> Any:
    """Return the success value, or raise ``MaxRetriesExceededError`` on failure.

    Double-wrap guard: result-predicate exhaustion already synthesized a
    ``MaxRetriesExceededError`` (carrying ``last_result`` / ``result_rejected``)
    — re-raise it as-is rather than nesting it inside a second one. Shared by the
    ``@retry`` sync and async wrappers.
    """
    from baldur.services.retry_handler.models import MaxRetriesExceededError

    if result.success:
        return result.value
    if isinstance(result.error, MaxRetriesExceededError):
        raise result.error
    raise MaxRetriesExceededError(
        f"Max retries exceeded for {func_name}",
        retry_count=result.total_attempts,
        max_retries=max_attempts,
        last_error=result.error,
    )


def retry(
    domain: str = "default",
    max_attempts: int | None = None,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
    non_retryable_exceptions: tuple[type[Exception], ...] | None = None,
    backoff: BackoffStrategy | None = None,
    retry_on_result: Callable[[Any], bool] | None = None,
    max_elapsed: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Unified retry decorator — dual-dispatches on sync vs async functions.

    Replaces the previous split retry decorators (a sync-only one and an
    async-only one) with a single call-style-safe surface. Both branches derive
    their configuration from ``RetryPolicyConfig.from_settings(domain)`` with
    the passed overrides applied:

    - An ``async def`` is wrapped by :class:`AsyncRetryPolicy`.
    - A plain ``def`` is wrapped by the synchronous ``RetryPolicy``.

    On exhaustion, both branches raise ``MaxRetriesExceededError`` (carrying
    ``last_error``); success returns the unwrapped value. ``functools.wraps``
    preserves the wrapped signature, so framework dependency injection (e.g.
    FastAPI ``Depends``) resolves against the original parameters.

    Args:
        domain: Configuration domain (also the retry / DLQ / metric key).
        max_attempts: Override the total attempt count (``None`` uses settings).
        retryable_exceptions: Override the retryable exception tuple.
        non_retryable_exceptions: Override the non-retryable exception tuple.
        backoff: Explicit BackoffStrategy (``None`` derives one from settings).
        retry_on_result: Synchronous predicate that returns True for a soft-error
            *result* (200 + error payload, ``None``, partial response) that
            should be retried. An ``async def`` predicate raises ``TypeError``.
        max_elapsed: Cooperative wall-clock retry budget in seconds (``None``
            uses settings, where it also defaults to disabled).

    Example::

        @retry(domain="payment", max_attempts=3)
        def call_external_api():
            return requests.post(...)

        @retry(domain="payment", retry_on_result=lambda r: r.get("status") == "error")
        def fetch_data():
            ...
    """
    from baldur.services.retry_handler.models import RetryPolicyConfig

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        config = RetryPolicyConfig.from_settings(domain)
        if max_attempts is not None:
            config.max_attempts = max_attempts
        if retryable_exceptions is not None:
            config.retryable_exceptions = retryable_exceptions
        if non_retryable_exceptions is not None:
            config.non_retryable_exceptions = non_retryable_exceptions
        if retry_on_result is not None:
            config.retry_on_result = retry_on_result
        if max_elapsed is not None:
            config.max_elapsed = max_elapsed

        if asyncio.iscoroutinefunction(func):
            apolicy = AsyncRetryPolicy.from_policy_config(config, backoff)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                # ParamSpec/TypeVar cannot track that R is Awaitable when
                # asyncio.iscoroutinefunction(func) is True — dispatch is dynamic.
                result = await apolicy.execute(func, *args, **kwargs)
                return _unwrap_or_raise(result, func.__name__, config.max_attempts)

            return async_wrapper  # type: ignore[return-value]

        from baldur.services.retry_handler.policy import RetryPolicy

        sync_policy = RetryPolicy(config=config, backoff=backoff)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            result = sync_policy.execute(func, *args, **kwargs)
            return _unwrap_or_raise(result, func.__name__, config.max_attempts)

        return sync_wrapper

    return decorator


__all__ = [
    "AsyncRetryPolicy",
    "async_retry_policy",
    "retry",
]
