"""
Core Retry Primitive.

Single retry function that all retry logic should build upon.
Sync-only by design (all 4 current implementations are synchronous).

Usage:
    from baldur.core.retry import retry_with_backoff, RetryConfig

    outcome = retry_with_backoff(
        func,
        RetryConfig(max_retries=3, context_name="payment"),
        arg1, arg2,
    )
    if outcome.success:
        print(outcome.result)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

import structlog

from baldur.core.backoff import BackoffStrategy, ExponentialBackoff

T = TypeVar("T")

logger = structlog.get_logger()


@dataclass
class RetryContext:
    """Context passed to on_retry/on_exhausted callbacks.

    Extensible without breaking callback signatures.
    Used for Prometheus labels, Audit logs, OTel trace correlation.
    """

    func_name: str
    attempt: int
    max_retries: int
    wait_time: float
    elapsed_total: float
    metric_labels: dict[str, str] = field(default_factory=dict)
    trace_id: str | None = None


@dataclass
class RetryConfig:
    """Configuration for retry_with_backoff().

    Args:
        max_retries: Total number of attempts (not retries-after-first).
        backoff: BackoffStrategy instance for delay calculation.
        retryable_exceptions: Exception types that trigger a retry.
        context_name: Label for metrics/logging (e.g. "retry_handler", "replay_service").
        on_retry: Called after each failed attempt (before sleep).
        on_exhausted: Called when all attempts are exhausted.
    """

    max_retries: int = 3
    backoff: BackoffStrategy = field(default_factory=ExponentialBackoff)
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,)
    context_name: str = ""
    on_retry: Callable[[RetryContext, Exception], None] | None = None
    on_exhausted: Callable[[RetryContext, Exception], None] | None = None


@dataclass
class RetryOutcome(Generic[T]):
    """Result of retry_with_backoff().

    Attributes:
        success: Whether the function succeeded.
        result: Return value on success.
        exception: Last exception on failure.
        attempts: Total attempts made.
        total_wait_seconds: Cumulative sleep time.
    """

    success: bool
    result: T | None = None
    exception: Exception | None = None
    attempts: int = 0
    total_wait_seconds: float = 0.0


def retry_with_backoff(
    func: Callable[..., T],
    config: RetryConfig,
    *args: Any,
    **kwargs: Any,
) -> RetryOutcome[T]:
    """Single retry primitive. Foundation for all retry logic. Sync only.

    - RetryHandler: wraps this with DLQ routing on exhaustion
    - ReplayService: binds DLQ status update to on_exhausted
    - RecoveryCoordinator: calls per-step with existing timeout logic
    - TaskQueue adapters: wraps enqueue failures with transient-only exceptions
    """
    last_exception: Exception | None = None
    total_wait = 0.0
    func_name = config.context_name or getattr(func, "__name__", "unknown")

    trace_id: str | None = None
    try:
        from baldur.observability import get_current_trace_id_from_otel

        trace_id = get_current_trace_id_from_otel()
    except Exception:
        pass

    for attempt in range(config.max_retries):
        try:
            result = func(*args, **kwargs)
            return RetryOutcome(success=True, result=result, attempts=attempt + 1)
        except config.retryable_exceptions as e:
            last_exception = e
            wait = 0.0
            if attempt < config.max_retries - 1:
                # calculate() is 1-indexed (attempt=1 -> base_delay); the loop is
                # 0-indexed, so the first retry must pass attempt+1 to honor the
                # configured base_delay instead of base_delay/multiplier.
                wait = config.backoff.calculate(attempt + 1)
                total_wait += wait

            ctx = RetryContext(
                func_name=func_name,
                attempt=attempt,
                max_retries=config.max_retries,
                wait_time=wait,
                elapsed_total=total_wait,
                metric_labels={"context": config.context_name},
                trace_id=trace_id,
            )

            logger.info(
                "retry.attempt",
                func=func_name,
                attempt=attempt + 1,
                max_retries=config.max_retries,
                wait=wait,
                trace_id=trace_id,
            )

            if config.on_retry:
                config.on_retry(ctx, e)

            if attempt < config.max_retries - 1:
                time.sleep(wait)
        except Exception as e:
            # Non-retryable exception: fail immediately
            return RetryOutcome(
                success=False,
                exception=e,
                attempts=attempt + 1,
                total_wait_seconds=total_wait,
            )

    exhausted_ctx = RetryContext(
        func_name=func_name,
        attempt=config.max_retries - 1,
        max_retries=config.max_retries,
        wait_time=0.0,
        elapsed_total=total_wait,
        metric_labels={"context": config.context_name},
        trace_id=trace_id,
    )

    if config.on_exhausted and last_exception:
        config.on_exhausted(exhausted_ctx, last_exception)

    return RetryOutcome(
        success=False,
        exception=last_exception,
        attempts=config.max_retries,
        total_wait_seconds=total_wait,
    )
