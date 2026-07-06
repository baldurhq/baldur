"""
Retry Policy — pure retry policy implementation.

Removes the hardcoded external dependencies (Kill Switch, ErrorBudgetGate,
Audit, DLQ) from the legacy RetryHandler and keeps only the retry loop.

External concerns are injected via PolicyComposer's Guard/Hook/Sink.
Internal collaborators are injected via the constructor:
- backoff: backoff calculation strategy (core/backoff.py BackoffStrategy ABC)
- rate_limit_coordinator: 429 wait / success notify / cooldown
- retry_budget: adaptive retry budget (state-mutating in-loop, Guard-unsuitable)
- sleeper: between-attempt wait function. Defaults to ``time.sleep`` so sync
  callers get backoff-honouring behaviour out of the box. Pass an explicit
  no-op (``lambda _: None``) to defer waiting to an external scheduler such
  as Celery.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

# Default sleeper for sync callers — wires backoff calculation into a real
# wall-clock wait. Pass an explicit no-op (``lambda _: None``) at construction
# to defer waiting to an external scheduler such as Celery.
_DEFAULT_SLEEPER: Callable[[float], None] = time.sleep

from baldur.core.backoff import BackoffStrategy, ExponentialBackoff
from baldur.core.execution_mode import intervention_suppressed
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

from .models import RetryPolicyConfig

if TYPE_CHECKING:
    from baldur.services.backoff_calculator import AdaptiveRetryBudget
    from baldur.services.rate_limit_coordinator import RateLimitCoordinator

logger = structlog.get_logger()

T = TypeVar("T")


class RetryPolicy(ResiliencePolicy[T]):
    """
    Pure retry Policy.

    External concerns such as Kill Switch, ErrorBudgetGate, Audit, and DLQ are
    handled by PolicyComposer's Guard/Hook/Sink.

    Idempotency contract:
        Functions passed to execute() MUST be idempotent.
        Use IdempotencyGuard + IdempotencyHook via PolicyComposer for
        framework-level enforcement, or implement idempotency in your handler.

    Collaborator:
    - retry_budget: state mutates on every in-loop attempt (Guard-unsuitable)
    - rate_limit_coordinator: bundles wait / success-signal / cooldown
    - backoff: reuses ``core/backoff.py`` BackoffStrategy ABC
    - sleeper: between-attempt wait function. ``None`` (default) -> ``time.sleep``;
      pass ``lambda _: None`` to defer waiting to an external scheduler.
    """

    def __init__(
        self,
        config: RetryPolicyConfig,
        backoff: BackoffStrategy | None = None,
        rate_limit_coordinator: RateLimitCoordinator | None = None,
        retry_budget: AdaptiveRetryBudget | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        from baldur.settings.retry import get_retry_settings

        self._globally_enabled = get_retry_settings().enabled
        self._config = config
        self._backoff = backoff or ExponentialBackoff(
            base_delay=config.backoff_base,
            max_delay=config.backoff_max,
            jitter_factor=config.jitter_percent / 100.0,
        )
        self._rate_limit_coordinator = rate_limit_coordinator
        self._retry_budget = retry_budget
        # ``sleeper=None`` means "use the safe sync default" — historically this
        # silently disabled backoff sleep, which broke the thundering-herd
        # guarantee for every sync call site (protect.py, decorators.py, etc.)
        # because no caller passed an explicit sleeper. Defer-to-Celery callers
        # now opt out by passing an explicit no-op.
        self._sleeper: Callable[[float], None] = (
            sleeper if sleeper is not None else _DEFAULT_SLEEPER
        )

    @property
    def name(self) -> str:
        return "retry"

    def execute(  # noqa: C901
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Pure retry execution.

        Kill Switch, ErrorBudgetGate, Audit, and DLQ are handled by
        PolicyComposer via Guard/Hook/Sink.
        """
        if not self._globally_enabled:
            return self._single_attempt(func, *args, **kwargs)

        # Observe-only (dry-run / shadow / evaluation): suppress the retry
        # intervention — take the single-attempt path (no re-execution),
        # mirroring the globally-disabled branch above. No ``should_dlq`` is
        # set on FAILURE, so the downstream DLQ sink also stays observe-only.
        if intervention_suppressed(
            service_name=self._config.domain or "retry",
            action="retry",
            max_attempts=self._config.max_attempts,
        ):
            return self._single_attempt(func, *args, **kwargs)

        attempt = 0
        last_error: Exception | None = None
        retry_history: list[dict[str, Any]] = []

        while attempt < self._config.max_attempts:
            attempt += 1

            # Adaptive Retry Budget: record request + check budget
            if self._retry_budget:
                self._retry_budget.record_request(is_retry=(attempt > 1))
                if attempt > 1 and not self._retry_budget.should_allow_retry():
                    logger.warning(
                        "retry.budget_exhausted",
                        stats=self._retry_budget.get_stats(),
                    )
                    break

            # Rate limit wait (optional)
            if self._rate_limit_coordinator:
                rl_result = self._rate_limit_coordinator.wait_if_needed(
                    self._config.domain
                )
                if rl_result.waited:
                    logger.debug(
                        "retry.rate_limit_cooldown_waited",
                        wait_time=rl_result.wait_time,
                    )

            try:
                result = func(*args, **kwargs)

                # Notify RateLimitCoordinator of success
                if self._rate_limit_coordinator:
                    self._rate_limit_coordinator.on_success(self._config.domain)

                self._record_outcome(attempt, "success")
                return PolicyResult(
                    value=result,
                    outcome=PolicyOutcome.SUCCESS,
                    total_attempts=attempt,
                    executed_policies=["retry"],
                )
            except Exception as e:
                last_error = e
                retry_history.append(
                    {
                        "attempt": attempt,
                        "error_type": type(e).__name__,
                        "error_message": str(e)[:500],
                    }
                )

                # 429 detected → request a cooldown from RateLimitCoordinator
                if self._rate_limit_coordinator:
                    self._notify_rate_limit_cooldown(e)

                if not self._should_retry(e, attempt):
                    break

                # Compute backoff
                delay = self._backoff.calculate(attempt, context=context)

                # Sleep between attempts. ``self._sleeper`` is always callable —
                # defaults to ``time.sleep`` for sync callers; Celery callers
                # pass an explicit no-op at construction time.
                if delay > 0:
                    self._sleeper(delay)

        self._emit_exhausted_event(last_error, attempt, retry_history, context=context)
        self._record_outcome(attempt, "exhausted")

        return PolicyResult(
            outcome=PolicyOutcome.FAILURE,
            error=last_error,
            total_attempts=attempt,
            executed_policies=["retry"],
            metadata={
                "max_attempts": self._config.max_attempts,
                "domain": self._config.domain,
                "should_dlq": self._config.enable_dlq,
                "retry_history": retry_history,
            },
        )

    def _single_attempt(
        self, func: Callable[..., T], *args: Any, **kwargs: Any
    ) -> PolicyResult[T]:
        """Run the function once with no retry, swallowing into a PolicyResult.

        Shared by the globally-disabled and observe-only paths — both execute
        the business call exactly once and never re-execute.
        """
        try:
            result = func(*args, **kwargs)
            self._record_outcome(1, "success")
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
                executed_policies=["retry"],
            )
        except Exception as e:
            self._record_outcome(1, "failure")
            return PolicyResult(
                outcome=PolicyOutcome.FAILURE,
                error=e,
                total_attempts=1,
                executed_policies=["retry"],
            )

    def _emit_exhausted_event(
        self,
        last_error: Exception | None,
        attempts: int,
        retry_history: list[dict],
        context: PolicyContext | None = None,
    ) -> None:
        """Emit retry.exhausted event to EventBus. Fail-open."""
        try:
            from baldur.services.event_bus import get_event_bus
            from baldur.services.event_bus.bus.event_types import EventType

            event_data: dict = {
                "domain": self._config.domain,
                "max_attempts": self._config.max_attempts,
                "final_error_type": type(last_error).__name__ if last_error else None,
                "attempts": attempts,
                "retry_history_length": len(retry_history),
            }
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

    def _record_outcome(self, attempt: int, outcome: str) -> None:
        """Record the terminal retry outcome to the Prometheus retry series. Fail-open.

        Delegates to the canonical ``record_retry_attempt`` facade, which
        resolves the ``domain`` and ``is_synthetic`` labels internally and
        performs both the attempts-histogram observe and the outcomes-counter
        increment in one call. The inline retry loop runs entirely inside this
        Policy stage, so the composer-level metrics hook cannot observe
        per-attempt retries — recording must live here. Mirrors the fail-open
        wrapping of ``_emit_exhausted_event``: a recorder fault must never
        change the returned value or the propagated exception.
        """
        try:
            from baldur.services.metrics.recorders import record_retry_attempt

            record_retry_attempt(self._config.domain, attempt, outcome)
        except Exception as e:
            logger.warning("retry.metric_recording_failed", error=str(e))

    def _should_retry(self, exception: Exception, attempt: int) -> bool:
        """Decide whether a retry is possible."""
        if attempt >= self._config.max_attempts:
            return False

        if isinstance(exception, self._config.non_retryable_exceptions):
            return False

        return bool(isinstance(exception, self._config.retryable_exceptions))

    def _notify_rate_limit_cooldown(self, exception: Exception) -> None:
        """Set a cooldown on the RateLimitCoordinator when a 429 response is detected."""
        is_rate_limited, retry_after = self._detect_rate_limit(exception)

        if is_rate_limited and self._rate_limit_coordinator:
            cooldown = self._rate_limit_coordinator.on_rate_limited(
                key=self._config.domain,
                retry_after=retry_after,
            )
            logger.info(
                "retry.rate_limit_cooldown_set",
                cooldown=cooldown,
            )

    @staticmethod
    def _detect_rate_limit(exception: Exception) -> tuple[bool, float | None]:
        """Detect 429 rate limit error and extract Retry-After value.

        Delegates to the shared rate_limit_detection utility.
        """
        from .rate_limit_detection import detect_rate_limit

        return detect_rate_limit(exception)
