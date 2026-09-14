"""
Async Retry Policy — retry support for async functions.

Implements the AsyncResiliencePolicy Protocol to close the retry gap in the
async policy chain, and hosts the unified ``@retry`` decorator (sync + async
dual-dispatch).

Coexists as a separate class from the synchronous RetryPolicy
(services/retry_handler/policy.py) and mirrors its loop: the same cooperative
budget, the same result predicate, and the same outbound 429 coordination —
both stages resolve the shared ``RateLimitCoordinator`` by default under one
identity rule (``rate_limit_key`` or ``domain``) and the same two opt-out
levers, wait on the shared cooldown before every attempt, record each observed
429 (raised or returned) once, and reset the ladder after a success that
followed a rate-limit signal. On this stage every wait is an ``asyncio.sleep``,
and every cooldown write, 429 publish and cascade 429 note runs on a worker
thread, so a cooldown costs the request its latency and never stalls the event
loop. The one collaborator not carried is ``AdaptiveRetryBudget`` (sync only).

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
from baldur.core.exceptions import RateLimitDeferredError
from baldur.core.execution_mode import intervention_suppressed
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)

if TYPE_CHECKING:
    from baldur.services.circuit_breaker.rate_limit_observation import (
        OutboundObservationScope,
    )
    from baldur.services.rate_limit_coordinator import RateLimitCoordinator
    from baldur.services.rate_limit_coordinator.models import RateLimitResult
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

    Outbound 429 coordination:
        Like the synchronous policy, this stage resolves the shared
        ``RateLimitCoordinator`` at use time when none is injected, provided
        ``rate_limit_aware`` is on, the deployment switch is on, and the call
        carries an identified domain (``rate_limit_key`` or a non-placeholder
        ``domain``). An injected coordinator wins over both levers. The wait
        before each attempt is an ``asyncio.sleep`` bounded by the remaining
        budget; a cooldown that outlasts it ends the call with
        ``reason="rate_limit_deferred"`` and ``not_before`` in the metadata.
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
        backoff_factory: Callable[[], BackoffStrategy] | None = None,
        rate_limit_aware: bool = True,
        rate_limit_key: str | None = None,
        rate_limit_coordinator: RateLimitCoordinator | None = None,
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
        # Global master toggle, snapshotted at construction (parity with the
        # synchronous RetryPolicy). When BALDUR_RETRY_ENABLED is False, execute()
        # runs the function once via _single_attempt with no retry. Snapshotting
        # in __init__ — not in from_policy_config — is what covers all four
        # construction paths (decoration, per-call composer build, the exported
        # async_retry_policy() factory, and the bare constructor).
        from baldur.settings.retry import get_retry_settings

        self._globally_enabled = get_retry_settings().enabled
        self._max_retries = max_retries
        # Mirrors the synchronous policy: an injected strategy wins, otherwise
        # the factory decides per execution. The factory slot exists for the one
        # stateful strategy — sharing a single decorrelated instance would let
        # two concurrent executions on the same policy consume each other's
        # previous delay.
        # ``_backoff`` holds the shared instance and is None exactly when each
        # execution builds its own.
        self._backoff: BackoffStrategy | None
        self._backoff_factory: Callable[[], BackoffStrategy]
        if backoff is None and backoff_factory is not None:
            self._backoff = None
            self._backoff_factory = backoff_factory
        else:
            strategy = backoff or ExponentialBackoff()
            self._backoff = strategy
            self._backoff_factory = lambda: strategy
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
        # Outbound 429 coordination, mirroring the synchronous policy: the
        # opt-out lever, the key override, and an injected coordinator that
        # wins over both levers. Resolution happens per call, never cached here.
        self._rate_limit_aware = rate_limit_aware
        self._rate_limit_key = rate_limit_key
        self._rate_limit_coordinator = rate_limit_coordinator

    @classmethod
    def from_policy_config(
        cls,
        cfg: RetryPolicyConfig,
        backoff: BackoffStrategy | None = None,
    ) -> AsyncRetryPolicy:
        """Build an AsyncRetryPolicy from a RetryPolicyConfig.

        Mirrors the synchronous ``RetryPolicy.__init__`` mapping so the async
        and sync retry stages behave identically off the fields listed below:

        - ``max_retries = max(cfg.max_attempts - 1, 0)`` — sync ``max_attempts``
          counts *total* attempts; async ``max_retries`` counts *additional*
          attempts (``range(max_retries + 1)``). The off-by-one is load-bearing.
        - ``backoff`` defaults to the strategy the config itself builds, so the
          async ladder honors the same resolved base, multiplier, increment,
          jitter width and strategy name as the sync one. The stateful
          decorrelated strategy is passed as a factory instead of an instance,
          so each execution gets its own.
        - ``enable_dlq`` / ``domain`` populate the exhaustion FAILURE metadata so
          a composed DLQ sink fires on async exhaustion.
        - ``retry_on_result`` / ``max_elapsed`` carry the result-predicate and
          cooperative wall-clock budget so async matches sync off the same config.
        - ``rate_limit_aware`` / ``rate_limit_key`` carry the outbound 429
          coordination fields in full: the same opt-out and the same key
          override the synchronous stage reads, resolved through the shared
          identity rule and lever order.
        """
        # Local import: the retry_handler package is deliberately kept out of
        # this module's import-time graph (see the TYPE_CHECKING block above).
        from baldur.services.retry_handler.models import STATEFUL_BACKOFF_STRATEGY

        stateful = cfg.backoff_strategy == STATEFUL_BACKOFF_STRATEGY
        return cls(
            max_retries=max(cfg.max_attempts - 1, 0),
            backoff=backoff or (None if stateful else cfg.build_backoff()),
            backoff_factory=cfg.build_backoff if stateful else None,
            retryable_exceptions=cfg.retryable_exceptions,
            non_retryable_exceptions=cfg.non_retryable_exceptions,
            enable_dlq=cfg.enable_dlq,
            domain=cfg.domain,
            retry_on_result=cfg.retry_on_result,
            max_elapsed=cfg.max_elapsed,
            rate_limit_aware=cfg.rate_limit_aware,
            rate_limit_key=cfg.rate_limit_key,
        )

    @staticmethod
    def _observation_scope() -> OutboundObservationScope | None:
        """The breaker stage's per-call observation scope, or ``None``.

        Lazy import: the breaker package stays out of this module's
        import-time graph.
        """
        from baldur.services.circuit_breaker.rate_limit_observation import (
            current_scope,
        )

        return current_scope()

    def _coordination_key(self) -> str:
        """The key this policy's outbound 429 cooldowns are shared under.

        The identity rule both retry stages share (``rate_limit_key`` or,
        failing that, ``domain``); def-body import keeps the retry_handler
        package out of this module's import-time graph.
        """
        from baldur.services.retry_handler.coordination import coordination_key

        return coordination_key(self._rate_limit_key, self._domain)

    async def _resolve_rate_limit_coordinator(self) -> RateLimitCoordinator | None:
        """Resolve the coordinator this call coordinates 429s through, or ``None``.

        An injected coordinator wins over both opt-out levers (sync parity).
        Otherwise the shared admission rule — per-policy opt-out, deployment
        kill switch, identity gate, in that order — decides whether the
        process-wide singleton is resolved; the first resolution of the
        singleton (storage auto-detect, a bounded Redis probe) runs on a
        worker thread. Fail-open: any fault degrades to no coordination.
        """
        if self._rate_limit_coordinator is not None:
            return self._rate_limit_coordinator

        try:
            from baldur.services.retry_handler.coordination import (
                coordination_admitted,
            )

            if not coordination_admitted(
                rate_limit_aware=self._rate_limit_aware,
                rate_limit_key=self._rate_limit_key,
                domain=self._domain,
            ):
                return None

            from baldur.services.rate_limit_coordinator import RateLimitCoordinator

            return await RateLimitCoordinator.aget_instance()
        except Exception as resolution_error:
            logger.warning(
                "retry.rate_limit_coordinator_resolution_failed",
                error=str(resolution_error),
                domain=self._domain,
            )
            return None

    async def _await_rate_limit_cooldown(
        self,
        coordinator: RateLimitCoordinator,
        key: str,
        max_wait: float | None,
    ) -> RateLimitResult | None:
        """Await out an active 429 cooldown, bounded by ``max_wait``. Fail-open.

        Returns the coordinator's result, or ``None`` when the coordinator
        itself failed — a coordinator that is down degrades to inert (proceed
        without waiting) rather than failing the business call. A *deferral*
        is not a fault: it is returned as a normal result for the loop to act
        on. Cancellation propagates untouched.
        """
        try:
            return await coordinator.await_if_needed(key, max_wait=max_wait)
        except asyncio.CancelledError:
            raise
        except Exception as coordinator_error:
            logger.warning(
                "retry.rate_limit_wait_failed",
                error=str(coordinator_error),
                domain=self._domain,
            )
            return None

    async def _aobserve_attempt_outcome(
        self,
        coordinator: RateLimitCoordinator | None,
        key: str,
        outcome: Any,
        scope: OutboundObservationScope | None,
    ) -> bool:
        """Classify one attempt's outcome once and fan a 429 out; report if it was one.

        Every attempt outcome passes through here exactly once — a raised
        exception, an accepted value, a rejected value — and is marked on the
        scope, so the breaker stage above never classifies an object an
        attempt already answered for. Fail-open around the whole fan-out:
        neither the classifier reading caller-supplied attributes nor a
        coordinator fault may replace the business outcome.
        """
        if coordinator is None and scope is None:
            return False
        try:
            return await self._anotify_rate_limit_cooldown(
                coordinator, key, outcome, scope
            )
        except asyncio.CancelledError:
            raise
        except Exception as coordinator_error:
            logger.warning(
                "retry.rate_limit_cooldown_notify_failed",
                error=str(coordinator_error),
                domain=self._domain,
            )
            return False

    @staticmethod
    async def _anotify_rate_limit_cooldown(
        coordinator: RateLimitCoordinator | None,
        key: str,
        subject: Any,
        scope: OutboundObservationScope | None,
    ) -> bool:
        """Set a cooldown when ``subject`` is a 429; report whether it was one.

        The classification is pure and runs inline. Both halves of a 429's
        fan-out leave the loop: the cascade note reaches the breaker's tracker
        (a network write when distributed tracking is on) and can trip the
        breaker — a repository write plus an event publish — so it is hopped
        to a worker thread, and the cooldown write is awaited through the
        coordinator's worker-thread twin because it also publishes to the
        event bus. Whoever classified the outcome first owns its cooldown: an
        outcome an inner surface already marked on the scope is detected for
        the signal only — no second cascade note, no second cooldown.
        """
        from baldur.services.retry_handler.rate_limit_detection import (
            detect_rate_limit,
        )

        already_classified = scope is not None and scope.was_classified(subject)
        if scope is not None and not already_classified:
            scope.mark_classified(subject)

        is_rate_limited, retry_after = detect_rate_limit(subject)
        if not is_rate_limited:
            return False
        if already_classified:
            return True

        if scope is not None:
            await asyncio.to_thread(scope.note_429, retry_after, subject)

        if coordinator is not None:
            cooldown = await coordinator.aon_rate_limited(
                key=key, retry_after=retry_after
            )
            logger.info(
                "retry.rate_limit_cooldown_set",
                cooldown=cooldown,
            )
        return True

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
        # Outbound 429 observation scope, claimed as the very first statement.
        # This stage carries its own decision about fleet-wide cooldowns in
        # *every* mode — retry disabled, observe-only, and the loop — so the
        # breaker stage above must not install one on its behalf (sync parity).
        scope = self._observation_scope()
        if scope is not None:
            scope.claim_coordination()

        _unwrapped = func
        while isinstance(_unwrapped, functools.partial):
            _unwrapped = _unwrapped.func
        is_async = asyncio.iscoroutinefunction(_unwrapped)

        # Global master toggle: when retry is disabled, run the function once via
        # the single-attempt path (no re-execution), mirroring the synchronous
        # RetryPolicy. Placed ahead of the observe-only guard, matching sync's
        # order — both paths coordinate nothing and record via _single_attempt.
        if not self._globally_enabled:
            return await self._single_attempt(func, is_async, *args, **kwargs)

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

        # Terminal observability helpers — shared with the sync RetryPolicy so a
        # payload/label change cannot drift between the two policies. Lazy
        # def-body import: the resilience -> services direction is acyclic, and a
        # def-body import forms no import-time cycle. It also keeps the
        # bus/metrics source-module test patches intercepting. Imported here,
        # past the single-attempt guards, so the disabled/observe-only paths
        # (which record via _single_attempt) do not pay for it.
        from baldur.services.retry_handler.observability import (
            REASON_TO_OUTCOME,
            emit_retry_exhausted_event,
            record_retry_attempt_started,
            record_retry_outcome,
        )

        # Outbound 429 coordination, resolved once per call and, like the sync
        # stage, only past the two suppression returns above: both take the
        # single-attempt path, which uses no coordinator, so resolving earlier
        # would build the coordinator singleton on paths that never use it.
        coordinator = await self._resolve_rate_limit_coordinator()
        rate_limit_key = self._coordination_key()
        # on_success costs a storage read (plus a reset write when a counter is
        # standing), so it is owed only once this call has actually observed a
        # rate-limit signal — a detected 429, an honored cooldown wait, or a
        # coordinator that reported one.
        rate_limit_signal = False
        not_before: float | None = None

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

        # Execution-local, never assigned back to ``self`` — one concurrent
        # execution must not advance another's backoff state.
        backoff = self._backoff_factory()

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

            # Timely retry-pressure series — the attempt is admitted from here
            # on, so a refused iteration above records nothing. ``attempt`` is
            # 0-indexed on this surface; the helper's contract is 1-based.
            record_retry_attempt_started(self._domain, attempt + 1)

            # Rate limit wait (optional), bounded by whatever budget is left.
            # A cooldown longer than the remaining budget is deferred rather
            # than slept: sleeping it would blow the budget and the attempt
            # would be aborted afterwards anyway. The wait is an asyncio.sleep,
            # so it costs this request its latency and the loop nothing.
            if coordinator:
                rl_bound = (
                    None
                    if budget is None
                    else max(0.0, budget - (time.monotonic() - start))
                )
                rl_result = await self._await_rate_limit_cooldown(
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
                if is_async:
                    result = await func(*args, **kwargs)  # type: ignore[misc]
                else:
                    result = await asyncio.to_thread(func, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A cooldown deferral raised by an inner surface is not a
                # dependency call: the breaker counts no request for it, and
                # counting one here would inflate the cascade denominator.
                inner_deferral = isinstance(e, RateLimitDeferredError)
                if scope is not None and not inner_deferral:
                    scope.note_attempt()
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

                # 429 detected → feed the cascade and request a cooldown from
                # the coordinator. Fail-open: a fault here must never replace
                # the business error that is being classified below.
                if await self._aobserve_attempt_outcome(
                    coordinator, rate_limit_key, e, scope
                ):
                    rate_limit_signal = True

                # Non-retryable check first (CB-open, etc.). The attempts bound
                # is hoisted to the shared tail so an out-of-attempts stop is
                # attributed to ``max_attempts``, not ``non_retryable``. An
                # inner deferral exits with the defer vocabulary instead: the
                # call was never made, and ``not_before`` is what a
                # requeue-capable caller acts on.
                if isinstance(e, self._non_retryable):
                    if isinstance(e, RateLimitDeferredError):
                        reason = "rate_limit_deferred"
                        not_before = e.not_before
                    else:
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
                if scope is not None:
                    scope.note_attempt()
                # Function returned — evaluate the result predicate (fail-open).
                if not self._evaluate_result_rejected(result):
                    # A client that hands its 429 back instead of raising is
                    # still rate-limited: classify the accepted value too, and
                    # never treat it as the reset a real success would be.
                    if await self._aobserve_attempt_outcome(
                        coordinator, rate_limit_key, result, scope
                    ):
                        rate_limit_signal = True
                    # Fail-open: a coordinator fault must never destroy a
                    # successful business result.
                    elif coordinator and rate_limit_signal:
                        try:
                            await coordinator.aon_success(rate_limit_key)
                        except asyncio.CancelledError:
                            raise
                        except Exception as coordinator_error:
                            logger.warning(
                                "retry.rate_limit_success_notify_failed",
                                error=str(coordinator_error),
                                domain=self._domain,
                            )
                    record_retry_outcome(self._domain, attempt + 1, "success")
                    return PolicyResult(
                        value=result,
                        outcome=PolicyOutcome.SUCCESS,
                        total_attempts=attempt + 1,
                        executed_policies=["retry"],
                    )
                # Soft failure: treat the rejected value like a retryable
                # exception; no exception is raised, so last_error stays None and
                # exhaustion synthesizes a MaxRetriesExceededError.
                if await self._aobserve_attempt_outcome(
                    coordinator, rate_limit_key, result, scope
                ):
                    rate_limit_signal = True
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
            delay = backoff.calculate(attempt + 1, context=context)

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

        # Cooldown-deferral exits are synthesized FIRST, ahead of the
        # result-rejection branch below (sync parity): ``last_error is None``
        # does not imply "attempt 1" — a rejected result sets it to None on
        # every attempt, and the deferral is the actual exit cause.
        if reason == "rate_limit_deferred" and last_error is None:
            # The coordination key, not the domain: they diverge whenever
            # ``rate_limit_key`` overrides, and the deferral was computed
            # against the former.
            last_error = RateLimitDeferredError(
                key=rate_limit_key,
                not_before=not_before,
            )

        # Result-rejection exits leave last_error=None; synthesize a first-class
        # exhaustion error so DLQ / @retry have a real exception and the composer
        # does not misclassify FAILURE(error=None) as REJECTED.
        elif last_error is None and result_rejected:
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
            if scope is not None:
                # This loop synthesised the object it is about to propagate,
                # and already classified the value behind it. Marking it keeps
                # the breaker stage from classifying it a second time — its
                # message carries the domain name, so a name containing
                # "throttle" or "429" would otherwise read as a fresh
                # rate-limit answer.
                scope.mark_classified(last_error)

        logger.warning(
            "retry.async_exhausted",
            func=func_name,
            max_retries=self._max_retries,
            error=str(last_error),
            reason=reason,
        )

        # Terminal observability (parity with the sync RetryPolicy). The bus
        # emit is offloaded to a thread: EventBus.publish is synchronous and
        # blocking (it waits on each subscriber's handler), so a bare call from
        # this coroutine would park the event loop and stall every other request
        # on this worker. asyncio.to_thread restores exact sync semantics — the
        # emitting call pays the handler cost, its neighbours do not. The metric
        # recorder is a bounded in-process counter increment with no I/O, so a
        # thread hop would cost more than the work it defers — it stays direct.
        elapsed = time.monotonic() - start
        await asyncio.to_thread(
            emit_retry_exhausted_event,
            domain=self._domain,
            max_attempts=self._max_retries + 1,
            last_error=last_error,
            attempts=attempt + 1,
            retry_history_length=len(retry_history),
            reason=reason,
            elapsed=elapsed,
            budget=budget,
            context=context,
        )
        record_retry_outcome(
            self._domain, attempt + 1, REASON_TO_OUTCOME.get(reason, "exhausted")
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
                # Defer vocabulary for requeue-capable callers (Celery/DLQ):
                # present only on a cooldown deferral. The key rides along so
                # a decorator can synthesise the deferral error when the loop
                # kept a real last error in its place.
                **(
                    {"not_before": not_before, "rate_limit_key": rate_limit_key}
                    if reason == "rate_limit_deferred"
                    else {}
                ),
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

        Used by the globally-disabled and observe-only paths — executes the
        business call exactly once and never re-executes. Mirrors the
        synchronous ``RetryPolicy._single_attempt``: it records the terminal
        outcome to the Prometheus retry series but emits **no** bus event (a
        single attempt is not an exhaustion), and the FAILURE result carries no
        ``should_dlq`` so the downstream DLQ sink stays observe-only.
        ``asyncio.CancelledError`` re-raises without recording — a cancellation
        is not a terminal (sync parity, by the exception hierarchy). The
        attempt start is recorded for the same reason the terminal is: these
        paths contribute the pressure ratio's denominator, so omitting them
        would inflate the retry share wherever retries run disabled.
        """
        from baldur.services.retry_handler.observability import (
            record_retry_attempt_started,
            record_retry_outcome,
        )

        record_retry_attempt_started(self._domain, 1)
        try:
            if is_async:
                result = await func(*args, **kwargs)  # type: ignore[misc]
            else:
                result = await asyncio.to_thread(func, *args, **kwargs)
            record_retry_outcome(self._domain, 1, "success")
            return PolicyResult(
                value=result,
                outcome=PolicyOutcome.SUCCESS,
                total_attempts=1,
                executed_policies=["retry"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            record_retry_outcome(self._domain, 1, "failure")
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
    """Return the success value, or raise the failure as the caller should see it.

    A cooldown deferral is raised as ``RateLimitDeferredError`` — never wrapped
    as an exhaustion, which would read "max retries exceeded" for a call that
    made zero attempts and bury ``not_before`` one level down. Two shapes reach
    here: the loop's own deferral (or an inner surface's, passed through as a
    non-retryable exit) already *is* the error, and is re-raised as-is by type
    — independent of metadata, because the retry-disabled and observe-only
    single-attempt paths return a FAILURE with no metadata at all; and a
    deferral that followed a real failure on an earlier attempt keeps that
    failure as ``result.error`` (the breaker must keep counting it), so the
    deferral is synthesised from the metadata with the earlier error as its
    ``__cause__``.

    Double-wrap guard: result-predicate exhaustion already synthesized a
    ``MaxRetriesExceededError`` (carrying ``last_result`` / ``result_rejected``)
    — re-raise it as-is rather than nesting it inside a second one. Shared by the
    ``@retry`` sync and async wrappers.
    """
    from baldur.services.retry_handler.models import MaxRetriesExceededError

    if result.success:
        return result.value
    error = result.error
    if isinstance(error, RateLimitDeferredError):
        raise error
    metadata = result.metadata or {}
    if metadata.get("reason") == "rate_limit_deferred":
        raise RateLimitDeferredError(
            key=metadata.get("rate_limit_key", ""),
            not_before=metadata.get("not_before"),
        ) from error
    if isinstance(error, MaxRetriesExceededError):
        raise error
    raise MaxRetriesExceededError(
        f"Max retries exceeded for {func_name}",
        retry_count=result.total_attempts,
        max_retries=max_attempts,
        last_error=error,
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

    Replaces the previous split retry decorators (one for ``def``, one for
    ``async def``) with a single call-style-safe surface. Both branches derive
    their configuration from ``RetryPolicyConfig.from_settings(domain)`` with
    the passed overrides applied:

    - An ``async def`` is wrapped by :class:`AsyncRetryPolicy`.
    - A plain ``def`` is wrapped by the synchronous ``RetryPolicy``.

    On exhaustion, both branches raise ``MaxRetriesExceededError`` (carrying
    ``last_error``); success returns the unwrapped value. A call refused by a
    shared 429 cooldown that outlasts the wait budget raises
    ``RateLimitDeferredError`` instead — the function was never called, and
    ``not_before`` says when it may be. ``functools.wraps`` preserves the
    wrapped signature, so framework dependency injection (e.g. FastAPI
    ``Depends``) resolves against the original parameters.

    Outbound 429 coordination applies to **both** branches, and only when
    ``domain`` is set. A function with a named domain shares a cooldown with
    every other caller on that domain, so a 429 backs the fleet off together
    instead of each worker retrying on its own ladder. Leaving ``domain``
    unset opts out — the placeholder is shared by every unnamed caller, and
    one cooldown record cannot stand for unrelated downstreams. On an
    ``async def`` the cooldown wait is an ``asyncio.sleep``: it costs the
    call its latency and never stalls the event loop.

    Args:
        domain: Configuration domain (also the retry / DLQ / metric key, and
            the outbound 429 coordination key — see above).
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
                # Dynamic dispatch: mypy cannot track R through the Any-returning
                # unwrap helper (see the return-value ignore below, same cause).
                return _unwrap_or_raise(result, func.__name__, config.max_attempts)  # type: ignore[no-any-return]

            return async_wrapper  # type: ignore[return-value]

        from baldur.services.retry_handler.policy import RetryPolicy

        sync_policy = RetryPolicy(config=config, backoff=backoff)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            result = sync_policy.execute(func, *args, **kwargs)
            # Dynamic dispatch: mypy cannot track R through the Any-returning
            # unwrap helper (parallels the async wrapper above).
            return _unwrap_or_raise(result, func.__name__, config.max_attempts)  # type: ignore[no-any-return]

        return sync_wrapper

    return decorator


__all__ = [
    "AsyncRetryPolicy",
    "async_retry_policy",
    "retry",
]
