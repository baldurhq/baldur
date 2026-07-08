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
    ):
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
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
        )

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "retry"

    async def execute(  # noqa: C901
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
        retry_history: list[dict[str, Any]] = []
        func_name = getattr(func, "__qualname__", None) or getattr(
            func, "__name__", "unknown"
        )

        for attempt in range(self._max_retries + 1):
            try:
                if is_async:
                    result = await func(*args, **kwargs)  # type: ignore[misc]
                else:
                    result = await asyncio.to_thread(func, *args, **kwargs)

                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS,
                    total_attempts=attempt + 1,
                    executed_policies=["retry"],
                )

            except asyncio.CancelledError:
                raise

            except Exception as e:
                last_error = e
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "error_type": type(e).__name__,
                        "error_message": str(e)[:500],
                    }
                )

                # Non-retryable check first (CB-open, etc.)
                if isinstance(e, self._non_retryable):
                    break
                if not isinstance(e, self._retryable_exceptions):
                    break

                if context is not None:
                    context = context.with_updates(
                        extra={
                            **context.extra,
                            "retry_attempt": attempt + 1,
                            "retry_last_error": str(e),
                        }
                    )

                if attempt < self._max_retries:
                    # calculate() is 1-indexed (attempt=1 -> base_delay); this loop
                    # is 0-indexed, so pass attempt+1 to honor the configured
                    # base_delay on the first retry (not base_delay/multiplier).
                    delay = self._backoff.calculate(attempt + 1, context=context)

                    logger.debug(
                        "retry.async_attempt_failed",
                        func=func_name,
                        attempt=attempt + 1,
                        max_retries=self._max_retries,
                        delay=delay,
                        error=str(e),
                    )

                    await asyncio.sleep(delay)

        logger.warning(
            "retry.async_exhausted",
            func=func_name,
            max_retries=self._max_retries,
            error=str(last_error),
        )

        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=last_error,
            total_attempts=attempt + 1,
            executed_policies=["retry"],
            metadata={
                "should_dlq": self._enable_dlq,
                "domain": self._domain,
                "max_attempts": self._max_retries + 1,
                "retry_history": retry_history,
            },
        )

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


def retry(
    domain: str = "default",
    max_attempts: int | None = None,
    retryable_exceptions: tuple[type[Exception], ...] | None = None,
    non_retryable_exceptions: tuple[type[Exception], ...] | None = None,
    backoff: BackoffStrategy | None = None,
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

    Example::

        @retry(domain="payment", max_attempts=3)
        def call_external_api():
            return requests.post(...)

        @retry(domain="payment", max_attempts=3, retryable_exceptions=(ConnectionError,))
        async def fetch_data():
            ...
    """
    from baldur.services.retry_handler.models import (
        MaxRetriesExceededError,
        RetryPolicyConfig,
    )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        config = RetryPolicyConfig.from_settings(domain)
        if max_attempts is not None:
            config.max_attempts = max_attempts
        if retryable_exceptions is not None:
            config.retryable_exceptions = retryable_exceptions
        if non_retryable_exceptions is not None:
            config.non_retryable_exceptions = non_retryable_exceptions

        if asyncio.iscoroutinefunction(func):
            apolicy = AsyncRetryPolicy.from_policy_config(config, backoff)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                # ParamSpec/TypeVar cannot track that R is Awaitable when
                # asyncio.iscoroutinefunction(func) is True — dispatch is dynamic.
                result = await apolicy.execute(func, *args, **kwargs)
                if result.success:
                    return result.value  # type: ignore[return-value]
                raise MaxRetriesExceededError(
                    f"Max retries exceeded for {func.__name__}",
                    retry_count=result.total_attempts,
                    max_retries=config.max_attempts,
                    last_error=result.error,
                )

            return async_wrapper  # type: ignore[return-value]

        from baldur.services.retry_handler.policy import RetryPolicy

        sync_policy = RetryPolicy(config=config, backoff=backoff)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            result = sync_policy.execute(func, *args, **kwargs)
            if result.success:
                return result.value  # type: ignore[return-value]
            raise MaxRetriesExceededError(
                f"Max retries exceeded for {func.__name__}",
                retry_count=result.total_attempts,
                max_retries=config.max_attempts,
                last_error=result.error,
            )

        return sync_wrapper

    return decorator


__all__ = [
    "AsyncRetryPolicy",
    "async_retry_policy",
    "retry",
]
