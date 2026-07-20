"""
Resilience Policy Interfaces for Baldur System

Unified interfaces for all resilience patterns (Retry, Circuit Breaker,
Bulkhead, Fallback, Hedging, Throttle). Each pattern implements the
same Protocol, enabling declarative composition via PolicyComposer.

Provides:
- PolicyOutcome / PolicyResult: Unified result type replacing
  RetryResult, FallbackResult, CircuitBreakerResult, BulkheadFullError
- PolicyContext: Immutable execution context (frozen dataclass)
- ResiliencePolicy / AsyncResiliencePolicy: Core Protocols (sync/async)
- PolicyGuard / GuardResult: Pre-execution validation hooks
- PolicyHook: Execution event observer (Fail-Open)
- FailureSink: Terminal failure handler (DLQ, logging, etc.)
- AsyncPolicyGuard / AsyncPolicyHook / AsyncFailureSink: async-native side-effect
  channels awaited directly by AsyncPolicyComposer (no thread hop)

Design Principles:
1. Protocol-based for structural subtyping (duck typing)
2. Existing implementations are wrapped, not replaced
3. All business exceptions are swallowed into PolicyResult
4. KeyboardInterrupt/SystemExit pass through (except Exception)

Usage:
    from baldur.interfaces import (
        PolicyOutcome,
        PolicyResult,
        PolicyContext,
        ResiliencePolicy,
        AsyncResiliencePolicy,
        PolicyGuard,
        GuardResult,
        PolicyHook,
        FailureSink,
        AsyncPolicyGuard,
        AsyncPolicyHook,
        AsyncFailureSink,
    )
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


# =============================================================================
# Exceptions
# =============================================================================


class PolicyRejectedException(Exception):
    """Marker base for exceptions that map to ``PolicyOutcome.REJECTED``.

    Two roles:

    1. **Composer-internal signal**: ``PolicyComposer`` raises this directly
       when an inner policy returns a non-success ``PolicyResult`` without an
       attached ``error``. The outer ``except PolicyRejectedException`` branch
       converts it back into ``PolicyResult(outcome=REJECTED)``.

    2. **Marker base for domain rejection exceptions**: Concrete rejection
       exceptions (``CircuitBreakerOpenError``, ``BulkheadFullError``) inherit
       this class so the same outer catch dispatch classifies them as
       ``REJECTED`` instead of funneling into the generic ``except Exception``
       branch (which would mislabel them as ``FAILURE``). The original
       exception type is preserved through ``PolicyResult.error`` so hooks /
       sinks observe the rich domain context (``service_name``, ``state``,
       ``bulkhead_name``, etc.).
    """


# =============================================================================
# Enums
# =============================================================================


class PolicyOutcome(str, Enum):
    """Kinds of Policy execution result."""

    SUCCESS = "success"  # Successful execution
    SUCCESS_WITH_FALLBACK = "fallback"  # Success via fallback
    REJECTED = "rejected"  # Rejected by a policy (CB open, Bulkhead full, etc.)
    FAILURE = "failure"  # All attempts failed
    TIMEOUT = "timeout"  # Timed out


# =============================================================================
# Data Transfer Objects (DTOs)
# =============================================================================


@dataclass(slots=True)
class PolicyResult(Generic[T]):
    """
    Unified result type for every resilience Policy.

    Consolidates each pattern's existing result type into a single shape:
    - RetryResult(success, action, attempt, value, error, dlq_id)
    - FallbackResult(value, used_fallback, fallback_mode, original_error)
    - CircuitBreakerResult is for state management only and is not converted
    - BulkheadFullError exceptions are mapped to PolicyResult(outcome=REJECTED)

    Attributes:
        value: Result value (on success)
        outcome: Kind of execution result
        error: Exception raised on failure
        executed_policies: Names of policies that ran
        total_attempts: Total number of attempts
        total_duration_ms: Total execution time (milliseconds)
        metadata: Per-pattern details (optional)
    """

    value: T | None = None
    outcome: PolicyOutcome = PolicyOutcome.SUCCESS
    error: Exception | None = None

    # Execution metadata
    executed_policies: list[str] = field(default_factory=list)
    total_attempts: int = 1
    total_duration_ms: float = 0.0

    # Per-pattern details (optional)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Whether the execution succeeded (including Fallback)."""
        return self.outcome in (
            PolicyOutcome.SUCCESS,
            PolicyOutcome.SUCCESS_WITH_FALLBACK,
        )

    @property
    def executed(self) -> bool:
        """Whether the policy pipeline was executed (always True if PolicyComposer ran)."""
        return True

    @property
    def rejected(self) -> bool:
        """Whether the execution was rejected by a policy."""
        return self.outcome == PolicyOutcome.REJECTED


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """
    Policy pipeline execution context (immutable).

    ``frozen=True`` prevents side-effects inside the pipeline. Use
    ``dataclasses.replace()`` (or ``with_updates()``) to produce a copy with
    edited fields (Copy-on-Write).

    Attributes:
        order_id: Order identifier (read by DLQ sink as ``entity_id``).
        payment_id: Payment identifier (no current named-field consumer;
            decorator auto-extract still forwards it via ``extra["request_data"]``).
        user_id: User identifier (read by DLQ sink as ``user_id`` column).
        tier_id: Service tier ("critical" | "standard" | "non_essential").
        region: Region identifier (input to ErrorBudgetGate).
        domain: Domain identifier (mirrors ``RetryPolicyConfig.domain``).
        trace_id: Distributed-trace ID (OTel ``trace_id``).
        extra: Open-ended extension dict. Conventional keys:
            - ``request_data`` (dict): Per-call payload snapshot written by
              ``@protected`` / ``@dlq_protect`` auto-extract. The
              decorator binds the wrapped function's primitive-typed args and
              writes them here so DLQ entries carry the full payload for
              operator search (``WHERE request_data->>'payment_id'='x'``).
              Direct ``protect()`` callers may populate this manually.
            - ``snapshot_data`` (dict): Pre-failure snapshot for forensic
              replay; read by DLQ sink.
            - ``response_data`` (dict): Downstream response payload; read by
              DLQ sink.
            - ``user_id`` (str/int): Legacy fallback for direct callers who
              populate ``extra`` without setting ``PolicyContext.user_id``;
              the named field wins when both are set.
    """

    # Business identifiers
    order_id: str | None = None
    payment_id: str | None = None
    user_id: str | None = None

    # Policy decision inputs
    tier_id: str | None = None
    region: str | None = None

    # Domain / tracing
    domain: str = ""
    trace_id: str | None = None

    # Extension field
    extra: dict[str, Any] = field(default_factory=dict)

    def with_updates(self, **kwargs: Any) -> PolicyContext:
        """Copy-on-Write: return a new instance with the given fields replaced."""
        return replace(self, **kwargs)


