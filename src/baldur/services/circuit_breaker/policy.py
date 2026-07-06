"""
Circuit Breaker Policy — function-wrapping-based Circuit Breaker.

Converts the existing condition-check approach based on
CircuitBreakerService.should_allow() into a function-wrapping approach
based on ResiliencePolicy.execute().

Internally it reuses the existing CircuitBreakerService and manages state
through automatic counting (record_failure/record_success).

Three circuit-control paths coexist independently:
- CircuitBreakerPolicy (record_failure): automatic counting based on generic Exceptions
- ProtectionMixin (record_rate_limit_response): force_open based on 429 traffic
- ManualControlMixin (force_open/force_close): manual operator control
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import structlog

from baldur.core.execution_mode import get_execution_mode, intervention_suppressed
from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)

from .config import CircuitBreakerConfig, CircuitState
from .exceptions import CircuitBreakerOpenError
from .service import CircuitBreakerService

logger = structlog.get_logger()

T = TypeVar("T")


class CircuitBreakerPolicy(ResiliencePolicy[T]):
    """
    Circuit Breaker Policy — function-wrapping-based.

    Decides whether a request is allowed via should_allow() and automatically
    calls record_success() / record_failure() based on the execution result.

    - CB disabled: run the function directly and return SUCCESS
    - CB OPEN state: do not run the function and return REJECTED (CircuitBreakerOpenError)
    - Function succeeds: call record_success() then return SUCCESS
    - Function fails: after _is_failure() judgment, call record_failure(); the exception propagates upward

    Transition-only philosophy: per-reject hook bodies are NOT a default
    responsibility of this policy. State-transition events
    (closed→open, half_open→open) and the matching audit rows are emitted by
    ``CircuitBreakerService``; per-reject volume is observable via
    ``baldur_circuit_breaker_blocked_total{service, reason}``. External
    authors can still inject custom ``hooks=[…]`` for per-call
    instrumentation.
    Ref: 494

    Args:
        service_name: Identifier of the external service protected by the Circuit Breaker
        cb_service: Existing CircuitBreakerService instance (auto-created if None)
        config: CircuitBreakerConfig (used when cb_service is None)
        failure_exceptions: Tuple of exception types counted as failures
        ignore_exceptions: Tuple of exception types NOT counted as failures
        hooks: List of PolicyHooks (None means an empty list (transition-only); cycle-level
            events are emitted by ``CircuitBreakerService``, and per-reject counts are
            handled by the ``baldur_circuit_breaker_blocked_total{service, reason}``
            metric)
    """

    def __init__(
        self,
        service_name: str,
        cb_service: CircuitBreakerService | None = None,
        config: CircuitBreakerConfig | None = None,
        failure_exceptions: tuple[type[Exception], ...] = (Exception,),
        ignore_exceptions: tuple[type[Exception], ...] = (),
        hooks: list | None = None,
    ):
        self._service_name = service_name
        self._cb_service = cb_service or self._create_default_service(config)
        self._failure_exceptions = failure_exceptions
        self._ignore_exceptions = ignore_exceptions
        self._hooks = hooks if hooks is not None else []

    @staticmethod
    def _create_default_service(
        config: CircuitBreakerConfig | None = None,
    ) -> CircuitBreakerService:
        """
        Create the default CircuitBreakerService — uses LayeredRepository.

        If the "layered" key is registered in ProviderRegistry, use LayeredRepository.
        Otherwise, fall back to the ProviderRegistry default (redis).
        This removes Redis I/O from the hot path and guarantees L1 Memory decisions (#227 §7.4).
        """
        repository = None
        try:
            from baldur.factory import ProviderRegistry

            repository = ProviderRegistry.get_circuit_breaker_repo(name="layered")
        except (ValueError, ImportError, Exception):
            logger.debug("circuit_breaker_policy.layered_repo_available_falling")
        return CircuitBreakerService(config=config, repository=repository)

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "circuit_breaker"

    @property
    def service_name(self) -> str:
        """Name of the protected service."""
        return self._service_name

    @property
    def cb_service(self) -> CircuitBreakerService:
        """Internal CircuitBreakerService instance."""
        return self._cb_service

    def _is_failure(self, error: Exception) -> bool:
        """
        Decide whether an exception should be counted as a failure.

        If it matches ignore_exceptions, it is not counted as a failure.
        If it matches failure_exceptions, it is counted as a failure.
        """
        if isinstance(error, self._ignore_exceptions):
            return False
        return isinstance(error, self._failure_exceptions)

    def _invoke_hooks(self, method: str, *args: Any) -> None:
        """Invoke all hooks fail-open."""
        for hook in self._hooks:
            try:
                getattr(hook, method)(*args)
            except Exception as e:
                logger.debug(
                    "circuit_breaker_policy.hook_failed",
                    adapter_type=type(hook).__name__,
                    method=method,
                    error=e,
                )

    def _admit(self) -> tuple[str, PolicyResult[T] | None, Any]:
        """Resolve the Circuit Breaker admission verdict WITHOUT running func.

        Returns ``(verdict, reject_result, hint_state)``:

        - ``("direct", None, None)`` — CB disabled or observe-only: run func
          once and wrap in a direct SUCCESS result; never record.
        - ``("reject", reject_result, None)`` — CB OPEN: return ``reject_result``
          (a REJECTED PolicyResult carrying ``CircuitBreakerOpenError``); do not
          run func.
        - ``("run", None, hint_state)`` — admitted: run func, then call
          :meth:`_on_success` / :meth:`_on_failure` with ``hint_state`` (the
          state object ``should_allow_with_state`` already loaded, so record_*
          skips a redundant lookup).

        Shared verbatim by the sync ``execute`` and the async wrapper — the only
        difference between the two variants is ``func()`` vs ``await func()``.
        """
        # Run directly when CB is disabled
        if not self._cb_service.is_enabled:
            return "direct", None, None

        # Hook: execution start
        self._invoke_hooks("on_execute", self._service_name, 1)

        # Observe-only (dry-run / shadow / evaluation): resolve the mode BEFORE
        # the admission check. ``should_allow_with_state`` atomically advances
        # OPEN->HALF_OPEN (a real persisted state mutation + auto-recovery audit
        # row + CIRCUIT_BREAKER_HALF_OPENED event) once recovery_timeout has
        # elapsed, so it must NOT run under observe-only — that would leak an
        # automatic transition the mode promises to suppress. Peek the state
        # read-only for the would-have signal, run the business function exactly
        # once, and never reject or record; the business exception still
        # propagates. The active path keeps its single-fetch admission below.
        if not get_execution_mode().should_execute:
            peek = self._cb_service.get_or_create_state(self._service_name)
            would_reject = peek.state == CircuitState.OPEN
            intervention_suppressed(
                service_name=self._service_name,
                action=(
                    "circuit_breaker_reject"
                    if would_reject
                    else "circuit_breaker_record"
                ),
                would_reject=would_reject,
            )
            return "direct", None, None

        # Check whether the request is allowed — single fetch via companion API (#485 D2/G1).
        # ``decision.state.state`` reuses the state object that
        # ``should_allow_with_state`` already loaded, eliminating the second
        # ``get_or_create_state`` call that the former ``get_state`` lookup
        # incurred on every reject.
        decision = self._cb_service.should_allow_with_state(self._service_name)

        if not decision.allowed:
            reject_result: PolicyResult[T] = PolicyResult(
                outcome=PolicyOutcome.REJECTED,
                error=CircuitBreakerOpenError(self._service_name),
                executed_policies=["circuit_breaker"],
                metadata={
                    "service_name": self._service_name,
                    "state": decision.state.state,
                },
            )
            # Hook: CB OPEN rejection (Audit + EventBus)
            self._invoke_hooks("on_reject", self._service_name, "circuit_open")
            return "reject", reject_result, None

        # 490 D4: return decision.state as hint_state so record_success /
        # record_failure skip the redundant ``get_or_create_state`` lookup. The
        # CLOSED steady-state path then does zero repository acquires.
        return "run", None, decision.state

    def _direct_result(self, value: T) -> PolicyResult[T]:
        """Build the SUCCESS result for the disabled / observe-only paths (no record)."""
        return PolicyResult(
            value=value,
            outcome=PolicyOutcome.SUCCESS,
            executed_policies=["circuit_breaker"],
        )

    def _on_success(self, value: T, hint_state: Any) -> PolicyResult[T]:
        """Record a success and fire the success hook, returning the SUCCESS result."""
        self._cb_service.record_success(
            self._service_name,
            hint_state=hint_state,
        )
        success_result = PolicyResult(
            value=value,
            outcome=PolicyOutcome.SUCCESS,
            executed_policies=["circuit_breaker"],
        )
        # Hook: execution success (Audit + EventBus)
        self._invoke_hooks("on_success", self._service_name, success_result)
        return success_result

    def _on_failure(self, error: Exception, hint_state: Any) -> None:
        """Record a failure (after the ``_is_failure`` gate) and fire the failure hook.

        The caller re-raises ``error`` so an upper Policy (Retry, etc.) can
        handle it. ``asyncio.CancelledError`` never reaches here: it is a
        ``BaseException``, so the caller's ``except Exception`` boundary lets it
        escape untouched (no ``record_failure``) and a cancelled call cannot
        corrupt the breaker's failure count. Do NOT widen the caller's boundary
        to ``except BaseException``.
        """
        if self._is_failure(error):
            self._cb_service.record_failure(
                self._service_name,
                error_context={"error": str(error), "type": type(error).__name__},
                hint_state=hint_state,
            )
        # Hook: execution failure (Audit + EventBus)
        self._invoke_hooks("on_failure", self._service_name, error, 1)

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """
        Execute the function based on the Circuit Breaker state.

        1. CB disabled → run directly
        2. should_allow() == False → return REJECTED (function not run) + hook.on_reject
        3. should_allow() == True → run the function
           - success → return SUCCESS after record_success() + hook.on_success
           - failure → after _is_failure() judgment, record_failure() + hook.on_failure, re-raise exception

        On rejection due to CB OPEN, no exception is thrown; a PolicyResult is returned instead.
        Exceptions raised during function execution are re-raised so an upper Policy (Retry, etc.) can handle them.
        """
        verdict, reject_result, hint_state = self._admit()
        if verdict == "reject":
            return reject_result  # type: ignore[return-value]
        if verdict == "direct":
            return self._direct_result(func(*args, **kwargs))

        # verdict == "run": execute + record. ``except Exception`` (never
        # BaseException) keeps KeyboardInterrupt/SystemExit propagating uncounted.
        try:
            value = func(*args, **kwargs)
            return self._on_success(value, hint_state)
        except Exception as e:
            self._on_failure(e, hint_state)
            raise  # propagate so an upper Policy (Retry, etc.) can handle it


class AsyncCircuitBreakerPolicy:
    """
    Async Circuit Breaker Policy — awaits ``func`` while reusing the sync
    CircuitBreakerPolicy state machine.

    Composes a :class:`CircuitBreakerPolicy` and drives its shared
    ``_admit`` / ``_direct_result`` / ``_on_success`` / ``_on_failure`` helpers,
    so admission, recording, hooks, and observe-only suppression are a single
    source of truth across sync and async. The only behavioral difference from
    the sync sibling is ``await func()`` in place of ``func()`` — the CB state
    machine is pure in-memory L1, so no async Redis driver or async lock is
    needed.

    Implements the AsyncResiliencePolicy Protocol.

    Args:
        inner: The composed synchronous CircuitBreakerPolicy whose
            CircuitBreakerService (and per-name state) is shared. Passing the
            same per-name breaker lets sync and async calls accumulate failures
            against one breaker so it actually opens.
    """

    def __init__(self, inner: CircuitBreakerPolicy) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        """Policy identifier."""
        return "circuit_breaker"

    @property
    def service_name(self) -> str:
        """Name of the protected service."""
        return self._inner.service_name

    @property
    def cb_service(self) -> CircuitBreakerService:
        """Underlying CircuitBreakerService (shared with the composed sync policy)."""
        return self._inner.cb_service

    @property
    def policy(self) -> CircuitBreakerPolicy:
        """The composed synchronous CircuitBreakerPolicy."""
        return self._inner

    async def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult[T]:
        """Await ``func`` under the Circuit Breaker, reusing the sync state machine.

        Mirrors :meth:`CircuitBreakerPolicy.execute` exactly, awaiting ``func``.
        A cancelled ``await func()`` raises ``asyncio.CancelledError`` — a
        ``BaseException`` — which escapes the ``except Exception`` boundary
        untouched, so a client-disconnect cancellation records no failure and
        cannot trip the breaker. Never widen this to ``except BaseException``.
        """
        inner = self._inner
        verdict, reject_result, hint_state = inner._admit()
        if verdict == "reject":
            return reject_result  # type: ignore[return-value]
        if verdict == "direct":
            return inner._direct_result(await func(*args, **kwargs))

        try:
            value = await func(*args, **kwargs)
            return inner._on_success(value, hint_state)
        except Exception as e:
            inner._on_failure(e, hint_state)
            raise  # propagate so an upper Policy (Retry, etc.) can handle it


def circuit_breaker(
    service_name: str | None = None,
    cb_service: CircuitBreakerService | None = None,
    config: CircuitBreakerConfig | None = None,
    failure_exceptions: tuple[type[Exception], ...] = (Exception,),
    ignore_exceptions: tuple[type[Exception], ...] = (),
) -> Callable:
    """
    Circuit Breaker decorator — sync/async dual-dispatch.

    Applying it to a function automatically wraps it with a CircuitBreakerPolicy.
    An ``async def`` is wrapped by :class:`AsyncCircuitBreakerPolicy` (which
    awaits the coroutine and records the real outcome) instead of the sync
    policy, so an async target is protected rather than silently bypassed. If
    service_name is None, the function's __qualname__ is used as the default.

    The wrapper returns a ``PolicyResult`` (not the unwrapped value), matching
    the sync convention; ``wrapper.policy`` exposes the underlying sync
    ``CircuitBreakerPolicy`` (its ``.cb_service`` reachable) for both variants.

    Usage::

        @circuit_breaker("payment_api")
        def call_payment_api():
            ...

        @circuit_breaker("payment_api")
        async def call_payment_api_async():
            ...

        @circuit_breaker()  # service_name = the function's __qualname__
        def call_external():
            ...
    """

    def decorator(func: Callable[..., T]) -> Callable[..., PolicyResult[T]]:
        name = service_name or func.__qualname__
        policy = CircuitBreakerPolicy(
            service_name=name,
            cb_service=cb_service,
            config=config,
            failure_exceptions=failure_exceptions,
            ignore_exceptions=ignore_exceptions,
        )

        if asyncio.iscoroutinefunction(func):
            apolicy = AsyncCircuitBreakerPolicy(policy)

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> PolicyResult[T]:
                return await apolicy.execute(func, *args, **kwargs)

            # Expose the underlying sync CircuitBreakerPolicy (.cb_service
            # reachable), matching the sync sibling's ``wrapper.policy``.
            async_wrapper.policy = policy  # type: ignore[attr-defined]
            return async_wrapper

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> PolicyResult[T]:
            return policy.execute(func, *args, **kwargs)

        # Attach attribute so the Policy instance is accessible
        wrapper.policy = policy  # type: ignore[attr-defined]
        return wrapper

    return decorator
