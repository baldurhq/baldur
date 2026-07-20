"""
Policy Composer — engine for composing multiple ResiliencePolicy declaratively.

Builds the pipeline in this order: Guard (pre-check) → Policy chain (nested
wrapping) → Hook (event observation) → Sink (terminal failure handling).

Sync/async split:
- PolicyComposer: accepts sync ResiliencePolicy only
- AsyncPolicyComposer: accepts async AsyncResiliencePolicy only
  Same pattern as the existing SemaphoreBulkhead/AsyncSemaphoreBulkhead and
  BulkheadPolicy/AsyncBulkheadPolicy splits.

Convenience functions:
- compose(): compose sync policies
- compose_async(): compose async policies

Hook observation scope — 2-layer structure:
- Composer Hook: observes only the end-to-end pipeline result
- Inside a Policy: its own logic, or nothing (per-attempt retry events etc.
  are handled by the Policy)

Sink handling:
- Runs synchronously (blocking), per the FailureSink Protocol
- A DLQ write is a local DB write, so it costs a few ms

Preventing duplicate FallbackPolicy execution:
- Inside the composer chain, _apply_fallback() is called instead of execute()
- Guarantees func runs exactly once
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

import structlog

from baldur.core.exceptions import TimeoutPolicyError
from baldur.core.execution_mode import get_execution_mode
from baldur.interfaces.resilience_policy import (
    AsyncFailureSink,
    AsyncPolicyGuard,
    AsyncPolicyHook,
    AsyncResiliencePolicy,
    FailureSink,
    GuardResult,
    PolicyContext,
    PolicyGuard,
    PolicyHook,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
    ResiliencePolicy,
)

logger = structlog.get_logger()

T = TypeVar("T")


# =============================================================================
# Sync→async offload adapters (D2/D3)
#
# AsyncPolicyComposer normalizes every guard/hook/sink to its async Protocol at
# add-time: a native-async impl passes through (awaited with zero thread hop),
# a sync impl is wrapped in one of these thin adapters that satisfy the async
# Protocol by offloading the sync call off the event loop via
# ``asyncio.to_thread``. ``execute`` then uniformly ``await``s every channel.
# The sink adapter is the live production consumer of ``AsyncFailureSink`` (the
# sync DLQ store, #446) — so the async Protocol surface is fully symmetric AND
# every member has a real implementation (claim↔wiring integrity).
# =============================================================================


class _SyncGuardToAsyncAdapter:
    """Wrap a sync :class:`PolicyGuard` as an :class:`AsyncPolicyGuard`."""

    def __init__(self, guard: PolicyGuard) -> None:
        self._guard = guard

    @property
    def name(self) -> str:
        return self._guard.name

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        return await asyncio.to_thread(self._guard.check, context=context)


class _SyncHookToAsyncAdapter:
    """Wrap a sync :class:`PolicyHook` as an :class:`AsyncPolicyHook`."""

    def __init__(self, hook: PolicyHook) -> None:
        self._hook = hook

    async def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        await asyncio.to_thread(
            self._hook.on_execute, policy_name, attempt, context=context
        )

    async def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._hook.on_success, policy_name, result, context=context
        )

    async def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._hook.on_failure, policy_name, error, attempt, context=context
        )

    async def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._hook.on_retry, policy_name, attempt, delay, context=context
        )

    async def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        await asyncio.to_thread(
            self._hook.on_reject, guard_name, reason, context=context
        )


class _SyncSinkToAsyncAdapter:
    """Wrap a sync :class:`FailureSink` as an :class:`AsyncFailureSink`.

    The live production consumer of ``AsyncFailureSink``: the DLQ store stays
    sync (#446), so on the async path it is offloaded here. When a native async
    DLQ store lands it implements ``AsyncFailureSink`` directly and passes
    through normalization with no composer change.
    """

    def __init__(self, sink: FailureSink) -> None:
        self._sink = sink

    async def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        return await asyncio.to_thread(
            self._sink.handle_failure,
            error=error,
            context=context,
            policy_result=policy_result,
        )


def _normalize_guard(guard: PolicyGuard | AsyncPolicyGuard) -> AsyncPolicyGuard:
    """Pass a native-async guard through; wrap a sync guard for offload.

    Discriminates on ``iscoroutinefunction(guard.check)`` rather than
    ``isinstance(guard, AsyncPolicyGuard)`` — a ``runtime_checkable`` Protocol
    only checks method *presence*, and a sync guard also has a ``check`` method,
    so the coroutine-ness of the method is the reliable async signal.
    """
    if asyncio.iscoroutinefunction(guard.check):
        return guard  # type: ignore[return-value]
    return _SyncGuardToAsyncAdapter(guard)  # type: ignore[arg-type]


def _normalize_hook(hook: PolicyHook | AsyncPolicyHook) -> AsyncPolicyHook:
    """Pass a native-async hook through; wrap a sync hook for offload."""
    if asyncio.iscoroutinefunction(hook.on_success):
        return hook  # type: ignore[return-value]
    return _SyncHookToAsyncAdapter(hook)  # type: ignore[arg-type]


def _normalize_sink(sink: FailureSink | AsyncFailureSink) -> AsyncFailureSink:
    """Pass a native-async sink through; wrap a sync sink for offload."""
    if asyncio.iscoroutinefunction(sink.handle_failure):
        return sink  # type: ignore[return-value]
    return _SyncSinkToAsyncAdapter(sink)  # type: ignore[arg-type]


class _FallbackApplied(BaseException):
    """Composer-internal signal for Fallback application.

    Inherits BaseException (not Exception) so that RetryPolicy's
    ``except Exception`` does not catch this signal. This ensures
    _FallbackApplied propagates directly to Composer's final handler
    regardless of policy ordering. Same pattern as Python's GeneratorExit.

    Placement-relative failure counting: the signal only bypasses the
    failure counting of policies that sit OUTSIDE the fallback stage. When
    the facade places Fallback outermost, CB/Timeout/Retry all sit inside
    the fallback wrapper, so each records its own failure (CB.record_failure
    via its normal ``except Exception`` path) BEFORE the wrapper absorbs the
    error — the absorbed failure is fully counted. Only a policy layered
    outside the fallback would have its counting bypassed; the facade layers
    none. Metadata propagation deliberately uses the separate closure-variable
    mechanism (not this signal), so inner policies stay observable to their
    own ``except Exception`` handlers like services/circuit_breaker/policy.py.
    """

    def __init__(self, result: PolicyResult) -> None:
        self.result = result
        super().__init__("Fallback applied")


def _classify_exception_outcome(error: BaseException) -> PolicyOutcome:
    """Classify a chain exception into its terminal PolicyOutcome.

    Single source shared by (i) the composer terminal catch ladders, (ii) the
    fallback wrappers' synthesized predicate input, and (iii) the standalone
    ``FallbackPolicy.execute`` predicate input. A ``PolicyRejectedException``
    (including ``CircuitBreakerOpenError`` / ``BulkheadFullError``) maps to
    REJECTED; a ``TimeoutPolicyError`` to TIMEOUT; anything else to FAILURE.
    The two matched branches are disjoint, so their relative order is
    irrelevant.
    """
    if isinstance(error, PolicyRejectedException):
        return PolicyOutcome.REJECTED
    if isinstance(error, TimeoutPolicyError):
        return PolicyOutcome.TIMEOUT
    return PolicyOutcome.FAILURE


def _fallback_source(metadata: dict[str, Any]) -> str | None:
    """Derive the served fallback's source label from its result metadata.

    ``fallback_fn`` / ``default_value`` paths carry an explicit
    ``fallback_source``; a chain entry carries only its ``fallback_index``,
    rendered here as ``chain[<i>]``.
    """
    src = metadata.get("fallback_source")
    if src is not None:
        return src
    idx = metadata.get("fallback_index")
    if idx is not None:
        return f"chain[{idx}]"
    return None


def _build_failure_result(
    outcome: PolicyOutcome,
    error: Exception,
    executed_policies: list[str],
    metadata: dict[str, Any],
    total_attempts: int = 1,
) -> PolicyResult:
    """Build the terminal failure-path PolicyResult.

    Centralizes outer catch-branch construction so every failure terminal
    (REJECTED / TIMEOUT / FAILURE) propagates ``executed_policies``, the
    accumulated ``chain_metadata`` from inner-policy ``PolicyResult.metadata``,
    and the chain-wide ``total_attempts`` (max across executed stages, so a
    retry-exhausted failure reports the real attempt count instead of 1).
    Symmetric to the success-path returns inside the chain executors.
    """
    return PolicyResult(
        value=None,
        outcome=outcome,
        error=error,
        executed_policies=list(reversed(executed_policies)),
        metadata=dict(metadata),
        total_attempts=total_attempts,
    )


def _merge_chain_metadata(
    chain_metadata: dict[str, Any],
    incoming: dict[str, Any] | None,
    policy_name: str,
) -> None:
    """Merge an inner policy's metadata into the chain accumulator.

    Last-write-wins on collision; emits ``policy_chain.metadata_collision``
    warning so the first real collision is operationally observable. The
    long-term migration to namespaced metadata is tracked in 466 OOS F8.
    """
    if not incoming:
        return
    for k, v in incoming.items():
        if k in chain_metadata and chain_metadata[k] != v:
            logger.warning(
                "policy_chain.metadata_collision",
                key=k,
                old=chain_metadata[k],
                new=v,
                policy=policy_name,
            )
        chain_metadata[k] = v


def _trace_structural_control(policy_name: str, result: PolicyResult) -> None:
    """Surface a live structural control in the observe-only (dry-run) trace.

    Complement to ``intervention_suppressed``: under observe-only the automatic
    *healing* interventions (CB / retry / DLQ) suppress their side-effects, but a
    *structural* control — e.g. a bulkhead concurrency ceiling — stays live by
    design. Its reject answers *current real resource occupancy*, not a
    simulatable failure-history decision, so suppressing it would admit calls past
    the ceiling and uncap concurrency, turning observe-only into a self-inflicted
    overload. The reject/timeout is therefore enforced even under dry-run; this
    logs it so the live block is visible in the trace alongside the suppressed
    interventions instead of a silent gap. Observation only — the control itself
    is unchanged. The non-success outcome is checked first, so the success path
    never resolves the execution mode.
    """
    if result.outcome not in (PolicyOutcome.REJECTED, PolicyOutcome.TIMEOUT):
        return
    if get_execution_mode().should_execute:
        return
    logger.info(
        "execution_mode.structural_control_enforced",
        policy=policy_name,
        outcome=result.outcome.value,
        state=(result.metadata or {}).get("state"),
    )


# =============================================================================
# PolicyComposer — sync Policy composition engine
# =============================================================================


class PolicyComposer(Generic[T]):
    """
    Sync Policy composition engine.

    Composes multiple ResiliencePolicy declaratively into a single execution
    pipeline, wiring Guard/Hook/Sink to integrate with the infrastructure layer.

    Execution order:
    1. Guard checks (Kill Switch, ErrorBudgetGate, etc.)
    2. Policies wrapped in turn (add order = outer→inner execution order)
    3. Hooks invoked (Audit, Metrics, etc.) — observe only the whole-pipeline
       result
    4. Sink handling on failure (DLQ, etc.) — synchronous, blocking

    Type safety:
    - Only ResiliencePolicy (sync) can be added
    - Adding an AsyncResiliencePolicy raises TypeError at runtime
    """

    def __init__(self) -> None:
        self._policies: list[ResiliencePolicy] = []
        self._guards: list[PolicyGuard] = []
        self._hooks: list[PolicyHook] = []
        self._sinks: list[FailureSink] = []

    # === Builder API ===

    def add(self, policy: ResiliencePolicy) -> PolicyComposer[T]:
        """Add a Policy. Add order is the outer→inner execution order."""
        if isinstance(policy, AsyncResiliencePolicy) and not isinstance(
            policy, ResiliencePolicy
        ):
            raise TypeError(
                f"Cannot add async policy '{policy.name}' to sync PolicyComposer. "
                f"Use AsyncPolicyComposer or compose_async() instead."
            )
        self._policies.append(policy)
        return self

    def add_guard(self, guard: PolicyGuard) -> PolicyComposer[T]:
        """Add a Guard. Checked before any Policy runs."""
        self._guards.append(guard)
        return self

    def add_hook(self, hook: PolicyHook) -> PolicyComposer[T]:
        """Add a Hook. Observes Policy execution events."""
        self._hooks.append(hook)
        return self

    def add_sink(self, sink: FailureSink) -> PolicyComposer[T]:
        """Add a FailureSink. Handles the terminal failure."""
        self._sinks.append(sink)
        return self

    # === Execution ===

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the composed Policy pipeline.

        Execution flow:
        1. Guard checks → REJECTED if any guard denies
        2. Policy chain execution (outer→inner nesting)
        3. Hook invocation (on_success / on_failure / on_reject)
        4. Sink handling on failure — synchronous, blocking

        Args:
            func: the function to execute
            *args: positional arguments for the function
            context: execution context (propagated to Guard/Hook/Sink).
                     If None, Guards check global state only and Sinks store
                     without a business identifier.
            **kwargs: keyword arguments for the function

        Returns:
            PolicyResult[T]: the composite result. Never raises.
        """
        start_time = time.perf_counter()

        # Step 1: Guard checks
        for guard in self._guards:
            try:
                guard_result = guard.check(context=context)
                if not guard_result.allowed:
                    self._notify_hooks_reject(
                        guard.name, guard_result.reason or "", context=context
                    )
                    return PolicyResult(
                        value=None,
                        outcome=PolicyOutcome.REJECTED,
                        # Propagate the guard's own metadata (e.g. the
                        # idempotency decision + key) so the facade can build a
                        # precise reject exception. Composer-owned keys win on
                        # collision.
                        metadata={
                            **guard_result.metadata,
                            "rejected_by": guard.name,
                            "reason": guard_result.reason,
                        },
                    )
            except Exception as e:
                # Fail-open: a failing Guard lets the call through
                logger.warning(
                    "policy_composer.guard_execution_failed",
                    guard_name=guard.name,
                    error=str(e),
                    mode="fail-open",
                )

        # Step 2: Policy chain execution
        result = self._execute_policy_chain(func, *args, context=context, **kwargs)

        # Step 3: Hook invocation — observes only the whole-pipeline result
        duration_ms = (time.perf_counter() - start_time) * 1000
        result.total_duration_ms = duration_ms

        if result.success:
            self._notify_hooks_success(result, context=context)
        else:
            self._notify_hooks_failure(result, context=context)

            # Step 4: Sink handling — synchronous, blocking
            if result.outcome == PolicyOutcome.FAILURE:
                self._process_sinks(result, context, args, kwargs)

        return result

    # === Policy Chain ===

    def _execute_policy_chain(  # noqa: C901
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the Policy chain as nested wrappers.

        For policies = [P1, P2, P3]:
        P1.execute(lambda: P2.execute(lambda: P3.execute(func)))

        FallbackPolicy special case:
        - Calls _apply_fallback() instead of execute() (prevents running func
          twice)
        - _apply_fallback() is a Composer-only internal API
        """
        from baldur.resilience.policies.fallback import FallbackPolicy

        if not self._policies:
            # No Policy → run directly
            try:
                value = func(*args, **kwargs)
                return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)
            except Exception as e:
                return PolicyResult(value=None, outcome=PolicyOutcome.FAILURE, error=e)

        # Build the nested execution (wrap in reverse order).
        def wrapped() -> T:
            return func(*args, **kwargs)

        executed_policies: list[str] = []
        # Closure-shared metadata accumulator. Each inner policy_wrapper
        # merges its PolicyResult.metadata here BEFORE returning the success
        # value or re-raising the error. The terminal branches below build the
        # outer PolicyResult with metadata=chain_metadata.
        chain_metadata: dict[str, Any] = {}
        # Closure-shared attempt accumulator (max across executed stages). Only
        # the retry stage reports total_attempts > 1; every other stage reports
        # 1, so ``max`` yields the real attempt count and every terminal reads a
        # truthful value regardless of fallback presence.
        chain_attempts = 1

        for policy in reversed(self._policies):
            outer_fn = wrapped
            current_policy = policy

            if isinstance(current_policy, FallbackPolicy):
                # FallbackPolicy: conditional wrapper on top of _apply_fallback().
                # Guarantees func runs once (reuses inner()'s outcome) and
                # signals SUCCESS_WITH_FALLBACK via _FallbackApplied.
                fb_policy_narrowed: FallbackPolicy = current_policy

                def fallback_wrapper(
                    inner: Callable = outer_fn, fb: FallbackPolicy = fb_policy_narrowed
                ) -> T:
                    try:
                        return inner()
                    except _FallbackApplied:
                        raise  # propagate an inner FallbackPolicy's signal as-is
                    except Exception as e:
                        # Check the predicate against the TRUE classified outcome
                        # (REJECTED for CB-open, TIMEOUT for a timeout), then call
                        # _apply_fallback directly with the absorbed error.
                        check_result = PolicyResult(
                            value=None,
                            outcome=_classify_exception_outcome(e),
                            error=e,
                        )
                        if fb._predicate(check_result):
                            fb_result = fb._apply_fallback(
                                original_error=e,
                                context=context,
                            )
                            if fb_result.success:
                                raise _FallbackApplied(fb_result) from e
                        raise

                wrapped = fallback_wrapper
            else:
                # Regular Policy: wrap via execute().
                def policy_wrapper(
                    inner: Callable = outer_fn, p: ResiliencePolicy = current_policy
                ) -> T:
                    nonlocal chain_attempts
                    result = p.execute(inner, context=context)
                    # Merge BEFORE the success branch so both success-return
                    # and failure-raise paths contribute to chain_metadata.
                    _merge_chain_metadata(chain_metadata, result.metadata, p.name)
                    chain_attempts = max(chain_attempts, result.total_attempts)
                    _trace_structural_control(p.name, result)
                    if result.success:
                        return result.value  # type: ignore[return-value]
                    if result.error:
                        raise result.error
                    raise PolicyRejectedException(
                        f"Policy '{p.name}' rejected: {result.outcome}"
                    )

                wrapped = policy_wrapper

            executed_policies.append(current_policy.name)

        # Final execution.
        try:
            value = wrapped()
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=list(reversed(executed_policies)),
                metadata=dict(chain_metadata),
                total_attempts=chain_attempts,
            )
        except _FallbackApplied as fa:
            # Fallback applied — propagate SUCCESS_WITH_FALLBACK. Under the
            # facade's fallback-outermost order the inner stages populate
            # chain_metadata (attempt counts, CB state) before the fallback
            # absorbs; merge it under fb_result.metadata (fallback keys win).
            fb_result: PolicyResult = fa.result
            original_error = fa.__cause__
            logger.warning(
                "policy_chain.fallback_applied",
                error_type=(
                    type(original_error).__name__
                    if original_error is not None
                    else None
                ),
                error=str(original_error) if original_error is not None else None,
                fallback_source=_fallback_source(fb_result.metadata),
            )
            return PolicyResult(
                value=fb_result.value,
                outcome=fb_result.outcome,
                error=fb_result.error,
                executed_policies=list(reversed(executed_policies)),
                metadata={**chain_metadata, **fb_result.metadata},
                total_attempts=chain_attempts,
            )
        except Exception as e:
            return _build_failure_result(
                _classify_exception_outcome(e),
                e,
                executed_policies,
                chain_metadata,
                chain_attempts,
            )

    # === Hook Notification ===

    def _notify_hooks_success(
        self, result: PolicyResult, context: PolicyContext | None = None
    ) -> None:
        """On success, call on_success on every Hook (fail-open)."""
        for hook in self._hooks:
            try:
                hook.on_success("composer", result, context=context)
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    def _notify_hooks_failure(
        self, result: PolicyResult, context: PolicyContext | None = None
    ) -> None:
        """On failure, call on_failure on every Hook (fail-open)."""
        for hook in self._hooks:
            try:
                hook.on_failure(
                    "composer",
                    result.error or Exception("Unknown"),
                    result.total_attempts,
                    context=context,
                )
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    def _notify_hooks_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """On rejection, call on_reject on every Hook (fail-open)."""
        for hook in self._hooks:
            try:
                hook.on_reject(guard_name, reason, context=context)
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    # === Sink Processing ===

    def _process_sinks(
        self,
        result: PolicyResult,
        context: PolicyContext | None,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """Deliver the terminal failure to every Sink (synchronous, blocking)."""
        if result.error is None:
            return

        for sink in self._sinks:
            try:
                sink_id = sink.handle_failure(
                    error=result.error,
                    context=context,
                    policy_result=result,
                )
                if sink_id is not None:
                    result.metadata["sink_id"] = sink_id
            except Exception as e:
                logger.warning(
                    "sink.failed",
                    error=e,
                )


# =============================================================================
# AsyncPolicyComposer — async Policy composition engine
# =============================================================================


class AsyncPolicyComposer(Generic[T]):
    """
    Async Policy composition engine.

    Only AsyncResiliencePolicy is accepted, blocking sync/async mixing at the
    type level. Provides the same Guard/Hook/Sink integration as
    PolicyComposer, asynchronously.

    Each guard/hook/sink is normalized to its async Protocol at ``add_*`` time
    (D2): a native-async impl (its method is a coroutine) passes through and is
    awaited with zero thread hop; a sync impl is wrapped in a thin
    ``to_thread``-offload adapter that satisfies the async Protocol. ``execute``
    then uniformly ``await``s all three channels — one coherent mechanism instead
    of per-channel ``to_thread`` calls. Empty channels are skipped, so a
    channel-less pipeline pays zero thread hops. The idempotency guard/hook run
    natively (no thread hop); the sync DLQ sink is wrapped in the
    ``AsyncFailureSink`` offload adapter (D3).
    """

    def __init__(self) -> None:
        self._policies: list[AsyncResiliencePolicy] = []
        self._guards: list[AsyncPolicyGuard] = []
        self._hooks: list[AsyncPolicyHook] = []
        self._sinks: list[AsyncFailureSink] = []

    # === Builder API ===

    def add(self, policy: AsyncResiliencePolicy) -> AsyncPolicyComposer[T]:
        """Add an async Policy. Add order is the outer→inner execution order."""
        self._policies.append(policy)
        return self

    def add_guard(
        self, guard: PolicyGuard | AsyncPolicyGuard
    ) -> AsyncPolicyComposer[T]:
        """Add a Guard (add-time normalize: native async pass-through / sync wrap)."""
        self._guards.append(_normalize_guard(guard))
        return self

    def add_hook(self, hook: PolicyHook | AsyncPolicyHook) -> AsyncPolicyComposer[T]:
        """Add a Hook (add-time normalize: native async pass-through / sync wrap)."""
        self._hooks.append(_normalize_hook(hook))
        return self

    def add_sink(self, sink: FailureSink | AsyncFailureSink) -> AsyncPolicyComposer[T]:
        """Add a FailureSink (add-time normalize: async pass-through / sync wrap)."""
        self._sinks.append(_normalize_sink(sink))
        return self

    # === Execution ===

    async def execute(
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the async Policy pipeline.

        Every guard/hook/sink was normalized to its async Protocol at add-time,
        so each channel is ``await``ed uniformly here: a native-async channel
        (e.g. the async idempotency guard/hook) runs with ZERO thread hop; a
        wrapped sync channel (e.g. the sync DLQ sink) offloads via
        ``asyncio.to_thread`` inside its adapter. Empty channels are skipped, so
        a channel-less pipeline pays zero thread hops. Intra-request
        happens-before ordering (guard → chain → hook/sink) is preserved.

        A ``CancelledError`` raised at any ``await`` is a ``BaseException`` and
        escapes the fail-open ``except Exception``, so cancellation still
        propagates. Across concurrent ``execute`` calls two native guard
        ``check``s may run interleaved; exactly-once dedup rests on the
        idempotency acquire being atomic (``AsyncIdempotencyGate`` rejects a
        non-atomic adapter at construction), not on loop-serialization.

        Args:
            func: the async function to execute
            *args: positional arguments for the function
            context: execution context (propagated to Guard/Hook/Sink)
            **kwargs: keyword arguments for the function

        Returns:
            PolicyResult[T]: the composite result. Never raises.
        """
        # verified-by: test_atomic_guard_preserves_exactly_once_under_concurrent_execute
        start_time = time.perf_counter()

        # Guard checks — awaited natively. A native async guard (idempotency)
        # drives the awaitable dedup gate with no thread hop; a wrapped sync
        # guard offloads inside its adapter.
        for guard in self._guards:
            try:
                guard_result = await guard.check(context=context)
                if not guard_result.allowed:
                    if self._hooks:
                        await self._notify_hooks_reject(
                            guard.name,
                            guard_result.reason or "",
                            context=context,
                        )
                    return PolicyResult(
                        value=None,
                        outcome=PolicyOutcome.REJECTED,
                        # Sync-symmetric metadata propagation.
                        metadata={
                            **guard_result.metadata,
                            "rejected_by": guard.name,
                            "reason": guard_result.reason,
                        },
                    )
            except Exception as e:
                # Fail-open: log symmetrically with the sync loop's
                # guard_execution_failed — a guard bypass must not be silent
                # (LOGGING_STANDARDS §3.2).
                logger.warning(
                    "policy_composer.guard_execution_failed",
                    guard_name=guard.name,
                    error=str(e),
                    mode="fail-open",
                )

        # Async Policy chain execution
        result = await self._execute_async_chain(func, *args, context=context, **kwargs)

        # Hook notification — observes only the end-to-end pipeline result.
        duration_ms = (time.perf_counter() - start_time) * 1000
        result.total_duration_ms = duration_ms

        if result.success:
            if self._hooks:
                await self._notify_hooks_success(result, context=context)
        else:
            if self._hooks:
                await self._notify_hooks_failure(result, context=context)

            # Sink processing — only on the FAILURE terminal.
            if result.outcome == PolicyOutcome.FAILURE and self._sinks:
                await self._process_sinks(result, context, args, kwargs)

        return result

    # === Async Policy Chain ===

    async def _execute_async_chain(  # noqa: C901
        self,
        func: Callable[..., Awaitable[T]],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the async Policy chain as nested wrappers.

        AsyncFallbackPolicy special case:
        - Calls _apply_fallback() instead of execute() (prevents running func
          twice)
        """
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        if not self._policies:
            try:
                value = await func(*args, **kwargs)
                return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)
            except Exception as e:
                return PolicyResult(value=None, outcome=PolicyOutcome.FAILURE, error=e)

        # Build the async nested execution (wrap in reverse order).
        async def initial_fn() -> T:
            return await func(*args, **kwargs)

        wrapped: Callable[[], Awaitable[T]] = initial_fn
        executed_policies: list[str] = []
        # Closure-shared accumulators (sync-symmetric): metadata merge + the
        # max-across-stages attempt count. See the sync twin for the rationale.
        chain_metadata: dict[str, Any] = {}
        chain_attempts = 1

        for policy in reversed(self._policies):
            outer_fn = wrapped
            current_policy = policy

            if isinstance(current_policy, AsyncFallbackPolicy):
                fb_policy_narrowed: AsyncFallbackPolicy = current_policy

                async def fallback_wrapper(
                    inner: Callable = outer_fn,
                    fb: AsyncFallbackPolicy = fb_policy_narrowed,
                ) -> T:
                    try:
                        return await inner()
                    except _FallbackApplied:
                        raise  # propagate an inner AsyncFallbackPolicy's signal
                    except Exception as e:
                        # Predicate against the TRUE classified outcome (REJECTED
                        # for CB-open, TIMEOUT for a timeout), then _apply_fallback.
                        check_result = PolicyResult(
                            value=None,
                            outcome=_classify_exception_outcome(e),
                            error=e,
                        )
                        if fb._predicate(check_result):
                            fb_result = await fb._apply_fallback(
                                original_error=e,
                                context=context,
                            )
                            if fb_result.success:
                                raise _FallbackApplied(fb_result) from e
                        raise

                wrapped = fallback_wrapper
            else:

                async def async_policy_wrapper(
                    inner: Callable = outer_fn,
                    p: AsyncResiliencePolicy = current_policy,
                ) -> T:
                    nonlocal chain_attempts
                    result = await p.execute(inner, context=context)
                    _merge_chain_metadata(chain_metadata, result.metadata, p.name)
                    chain_attempts = max(chain_attempts, result.total_attempts)
                    _trace_structural_control(p.name, result)
                    if result.success:
                        return result.value  # type: ignore[return-value]
                    if result.error:
                        raise result.error
                    raise PolicyRejectedException(
                        f"Policy '{p.name}' rejected: {result.outcome}"
                    )

                wrapped = async_policy_wrapper

            executed_policies.append(current_policy.name)

        # Final execution.
        try:
            value = await wrapped()
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.SUCCESS,
                executed_policies=list(reversed(executed_policies)),
                metadata=dict(chain_metadata),
                total_attempts=chain_attempts,
            )
        except _FallbackApplied as fa:
            # Fallback applied — propagate SUCCESS_WITH_FALLBACK. Merge the inner
            # chain_metadata under fb_result.metadata (sync-symmetric; fallback
            # keys win) and log the degraded-mode WARNING once here.
            fb_result: PolicyResult = fa.result
            original_error = fa.__cause__
            logger.warning(
                "policy_chain.fallback_applied",
                error_type=(
                    type(original_error).__name__
                    if original_error is not None
                    else None
                ),
                error=str(original_error) if original_error is not None else None,
                fallback_source=_fallback_source(fb_result.metadata),
            )
            return PolicyResult(
                value=fb_result.value,
                outcome=fb_result.outcome,
                error=fb_result.error,
                executed_policies=list(reversed(executed_policies)),
                metadata={**chain_metadata, **fb_result.metadata},
                total_attempts=chain_attempts,
            )
        except Exception as e:
            return _build_failure_result(
                _classify_exception_outcome(e),
                e,
                executed_policies,
                chain_metadata,
                chain_attempts,
            )

    # === Hook Notification (async — awaits each normalized channel) ===

    async def _notify_hooks_success(
        self, result: PolicyResult, context: PolicyContext | None = None
    ) -> None:
        """On success, await on_success on every Hook (fail-open per hook)."""
        for hook in self._hooks:
            try:
                await hook.on_success("composer", result, context=context)
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    async def _notify_hooks_failure(
        self, result: PolicyResult, context: PolicyContext | None = None
    ) -> None:
        """On failure, await on_failure on every Hook (fail-open per hook)."""
        for hook in self._hooks:
            try:
                await hook.on_failure(
                    "composer",
                    result.error or Exception("Unknown"),
                    result.total_attempts,
                    context=context,
                )
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    async def _notify_hooks_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """On rejection, await on_reject on every Hook (fail-open per hook)."""
        for hook in self._hooks:
            try:
                await hook.on_reject(guard_name, reason, context=context)
            except Exception as e:
                logger.warning(
                    "hook.failed_fail_open",
                    error=e,
                )

    # === Sink Processing (async — awaits each normalized channel) ===

    async def _process_sinks(
        self,
        result: PolicyResult,
        context: PolicyContext | None,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """Deliver the terminal failure to every Sink (fail-open per sink)."""
        if result.error is None:
            return

        for sink in self._sinks:
            try:
                sink_id = await sink.handle_failure(
                    error=result.error,
                    context=context,
                    policy_result=result,
                )
                if sink_id is not None:
                    result.metadata["sink_id"] = sink_id
            except Exception as e:
                logger.warning(
                    "sink.failed",
                    error=e,
                )


# =============================================================================
# Convenience functions
# =============================================================================


def compose(*policies: ResiliencePolicy) -> PolicyComposer:
    """
    Convenience function for composing sync Policies declaratively.

    policies order = outer→inner execution order:
    - compose(Retry, CB, Bulkhead).execute(func)
    - = Retry(CB(Bulkhead(func)))

    Usage::

        result = compose(
            RetryPolicy(max_retries=3),
            CircuitBreakerPolicy(service_name="payment"),
            BulkheadPolicy(bulkhead=semaphore),
            FallbackPolicy(default_value={"status": "degraded"}),
        ).execute(lambda: call_payment_api())
    """
    composer: PolicyComposer = PolicyComposer()
    for policy in policies:
        composer.add(policy)
    return composer


def compose_async(*policies: AsyncResiliencePolicy) -> AsyncPolicyComposer:
    """
    Convenience function for composing async Policies declaratively.

    policies order = outer→inner execution order.
    Provides the same declarative pattern as the sync compose(), for async.

    Usage::

        result = await compose_async(
            AsyncBulkheadPolicy(async_bulkhead=bulkhead),
            AsyncFallbackPolicy(default_value={"degraded": True}),
        ).execute(async_func)
    """
    composer: AsyncPolicyComposer = AsyncPolicyComposer()
    for policy in policies:
        composer.add(policy)
    return composer