@dataclass
class GuardResult:
    """
    Guard validation result.

    Attributes:
        allowed: Whether execution is allowed (True = pass, False = reject)
        reason: Rejection reason (when allowed=False)
        metadata: Additional information (per-Guard details)
    """

    allowed: bool
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Core Protocols — Sync / Async
# =============================================================================


@runtime_checkable
class ResiliencePolicy(Protocol[T]):
    """
    Core Protocol implemented by synchronous resilience patterns.

    Each Policy wraps a function and applies resilience logic.
    Composition of policies is handled by PolicyComposer.

    Exception-handling contract:
    - Business exceptions are wrapped in PolicyResult(outcome=FAILURE, error=e) and returned
    - The "except Exception" pattern automatically lets KeyboardInterrupt/SystemExit through
    - BulkheadFullError is converted to PolicyResult(outcome=REJECTED) by the Policy wrapper
    """

    @property
    def name(self) -> str:
        """Policy identifier (e.g., 'retry', 'circuit_breaker', 'bulkhead')."""
        ...

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Wrap a function in the Policy and execute it.

        Args:
            func: Function to execute
            *args: Function positional arguments
            context: Execution context (propagated to Guard/Hook/Sink)
            **kwargs: Function keyword arguments

        Returns:
            PolicyResult[T]: Unified result. Does not raise.
        """
        ...


@runtime_checkable
class AsyncResiliencePolicy(Protocol[T]):
    """
    Protocol implemented by asynchronous resilience patterns.

    Current concrete implementation: AsyncSemaphoreBulkhead (async_semaphore.py).
    Follows the same exception-handling contract.
    """

    @property
    def name(self) -> str:
        """Policy identifier."""
        ...

    async def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Wrap an async function in the Policy and execute it.

        Args:
            func: Async function to execute
            *args: Function positional arguments
            context: Execution context (propagated to Guard/Hook/Sink)
            **kwargs: Function keyword arguments

        Returns:
            PolicyResult[T]: Unified result. Does not raise.
        """
        ...


# =============================================================================
# Guard — Pre-execution Validation
# =============================================================================


