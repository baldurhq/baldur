"""
Retry Policy — pure retry policy implementation.

Removes the hardcoded external dependencies (Kill Switch, ErrorBudgetGate,
Audit, DLQ) from the legacy RetryHandler and keeps only the retry loop.

External concerns are injected via PolicyComposer's Guard/Hook/Sink.
Internal collaborators are injected via the constructor:
- backoff: backoff calculation strategy (core/backoff.py BackoffStrategy ABC)
- rate_limit_coordinator: 429 wait / success notify / cooldown. This one is
  also *resolved by default* at use time when it is not injected — see
  ``RetryPolicy._resolve_rate_limit_coordinator``. Injection always wins.
- retry_budget: adaptive retry budget (state-mutating in-loop, Guard-unsuitable)
- sleeper: between-attempt wait function. Defaults to ``time.sleep`` so sync
  callers get backoff-honouring behaviour out of the box. Pass an explicit
  no-op (``lambda _: None``) to defer waiting to an external scheduler such
  as Celery.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import structlog

# Default sleeper for sync callers — wires backoff calculation into a real
# wall-clock wait. Pass an explicit no-op (``lambda _: None``) at construction
# to defer waiting to an external scheduler such as Celery.
_DEFAULT_SLEEPER: Callable[[float], None] = time.sleep

# Placeholder domain meaning "the caller did not identify a downstream".
# It is the default of RetryPolicyConfig.domain, of the @retry decorator, and
# of both pipeline presets, so every caller who did not choose a name shares it.
# Outbound 429 coordination therefore refuses to key on it: the storage key
# carries no per-service namespace, so one shared placeholder record would let a
# 429 from one provider stall calls to an unrelated one.
_UNIDENTIFIED_DOMAIN = "default"

# Coordination keys already warned about, so the unidentified-domain diagnostic
# costs one WARNING line per key per process rather than one per call.
_unidentified_key_warned: set[str] = set()
_unidentified_key_warned_lock = threading.Lock()

# Exhaustion-reason -> Prometheus outcome-label value. ``max_attempts`` keeps
# the historical ``"exhausted"`` value for dashboard/alert continuity; every
# other cause gets its own additive value so ``"exhausted"`` no longer conflates
# non-retryable aborts and budget/deadline breaks with genuine attempt exhaustion.
_REASON_TO_OUTCOME: dict[str, str] = {
    "max_attempts": "exhausted",
    "non_retryable": "non_retryable",
    "retry_budget": "retry_budget",
    "max_elapsed": "max_elapsed",
    "deadline": "deadline",
    "rate_limit_deferred": "rate_limit_deferred",
}

from baldur.core.backoff import BackoffStrategy, ExponentialBackoff
from baldur.core.execution_mode import intervention_suppressed
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

from .models import MaxRetriesExceededError, RetryPolicyConfig

if TYPE_CHECKING:
    from baldur.services.backoff_calculator import AdaptiveRetryBudget
    from baldur.services.rate_limit_coordinator import RateLimitCoordinator
    from baldur.services.rate_limit_coordinator.models import RateLimitResult

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
    - rate_limit_coordinator: bundles wait / success-signal / cooldown. Unlike
      the others this one is optional *at construction*: when it is not passed,
      the loop resolves the shared coordinator at use time so a settings-derived
      policy coordinates outbound 429s by default. Passing one always wins, and
      two levers turn the default off — ``rate_limit_aware=False`` on the config
      and ``BALDUR_RATE_LIMIT_BACKOFF_COORDINATION_ENABLED=false``. The default
      also requires an identified domain (see the config field docs).
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
        # Result predicate must be synchronous: an ``async def`` returns a
        # truthy coroutine object that the fail-open guard cannot catch, so every
        # successful result would be judged a soft failure and retried to
        # exhaustion. Reject at construction — the policy is the convergence
        # point of direct-config, from_policy_config, and @retry surfaces.
        self._retry_on_result = config.retry_on_result
        if self._retry_on_result is not None and asyncio.iscoroutinefunction(
            self._retry_on_result
        ):
            raise TypeError(
                "retry_on_result must be a synchronous callable, not a coroutine "
                "function; an async predicate always returns a truthy coroutine "
                "object and cannot be evaluated by the sync retry loop."
            )
        self._max_elapsed = config.max_elapsed
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

    def _resolve_rate_limit_coordinator(self) -> RateLimitCoordinator | None:
        """Resolve the coordinator that this call should coordinate 429s through.

        An explicitly injected coordinator always wins — it bypasses both
        opt-out levers, because a caller who constructed one asked for it.
        Otherwise the process-wide singleton is resolved when all three hold:

        1. ``coordination_enabled`` (deployment kill switch), then
        2. ``config.rate_limit_aware`` (per-policy / per-domain opt-out), then
        3. the coordination key is *identified* (not the placeholder domain).

        The order is load-bearing rather than incidental. Both levers are
        checked before the identity gate because the identity gate is the only
        conjunct that logs: an operator who deliberately turned coordination off
        must not be told to configure the thing they just disabled.

        Fail-open: any resolution fault (settings read, storage auto-detect,
        Redis connect) degrades to ``None`` — no coordination — never to a
        failed business call.
        """
        if self._rate_limit_coordinator is not None:
            return self._rate_limit_coordinator

        try:
            if not self._config.rate_limit_aware:
                return None

            from baldur.settings.rate_limit_backoff import (
                get_rate_limit_backoff_settings,
            )

            if not get_rate_limit_backoff_settings().coordination_enabled:
                return None

            if self._config.rate_limit_key is None and (
                self._config.domain == _UNIDENTIFIED_DOMAIN
            ):
                self._warn_unidentified_coordination_key()
                return None

            from baldur.services.rate_limit_coordinator import RateLimitCoordinator

            return RateLimitCoordinator.get_instance()
        except Exception as resolution_error:
            logger.warning(
                "retry.rate_limit_coordinator_resolution_failed",
                error=str(resolution_error),
                domain=self._config.domain,
            )
            return None

    def _warn_unidentified_coordination_key(self) -> None:
        """Warn once per key that outbound 429 coordination is inert here.

        WARNING rather than DEBUG on purpose: this is the only runtime signal
        that a default-on protection is not actually protecting this call site,
        and DEBUG is off under any production log configuration — the operator
        who most needs the line would never see it. The once-per-key dedup is
        what makes that level affordable.
        """
        key = self._config.domain
        with _unidentified_key_warned_lock:
            if key in _unidentified_key_warned:
                return
            _unidentified_key_warned.add(key)

        logger.warning(
            "retry.rate_limit_coordination_skipped",
            reason="unidentified_domain",
            domain=key,
            remedy=(
                "pass an explicit domain (or a RetryPolicyConfig.rate_limit_key) "
                "so outbound 429 cooldowns are shared per downstream"
            ),
        )

    def execute(  # noqa: C901, PLR0912, PLR0915
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

        # Outbound 429 coordination, resolved once per call. Deliberately placed
        # after the two suppression returns above: both take the single-attempt
        # path, which coordinates nothing, so resolving at method entry would
        # build the coordinator singleton — and with it storage auto-detect and
        # a Redis connect — on paths that never use it. Coverage statement:
        # retry-disabled and observe-only/shadow calls do not coordinate.
        coordinator = self._resolve_rate_limit_coordinator()
        rate_limit_key = self._config.rate_limit_key or self._config.domain
        # D5 cost containment: on_success costs a storage read (plus a reset
        # write when a counter is standing), so it is owed only once this call
        # has actually observed a rate-limit signal — a detected 429, an
        # honored cooldown wait, or a coordinator that reported one.
        rate_limit_signal = False

        attempt = 0
        last_error: Exception | None = None
        last_result: Any = None
        result_rejected = False
        retry_history: list[dict[str, Any]] = []
        reason = "max_attempts"
        not_before: float | None = None

        # Cooperative wall-clock budget (seconds) + its attribution reason,
        # resolved once at entry: min-of-two over the policy knob (max_elapsed)
        # and the request-scoped deadline. ``start`` and the deadline snapshot
        # share this instant, so ``elapsed >= budget`` means the deadline is
        # spent. ``budget is None`` -> unbounded (exactly today's behavior).
        start = time.monotonic()
        budget, budget_reason = self._resolve_effective_budget()

        while attempt < self._config.max_attempts:
            attempt += 1

            # (i) Cooperative budget check — loop top, attempt 2 onward. Catches
            # budget consumed by the previous sleep (rate-limit cooldown waits
            # are now bounded by the remaining budget at the wait site itself,
            # so they can no longer overrun it). Attempt 1 always runs, with one
            # exception: a cooldown deferral can refuse attempt 1 (see the wait
            # site below). Both original reasons survive that exception — the
            # zero-attempt last_error=None FAILURE is prevented by the deferral
            # synthesis below, and a deferral is a correct refusal rather than a
            # deadline artifact, so the deadline middleware's fast-fail job is
            # untouched.
            if (
                attempt > 1
                and budget is not None
                and (time.monotonic() - start) >= budget
            ):
                reason = budget_reason
                break

            # Adaptive Retry Budget: record request + check budget
            if self._retry_budget:
                self._retry_budget.record_request(is_retry=(attempt > 1))
                if attempt > 1 and not self._retry_budget.should_allow_retry():
                    logger.warning(
                        "retry.budget_exhausted",
                        stats=self._retry_budget.get_stats(),
                    )
                    reason = "retry_budget"
                    break

            # Rate limit wait (optional), bounded by whatever budget is left.
            # A cooldown longer than the remaining budget is deferred rather
            # than slept: sleeping it would blow the budget and the attempt
            # would be aborted afterwards anyway.
            if coordinator:
                rl_bound = (
                    None
                    if budget is None
                    else max(0.0, budget - (time.monotonic() - start))
                )
                rl_result = self._wait_for_rate_limit_cooldown(
                    coordinator, rate_limit_key, rl_bound
                )
                if rl_result is not None and rl_result.deferred:
                    reason = "rate_limit_deferred"
                    not_before = rl_result.not_before
                    break
                if rl_result is not None and (
                    rl_result.waited or rl_result.was_rate_limited
                ):
                    rate_limit_signal = True
                if rl_result is not None and rl_result.waited:
                    logger.debug(
                        "retry.rate_limit_cooldown_waited",
                        wait_time=rl_result.wait_time,
                    )

            try:
                result = func(*args, **kwargs)
            except Exception as e:
                last_error = e
                last_result = None
                result_rejected = False
                retry_history.append(
                    {
                        "attempt": attempt,
                        "error_type": type(e).__name__,
                        "error_message": str(e)[:500],
                    }
                )

                # 429 detected → request a cooldown from RateLimitCoordinator.
                # Fail-open: a coordinator fault here must never replace the
                # business error that is being classified below.
                if coordinator:
                    try:
                        if self._notify_rate_limit_cooldown(
                            coordinator, rate_limit_key, e
                        ):
                            rate_limit_signal = True
                    except Exception as coordinator_error:
                        logger.warning(
                            "retry.rate_limit_cooldown_notify_failed",
                            error=str(coordinator_error),
                            domain=self._config.domain,
                        )

                # Pure exception classification. The attempts bound is hoisted to
                # the shared tail below so an out-of-attempts stop is attributed
                # to ``max_attempts``, not ``non_retryable`` (polymorphic-break).
                if not self._should_retry(e):
                    reason = "non_retryable"
                    break
            else:
                # Function returned — evaluate the result predicate (fail-open).
                if not self._evaluate_result_rejected(result):
                    # Fail-open: a coordinator fault must never destroy a
                    # successful business result.
                    if coordinator and rate_limit_signal:
                        try:
                            coordinator.on_success(rate_limit_key)
                        except Exception as coordinator_error:
                            logger.warning(
                                "retry.rate_limit_success_notify_failed",
                                error=str(coordinator_error),
                                domain=self._config.domain,
                            )
                    self._record_outcome(attempt, "success")
                    return PolicyResult(
                        value=result,
                        outcome=PolicyOutcome.SUCCESS,
                        total_attempts=attempt,
                        executed_policies=["retry"],
                    )
                # Soft failure: treat the rejected value exactly like a retryable
                # exception, but no exception is raised — track it so exhaustion
                # can synthesize a MaxRetriesExceededError (last_error stays None).
                last_result = result
                last_error = None
                result_rejected = True
                retry_history.append(
                    {
                        "attempt": attempt,
                        "result_rejected": True,
                        "result_type": type(result).__name__,
                    }
                )

            # --- Shared failure tail: retryable exception OR rejected result ---
            if attempt >= self._config.max_attempts:
                reason = "max_attempts"
                break

            # Compute backoff
            delay = self._backoff.calculate(attempt, context=context)

            # (ii) Cooperative budget check — never start a sleep+attempt that
            # would overrun the budget.
            if budget is not None and (time.monotonic() - start) + delay > budget:
                reason = budget_reason
                break

            # Sleep between attempts. ``self._sleeper`` is always callable —
            # defaults to ``time.sleep`` for sync callers; Celery callers
            # pass an explicit no-op at construction time.
            if delay > 0:
                self._sleeper(delay)

        # Cooldown-deferral exits are synthesized FIRST, ahead of the
        # result-rejection branch below. ``last_error is None`` does not imply
        # "attempt 1": a rejected result sets it to None on every attempt, so a
        # rejection on attempt 1 followed by a deferral on attempt 2 matches both
        # conditions. The deferral is the actual exit cause, and the exhaustion
        # wording below would be factually wrong on it — retries were not
        # exhausted and the deferred attempt never called ``func``. Nothing is
        # lost: the rejected value still rides out in ``PolicyResult.value``.
        if reason == "rate_limit_deferred" and last_error is None:
            # Lazy import: keeps the coordinator package (and its adapter chain)
            # out of this module's import graph, matching the TYPE_CHECKING-only
            # deferral of ``RateLimitCoordinator`` above.
            from baldur.services.rate_limit_coordinator.models import (
                RateLimitDeferredError,
            )

            last_error = RateLimitDeferredError(
                key=self._config.domain,
                not_before=not_before,
            )

        # Result-rejection exits leave last_error=None; synthesize a first-class
        # exhaustion error. Without it the composer maps FAILURE(error=None) to
        # REJECTED (misclassification), and DLQ/@retry lack a real exception.
        elif last_error is None and result_rejected:
            last_error = MaxRetriesExceededError(
                f"Retry exhausted for domain '{self._config.domain}': "
                f"result rejected by predicate after {attempt} attempt(s)",
                retry_count=attempt,
                max_retries=self._config.max_attempts,
                last_error=None,
                last_result=last_result,
                result_rejected=True,
            )

        elapsed = time.monotonic() - start
        self._emit_exhausted_event(
            last_error,
            attempt,
            retry_history,
            reason=reason,
            elapsed=elapsed,
            budget=budget,
            context=context,
        )
        self._record_outcome(attempt, _REASON_TO_OUTCOME.get(reason, "exhausted"))

        return PolicyResult(
            value=last_result if result_rejected else None,
            outcome=PolicyOutcome.FAILURE,
            error=last_error,
            total_attempts=attempt,
            executed_policies=["retry"],
            metadata={
                "max_attempts": self._config.max_attempts,
                "domain": self._config.domain,
                "should_dlq": self._config.enable_dlq,
                "retry_history": retry_history,
                "reason": reason,
                # Defer vocabulary for requeue-capable callers (Celery/DLQ):
                # present only on a cooldown deferral.
                **({"not_before": not_before} if not_before is not None else {}),
            },
        )

    def _wait_for_rate_limit_cooldown(
        self,
        coordinator: RateLimitCoordinator,
        key: str,
        max_wait: float | None,
    ) -> RateLimitResult | None:
        """Wait out an active 429 cooldown, bounded by ``max_wait``. Fail-open.

        Returns the coordinator's result, or ``None`` when the coordinator itself
        failed — a coordinator that is down degrades to inert (proceed without
        waiting) rather than failing the business call. A *deferral* is not a
        fault: it is returned as a normal result for the caller to act on.
        """
        try:
            return coordinator.wait_if_needed(key, max_wait=max_wait)
        except Exception as coordinator_error:
            logger.warning(
                "retry.rate_limit_wait_failed",
                error=str(coordinator_error),
                domain=self._config.domain,
            )
            return None

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
        *,
        reason: str = "max_attempts",
        elapsed: float | None = None,
        budget: float | None = None,
        context: PolicyContext | None = None,
    ) -> None:
        """Emit retry.exhausted event to EventBus. Fail-open.

        ``reason`` disambiguates the exit cause (max_attempts / retry_budget /
        non_retryable / max_elapsed / deadline); ``elapsed`` and ``budget`` are
        additive fields carried for the wall-clock exits.
        """
        try:
            from baldur.services.event_bus import get_event_bus
            from baldur.services.event_bus.bus.event_types import EventType

            event_data: dict = {
                "domain": self._config.domain,
                "max_attempts": self._config.max_attempts,
                "final_error_type": type(last_error).__name__ if last_error else None,
                "attempts": attempts,
                "retry_history_length": len(retry_history),
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

    def _should_retry(self, exception: Exception) -> bool:
        """Pure exception classification: is this exception retryable?

        The attempts bound is intentionally NOT checked here — it is hoisted to
        an explicit loop check so an out-of-attempts stop is attributed to
        ``max_attempts`` rather than ``non_retryable`` (the polymorphic-break
        fix). A non-retryable-classification stop is the only ``non_retryable``.
        """
        if isinstance(exception, self._config.non_retryable_exceptions):
            return False

        return bool(isinstance(exception, self._config.retryable_exceptions))

    def _resolve_effective_budget(self) -> tuple[float | None, str]:
        """Resolve the cooperative wall-clock budget (seconds) and its reason.

        min-of-two over the policy knob (``max_elapsed``) and the request-scoped
        deadline (``deadline_context.get_remaining_ms``). Each side is optional;
        both absent -> ``(None, ...)`` = unbounded. The tighter bound wins the
        attribution; an exact tie is attributed to ``max_elapsed``. The deadline
        lookup is a lazy in-method import (``services -> scaling`` is acyclic)
        and fail-open: any lookup fault degrades to the knob alone.
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
        # Both set: remaining < knob -> deadline is tighter; exact tie -> knob.
        if deadline_s < knob:
            return deadline_s, "deadline"
        return knob, "max_elapsed"

    def _evaluate_result_rejected(self, result: Any) -> bool:
        """Return True if the result predicate rejects ``result`` (soft failure).

        Fail-open: a predicate that raises is logged and treated as *not*
        rejected (accept the result as success) — re-executing on a broken
        predicate would amplify side effects, and a failed feature must be
        inert. ``retry_on_result=None`` never rejects.
        """
        if self._retry_on_result is None:
            return False
        try:
            return bool(self._retry_on_result(result))
        except Exception as e:
            logger.warning("retry.result_predicate_failed", error=str(e))
            return False

    def _notify_rate_limit_cooldown(
        self,
        coordinator: RateLimitCoordinator,
        key: str,
        exception: Exception,
    ) -> bool:
        """Set a cooldown when ``exception`` is a 429; report whether it was one.

        The coordinator is a parameter rather than an attribute read because the
        effective coordinator is resolved per call and may not be the injected
        one. The returned flag is also what tells the loop a rate-limit signal
        was observed, which is the condition for owing an ``on_success`` reset.
        """
        is_rate_limited, retry_after = self._detect_rate_limit(exception)
        if not is_rate_limited:
            return False

        cooldown = coordinator.on_rate_limited(key=key, retry_after=retry_after)
        logger.info(
            "retry.rate_limit_cooldown_set",
            cooldown=cooldown,
        )
        return True

    @staticmethod
    def _detect_rate_limit(exception: Exception) -> tuple[bool, float | None]:
        """Detect 429 rate limit error and extract Retry-After value.

        Delegates to the shared rate_limit_detection utility.
        """
        from .rate_limit_detection import detect_rate_limit

        return detect_rate_limit(exception)