@runtime_checkable
class PolicyGuard(Protocol):
    """
    Pre-execution validation for a Policy.

    Guard implementations must define the default behavior when context=None:
    - KillSwitchGuard: ignore context, only check global state
    - ErrorBudgetGuard: tier_id=None -> global decision (tier-agnostic)
    - RetryBudgetGuard: decide against the default budget
    """

    @property
    def name(self) -> str:
        """Guard identifier."""
        ...

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Check whether execution is allowed.

        Args:
            context: Execution context. When None, only the global state is checked.

        Returns:
            GuardResult: allowed=True passes, allowed=False rejects.
        """
        ...


# =============================================================================
# Hook — Execution Event Observer (Fail-Open)
# =============================================================================


@runtime_checkable
class PolicyHook(Protocol):
    """
    Hook that observes Policy execution events.

    Fail-Open principle: hook failures must not stop business logic.
    """

    def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """Called when execution starts."""
        ...

    def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """Called on successful execution."""
        ...

    def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """Called on failed execution."""
        ...

    def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """Called before a scheduled retry (not invoked on the final failure or when the budget is exhausted)."""
        ...

    def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """Called when a Guard rejects (Kill Switch, CB open, Bulkhead full, etc.)."""
        ...


# =============================================================================
# Sink — Terminal Failure Handler
# =============================================================================


@runtime_checkable
class FailureSink(Protocol):
    """
    Interface that handles the terminal failure after every Policy is exhausted.

    Performs final-failure handling such as DLQ persistence, error logging, alerts, etc.
    """

    def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        """
        Handle the terminal failure.

        Args:
            error: Terminal failure exception
            context: PolicyContext (order_id, user_id, etc. needed for DLQ persistence)
            policy_result: Full pipeline result

        Returns:
            Failure record ID (e.g., DLQ ID) or None
        """
        ...


# =============================================================================
# Async side-effect channels — awaited natively by AsyncPolicyComposer
# =============================================================================
#
# The async twins of PolicyGuard / PolicyHook / FailureSink. AsyncPolicyComposer
# awaits these directly (zero thread hop) instead of offloading a sync channel
# via asyncio.to_thread. Method names mirror the sync trio (check / on_* /
# handle_failure) with async signatures — following the AsyncResiliencePolicy
# .execute naming precedent, NOT an acheck / ahandle_* prefix. A sync channel
# added to AsyncPolicyComposer is auto-wrapped in a thin to_thread-offloading
# adapter that satisfies these Protocols, so both native-async and legacy sync
# implementations compose uniformly.


@runtime_checkable
class AsyncPolicyGuard(Protocol):
    """Async twin of :class:`PolicyGuard` — pre-execution validation.

    Awaited by AsyncPolicyComposer before the async policy chain runs. A
    native implementation (e.g. an async idempotency guard driving an
    awaitable dedup gate) executes with no thread hop.
    """

    @property
    def name(self) -> str:
        """Guard identifier."""
        ...

    async def check(self, context: PolicyContext | None = None) -> GuardResult:
        """Check whether execution is allowed (awaitable).

        Args:
            context: Execution context. When None, only the global state is checked.

        Returns:
            GuardResult: allowed=True passes, allowed=False rejects.
        """
        ...


@runtime_checkable
class AsyncPolicyHook(Protocol):
    """Async twin of :class:`PolicyHook` — execution event observer (Fail-Open).

    Awaited by AsyncPolicyComposer around the async chain. Fail-Open
    principle: a hook failure must not stop business logic.
    """

    async def on_execute(
        self, policy_name: str, attempt: int, context: PolicyContext | None = None
    ) -> None:
        """Called when execution starts."""
        ...

    async def on_success(
        self,
        policy_name: str,
        result: PolicyResult,
        context: PolicyContext | None = None,
    ) -> None:
        """Called on successful execution."""
        ...

    async def on_failure(
        self,
        policy_name: str,
        error: Exception,
        attempt: int,
        context: PolicyContext | None = None,
    ) -> None:
        """Called on failed execution."""
        ...

    async def on_retry(
        self,
        policy_name: str,
        attempt: int,
        delay: float,
        context: PolicyContext | None = None,
    ) -> None:
        """Called before a scheduled retry (not on final failure / budget exhaustion)."""
        ...

    async def on_reject(
        self, guard_name: str, reason: str, context: PolicyContext | None = None
    ) -> None:
        """Called when a Guard rejects (Kill Switch, CB open, Bulkhead full, etc.)."""
        ...


@runtime_checkable
class AsyncFailureSink(Protocol):
    """Async twin of :class:`FailureSink` — terminal failure handler.

    Awaited by AsyncPolicyComposer after every policy is exhausted. The DLQ
    store is still synchronous, so on the async path it is wrapped in a
    to_thread-offloading adapter that satisfies this Protocol; when a native
    async DLQ store lands it implements this Protocol directly with no
    composer change.
    """

    async def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        """Handle the terminal failure (awaitable).

        Args:
            error: Terminal failure exception
            context: PolicyContext (order_id, user_id, etc. needed for DLQ persistence)
            policy_result: Full pipeline result

        Returns:
            Failure record ID (e.g., DLQ ID) or None
        """
        ...
