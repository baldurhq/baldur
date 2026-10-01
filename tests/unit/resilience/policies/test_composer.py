"""
Unit tests for PolicyComposer / AsyncPolicyComposer / compose / compose_async (#231).

Targets:
- resilience/policies/composer.py
  (PolicyComposer, AsyncPolicyComposer, compose, compose_async, _FallbackApplied)

UNIT_TEST_GUIDELINES.md compliance:
- Contract verification: hardcoded expected values (_FallbackApplied structure, initial state)
- Behavior verification: source references (PolicyOutcome, PolicyResult, etc.)
- conftest.py placement: single-file fixtures stay inside the file (§5.1)
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

import pytest
from structlog.testing import capture_logs

from baldur.core.exceptions import TimeoutPolicyError
from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
    PolicyOutcome,
    PolicyRejectedException,
    PolicyResult,
)
from baldur.resilience.policies.composer import (
    AsyncPolicyComposer,
    PolicyComposer,
    _arm_failure_verdict,
    _classify_exception_outcome,
    _FallbackApplied,
    _is_failed_call,
    _is_open_circuit_rejection,
    _SyncSinkToAsyncAdapter,
    _terminal_reaches_sinks,
    compose,
    compose_async,
)
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

# =============================================================================
# Mock implementations — Protocol-compliant
# =============================================================================


class MockPolicy:
    """ResiliencePolicy Protocol-compliant mock — runs the function as-is."""

    def __init__(self, name: str = "mock_policy") -> None:
        self._name = name
        self.execute_count = 0

    @property
    def name(self) -> str:
        return self._name

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        self.execute_count += 1
        try:
            value = func(*args, **kwargs)
            return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)
        except Exception as e:
            return PolicyResult(value=None, outcome=PolicyOutcome.FAILURE, error=e)


class MockRejectingPolicy:
    """Policy that rejects the request — returns REJECTED."""

    def __init__(self, name: str = "rejecting_policy") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            error=PolicyRejectedException("Rejected by mock"),
        )


class MockAsyncPolicy:
    """AsyncResiliencePolicy Protocol-compliant mock."""

    def __init__(self, name: str = "async_mock_policy") -> None:
        self._name = name
        self.execute_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        self.execute_count += 1
        try:
            value = await func(*args, **kwargs)
            return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)
        except Exception as e:
            return PolicyResult(value=None, outcome=PolicyOutcome.FAILURE, error=e)


class MockGuard:
    """PolicyGuard Protocol-compliant mock."""

    def __init__(
        self,
        allowed: bool = True,
        reason: str | None = None,
        guard_name: str = "mock_guard",
    ) -> None:
        self._allowed = allowed
        self._reason = reason
        self._name = guard_name
        self.check_count = 0

    @property
    def name(self) -> str:
        return self._name

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        self.check_count += 1
        return GuardResult(allowed=self._allowed, reason=self._reason)


class MockFailingGuard:
    """Guard whose check() raises — for fail-open tests."""

    @property
    def name(self) -> str:
        return "failing_guard"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        raise RuntimeError("Guard internal error")


class MockMetadataGuard:
    """Rejecting guard that carries ``GuardResult.metadata`` (#567 D2).

    Models the IdempotencyGuard reject shape so the composer's reject-path
    metadata propagation can be verified without importing the real guard.
    """

    def __init__(
        self,
        metadata: dict[str, Any],
        reason: str = "blocked",
        guard_name: str = "idempotency",
    ) -> None:
        self._metadata = metadata
        self._reason = reason
        self._name = guard_name

    @property
    def name(self) -> str:
        return self._name

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        return GuardResult(allowed=False, reason=self._reason, metadata=self._metadata)


class MockHook:
    """PolicyHook Protocol-compliant mock — records calls."""

    def __init__(self) -> None:
        self.success_calls: list[tuple[str, PolicyResult]] = []
        self.failure_calls: list[tuple[str, Exception, int]] = []
        self.reject_calls: list[tuple[str, str]] = []

    def on_execute(self, policy_name: str, attempt: int, **kwargs) -> None:
        pass

    def on_success(self, policy_name: str, result: PolicyResult, **kwargs) -> None:
        self.success_calls.append((policy_name, result))

    def on_failure(
        self, policy_name: str, error: Exception, attempt: int, **kwargs
    ) -> None:
        self.failure_calls.append((policy_name, error, attempt))

    def on_retry(self, policy_name: str, attempt: int, delay: float, **kwargs) -> None:
        pass

    def on_reject(self, policy_name: str, reason: str, **kwargs) -> None:
        self.reject_calls.append((policy_name, reason))


class MockFailingHook:
    """Hook that raises from on_success/on_failure/on_reject — fail-open tests."""

    def on_execute(self, policy_name: str, attempt: int, **kwargs) -> None:
        raise RuntimeError("Hook error")

    def on_success(self, policy_name: str, result: PolicyResult, **kwargs) -> None:
        raise RuntimeError("Hook error")

    def on_failure(
        self, policy_name: str, error: Exception, attempt: int, **kwargs
    ) -> None:
        raise RuntimeError("Hook error")

    def on_retry(self, policy_name: str, attempt: int, delay: float, **kwargs) -> None:
        raise RuntimeError("Hook error")

    def on_reject(self, policy_name: str, reason: str, **kwargs) -> None:
        raise RuntimeError("Hook error")


class MockSink:
    """FailureSink Protocol-compliant mock — records calls."""

    def __init__(self, sink_id: str | None = "sink-123") -> None:
        self._sink_id = sink_id
        self.calls: list[tuple[Exception, PolicyContext | None, PolicyResult]] = []

    def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        self.calls.append((error, context, policy_result))
        return self._sink_id


class MockFailingSink:
    """Sink that raises from handle_failure — fail-open tests."""

    def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        raise RuntimeError("Sink error")


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def composer():
    """Empty PolicyComposer instance."""
    return PolicyComposer()


@pytest.fixture
def async_composer():
    """Empty AsyncPolicyComposer instance."""
    return AsyncPolicyComposer()


# =============================================================================
# Contract verification — _FallbackApplied internal signal
# =============================================================================


class TestFallbackAppliedContract:
    """Contract verification of the _FallbackApplied internal signal exception."""

    def test_is_base_exception_subclass(self):
        """_FallbackApplied is a BaseException subclass (not Exception)."""
        assert issubclass(_FallbackApplied, BaseException)
        assert not issubclass(_FallbackApplied, Exception)

    def test_has_result_attribute(self):
        """A _FallbackApplied instance carries a result attribute."""
        result = PolicyResult(value="test", outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        exc = _FallbackApplied(result)
        assert exc.result is result

    def test_message(self):
        """_FallbackApplied's default message is 'Fallback applied'."""
        result = PolicyResult(value=None)
        exc = _FallbackApplied(result)
        assert str(exc) == "Fallback applied"

    def test_not_caught_by_except_exception(self):
        """except Exception does not catch _FallbackApplied (#418 P0-4)."""
        result = PolicyResult(value="fb", outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        caught = False
        try:
            raise _FallbackApplied(result)
        except Exception:
            caught = True
        except BaseException:
            pass  # expected path
        assert not caught, "_FallbackApplied must not be caught by except Exception"


# =============================================================================
# Contract verification — PolicyComposer initial state
# =============================================================================


class TestPolicyComposerInitContract:
    """Contract verification of the PolicyComposer initial state."""

    def test_policies_empty(self, composer):
        """The initial _policies list is empty."""
        assert composer._policies == []

    def test_guards_empty(self, composer):
        """The initial _guards list is empty."""
        assert composer._guards == []

    def test_hooks_empty(self, composer):
        """The initial _hooks list is empty."""
        assert composer._hooks == []

    def test_sinks_empty(self, composer):
        """The initial _sinks list is empty."""
        assert composer._sinks == []


class TestAsyncPolicyComposerInitContract:
    """Contract verification of the AsyncPolicyComposer initial state."""

    def test_policies_empty(self, async_composer):
        """The initial _policies list is empty."""
        assert async_composer._policies == []

    def test_guards_empty(self, async_composer):
        """The initial _guards list is empty."""
        assert async_composer._guards == []

    def test_hooks_empty(self, async_composer):
        """The initial _hooks list is empty."""
        assert async_composer._hooks == []

    def test_sinks_empty(self, async_composer):
        """The initial _sinks list is empty."""
        assert async_composer._sinks == []


# =============================================================================
# Behavior verification — Builder API
# =============================================================================


class TestPolicyComposerBuilderBehavior:
    """PolicyComposer Builder API behavior verification."""

    def test_add_returns_self(self, composer):
        """add() returns self to support chaining."""
        policy = MockPolicy()
        result = composer.add(policy)
        assert result is composer

    def test_add_appends_policy(self, composer):
        """add() appends the Policy to _policies."""
        policy = MockPolicy()
        composer.add(policy)
        assert composer._policies == [policy]

    def test_add_multiple_policies_preserves_order(self, composer):
        """add() preserves insertion order."""
        p1 = MockPolicy("p1")
        p2 = MockPolicy("p2")
        p3 = MockPolicy("p3")
        composer.add(p1).add(p2).add(p3)
        assert composer._policies == [p1, p2, p3]

    def test_add_async_policy_structural_match(self, composer):
        """A @runtime_checkable Protocol matches structurally, so an async policy is added too.

        ResiliencePolicy and AsyncResiliencePolicy both check only the name+execute attributes.
        The isinstance condition is AsyncResiliencePolicy AND NOT ResiliencePolicy, but
        the signatures are structurally identical, so the guard does not fire.
        Type safety is left to Mypy static analysis.
        """
        async_policy = MockAsyncPolicy()
        # Structural matching satisfies both Protocols → guard does not fire → added
        composer.add(async_policy)
        assert async_policy in composer._policies

    def test_add_guard_returns_self(self, composer):
        """add_guard() returns self."""
        guard = MockGuard()
        result = composer.add_guard(guard)
        assert result is composer

    def test_add_guard_appends_guard(self, composer):
        """add_guard() appends the Guard to _guards."""
        guard = MockGuard()
        composer.add_guard(guard)
        assert composer._guards == [guard]

    def test_add_hook_returns_self(self, composer):
        """add_hook() returns self."""
        hook = MockHook()
        result = composer.add_hook(hook)
        assert result is composer

    def test_add_hook_appends_hook(self, composer):
        """add_hook() appends the Hook to _hooks."""
        hook = MockHook()
        composer.add_hook(hook)
        assert composer._hooks == [hook]

    def test_add_sink_returns_self(self, composer):
        """add_sink() returns self."""
        sink = MockSink()
        result = composer.add_sink(sink)
        assert result is composer

    def test_add_sink_appends_sink(self, composer):
        """add_sink() appends the Sink to _sinks."""
        sink = MockSink()
        composer.add_sink(sink)
        assert composer._sinks == [sink]


class TestAsyncPolicyComposerBuilderBehavior:
    """AsyncPolicyComposer Builder API behavior verification."""

    def test_add_returns_self(self, async_composer):
        """add() returns self."""
        policy = MockAsyncPolicy()
        result = async_composer.add(policy)
        assert result is async_composer

    def test_add_appends_policy(self, async_composer):
        """add() appends the Policy to _policies."""
        policy = MockAsyncPolicy()
        async_composer.add(policy)
        assert async_composer._policies == [policy]

    def test_add_guard_returns_self(self, async_composer):
        """add_guard() returns self."""
        guard = MockGuard()
        result = async_composer.add_guard(guard)
        assert result is async_composer

    def test_add_hook_returns_self(self, async_composer):
        """add_hook() returns self."""
        hook = MockHook()
        result = async_composer.add_hook(hook)
        assert result is async_composer

    def test_add_sink_returns_self(self, async_composer):
        """add_sink() returns self."""
        sink = MockSink()
        result = async_composer.add_sink(sink)
        assert result is async_composer


# =============================================================================
# Behavior verification — execute(): no Policy
# =============================================================================


class TestComposerExecuteNoPolicyBehavior:
    """execute() behavior verification without any Policy."""

    def test_success_without_policies(self, composer):
        """With no Policy, a succeeding func yields the SUCCESS outcome."""
        result = composer.execute(lambda: 42)
        assert result.success is True
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_failure_without_policies(self, composer):
        """With no Policy, a failing func yields the FAILURE outcome."""
        err = ValueError("test error")

        def failing():
            raise err

        result = composer.execute(failing)
        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (CircuitBreakerOpenError("inner_api"), PolicyOutcome.REJECTED),
            (BulkheadFullError("inner_api", 2, 2), PolicyOutcome.REJECTED),
            (TimeoutPolicyError(5.0), PolicyOutcome.TIMEOUT),
        ],
        ids=["open_circuit", "bulkhead_full", "timeout"],
    )
    def test_raise_without_policies_is_classified_as_a_chain_classifies_it(
        self, composer, error, expected
    ):
        """An enclosing call with no stage sees the same terminal a one-stage
        chain does — an inner site's open-circuit rejection stays a rejection,
        so the open-circuit lane (and its custody mark) handles it."""
        empty = composer.execute(_throwing(error))
        chained = PolicyComposer().add(MockPolicy("wrapper")).execute(_throwing(error))

        assert empty.outcome == chained.outcome == expected
        assert empty.error is error


# =============================================================================
# Behavior verification — execute(): Guard checks
# =============================================================================


class TestComposerGuardBehavior:
    """Guard check behavior tests."""

    def test_guard_allowed(self, composer):
        """func runs when the Guard allows."""
        guard = MockGuard(allowed=True)
        composer.add_guard(guard)
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"
        assert guard.check_count == 1

    def test_guard_rejected(self, composer):
        """When the Guard denies, the outcome is REJECTED and func does not run."""
        guard = MockGuard(allowed=False, reason="budget exhausted")
        func_called = False

        def func():
            nonlocal func_called
            func_called = True
            return "should not reach"

        composer.add_guard(guard)
        result = composer.execute(func)

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["rejected_by"] == guard.name
        assert result.metadata["reason"] == "budget exhausted"
        assert func_called is False

    def test_guard_fail_open(self, composer):
        """An exception inside the Guard passes through fail-open."""
        composer.add_guard(MockFailingGuard())
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"

    def test_multiple_guards_short_circuit(self, composer):
        """When the first Guard denies, the second Guard is not called."""
        guard1 = MockGuard(allowed=False, reason="blocked", guard_name="g1")
        guard2 = MockGuard(allowed=True, guard_name="g2")
        composer.add_guard(guard1).add_guard(guard2)

        result = composer.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED
        assert guard1.check_count == 1
        assert guard2.check_count == 0

    def test_guard_receives_context(self, composer):
        """The context is passed to Guard.check()."""
        received_context = []

        class ContextCapturingGuard:
            @property
            def name(self):
                return "ctx_guard"

            def check(self, context=None):
                received_context.append(context)
                return GuardResult(allowed=True)

        ctx = PolicyContext(tier_id="critical", region="us-east-1")
        composer.add_guard(ContextCapturingGuard())
        composer.execute(lambda: "ok", context=ctx)

        assert len(received_context) == 1
        assert received_context[0] is ctx


# =============================================================================
# Behavior verification — execute(): Guard reject metadata propagation (#567 D2)
# =============================================================================


class TestComposerRejectMetadataBehavior:
    """#567 D2: a rejecting guard's ``GuardResult.metadata`` (e.g. the
    idempotency decision + key) is merged into the reject ``PolicyResult``, with
    composer-owned keys winning on collision — symmetric across sync + async."""

    def test_sync_guard_metadata_reaches_reject_result(self, composer):
        guard = MockMetadataGuard(
            metadata={"idempotency_decision": "ABORT", "idempotency_key": "svc:o-1"}
        )
        composer.add_guard(guard)

        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["idempotency_decision"] == "ABORT"
        assert result.metadata["idempotency_key"] == "svc:o-1"
        assert result.metadata["rejected_by"] == "idempotency"
        assert result.metadata["reason"] == "blocked"

    def test_sync_composer_owned_keys_win_on_collision(self, composer):
        # A guard cannot override the composer's own ``rejected_by`` / ``reason``.
        guard = MockMetadataGuard(
            metadata={"rejected_by": "spoofed", "reason": "spoofed"},
            reason="real-reason",
            guard_name="idempotency",
        )
        composer.add_guard(guard)

        result = composer.execute(lambda: "ok")

        assert result.metadata["rejected_by"] == "idempotency"
        assert result.metadata["reason"] == "real-reason"

    @pytest.mark.asyncio
    async def test_async_guard_metadata_reaches_reject_result(self, async_composer):
        guard = MockMetadataGuard(
            metadata={"idempotency_decision": "SKIP", "idempotency_key": "svc:o-2"}
        )
        async_composer.add_guard(guard)

        async def func():
            return "ok"

        result = await async_composer.execute(func)

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["idempotency_decision"] == "SKIP"
        assert result.metadata["idempotency_key"] == "svc:o-2"
        assert result.metadata["rejected_by"] == "idempotency"


# =============================================================================
# Behavior verification — execute(): guard fail-open WARN, sync↔async parity (#567 D7)
# =============================================================================


class TestComposerGuardFailOpenLogBehavior:
    """#567 D7: a guard exception fail-opens but is NOT silent — both the sync
    and async composer loops log ``policy_composer.guard_execution_failed`` at
    WARNING (LOGGING_STANDARDS §3.2: a guard bypass must not be silent)."""

    def test_sync_guard_exception_logs_warning(self, composer):
        composer.add_guard(MockFailingGuard())
        with capture_logs() as cap_logs:
            result = composer.execute(lambda: "ok")

        assert result.success is True  # fail-open
        events = [
            e
            for e in cap_logs
            if e["event"] == "policy_composer.guard_execution_failed"
        ]
        assert len(events) == 1
        assert events[0]["guard_name"] == "failing_guard"
        assert events[0]["mode"] == "fail-open"

    @pytest.mark.asyncio
    async def test_async_guard_exception_logs_warning(self, async_composer):
        async_composer.add_guard(MockFailingGuard())

        async def func():
            return "ok"

        with capture_logs() as cap_logs:
            result = await async_composer.execute(func)

        assert result.success is True  # fail-open, sync-symmetric
        events = [
            e
            for e in cap_logs
            if e["event"] == "policy_composer.guard_execution_failed"
        ]
        assert len(events) == 1
        assert events[0]["guard_name"] == "failing_guard"
        assert events[0]["mode"] == "fail-open"


# =============================================================================
# Behavior verification — execute(): Policy chain
# =============================================================================


class TestComposerPolicyChainBehavior:
    """Policy chain execution behavior verification."""

    def test_single_policy_wraps_func(self, composer):
        """A single Policy wraps and runs func."""
        policy = MockPolicy("p1")
        composer.add(policy)
        result = composer.execute(lambda: "value")

        assert result.success is True
        assert result.value == "value"
        assert policy.execute_count == 1

    def test_multiple_policies_nesting_order(self, composer):
        """Policy add order is outer→inner execution order (the first one is outermost)."""
        execution_order = []

        class OrderTrackingPolicy:
            def __init__(self, label):
                self._label = label

            @property
            def name(self):
                return self._label

            def execute(self, func, *args, context=None, **kwargs):
                execution_order.append(f"{self._label}_before")
                try:
                    value = func(*args, **kwargs)
                    execution_order.append(f"{self._label}_after")
                    return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)
                except Exception as e:
                    return PolicyResult(
                        value=None, outcome=PolicyOutcome.FAILURE, error=e
                    )

        composer.add(OrderTrackingPolicy("outer"))
        composer.add(OrderTrackingPolicy("inner"))
        result = composer.execute(lambda: "ok")

        assert result.success is True
        assert execution_order == [
            "outer_before",
            "inner_before",
            "inner_after",
            "outer_after",
        ]

    def test_policy_failure_propagates(self, composer):
        """A func failure inside a Policy propagates the error upward."""
        err = RuntimeError("func failed")

        def failing():
            raise err

        policy = MockPolicy()
        composer.add(policy)
        result = composer.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err

    def test_rejecting_policy_returns_rejected(self, composer):
        """A Policy returning REJECTED → PolicyRejectedException → REJECTED outcome."""
        composer.add(MockRejectingPolicy())
        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, PolicyRejectedException)

    def test_executed_policies_tracked(self, composer):
        """Executed Policy names are recorded in executed_policies."""
        composer.add(MockPolicy("retry"))
        composer.add(MockPolicy("circuit_breaker"))
        result = composer.execute(lambda: "ok")

        assert "retry" in result.executed_policies
        assert "circuit_breaker" in result.executed_policies

    def test_context_passed_to_policy(self, composer):
        """The context is passed to Policy.execute()."""
        received_contexts = []

        class ContextCapturingPolicy:
            @property
            def name(self):
                return "ctx_policy"

            def execute(self, func, *args, context=None, **kwargs):
                received_contexts.append(context)
                value = func(*args, **kwargs)
                return PolicyResult(value=value, outcome=PolicyOutcome.SUCCESS)

        ctx = PolicyContext(order_id="ORD-123")
        composer.add(ContextCapturingPolicy())
        composer.execute(lambda: "ok", context=ctx)

        assert len(received_contexts) == 1
        assert received_contexts[0] is ctx


# =============================================================================
# Behavior verification — execute(): FallbackPolicy special handling in the chain
# =============================================================================


class TestComposerFallbackChainBehavior:
    """Behavior verification of FallbackPolicy special handling inside the Composer."""

    def test_fallback_applied_signal_produces_success_with_fallback(self, composer):
        """FallbackPolicy propagates SUCCESS_WITH_FALLBACK through _FallbackApplied in the chain."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fallback = FallbackPolicy(default_value="fallback_value")
        composer.add(fallback)

        err = RuntimeError("original error")
        result = composer.execute(lambda: (_ for _ in ()).throw(err))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback_value"

    def test_fallback_not_triggered_on_success(self, composer):
        """FallbackPolicy is not triggered when func succeeds."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fallback = FallbackPolicy(default_value="fallback_value")
        composer.add(fallback)
        result = composer.execute(lambda: "original_value")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "original_value"

    def test_fallback_with_predicate_not_matching(self, composer):
        """Fallback is not applied when the predicate returns False."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        # predicate: always False → Fallback disabled
        fallback = FallbackPolicy(
            default_value="fallback_value",
            predicate=lambda r: False,
        )
        composer.add(fallback)

        err = ValueError("test")
        result = composer.execute(lambda: (_ for _ in ()).throw(err))

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, ValueError)

    def test_policy_before_fallback_in_chain(self, composer):
        """Regular Policy + FallbackPolicy: Fallback applies when the Policy fails."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        composer.add(MockPolicy("wrapper"))
        composer.add(FallbackPolicy(default_value="fallback_result"))

        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback_result"


# =============================================================================
# Behavior verification — execute(): Hook invocation
# =============================================================================


class TestComposerHookBehavior:
    """Hook invocation behavior verification."""

    def test_on_success_called(self, composer):
        """Hook.on_success is called on success."""
        hook = MockHook()
        composer.add_hook(hook)
        composer.execute(lambda: "ok")

        assert len(hook.success_calls) == 1
        assert hook.success_calls[0][0] == "composer"

    def test_on_failure_called(self, composer):
        """Hook.on_failure is called on failure."""
        hook = MockHook()
        composer.add_hook(hook)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        assert len(hook.failure_calls) == 1
        assert hook.failure_calls[0][0] == "composer"

    def test_on_reject_called(self, composer):
        """Hook.on_reject is called when a Guard denies."""
        hook = MockHook()
        guard = MockGuard(allowed=False, reason="blocked", guard_name="test_guard")
        composer.add_guard(guard).add_hook(hook)
        composer.execute(lambda: "ok")

        assert len(hook.reject_calls) == 1
        assert hook.reject_calls[0] == ("test_guard", "blocked")

    def test_hook_fail_open_on_success(self, composer):
        """Hook.on_success raising does not affect the result (fail-open)."""
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"

    def test_hook_fail_open_on_failure(self, composer):
        """Hook.on_failure raising does not affect the result (fail-open)."""
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.FAILURE

    def test_hook_fail_open_on_reject(self, composer):
        """Hook.on_reject raising does not affect the result (fail-open)."""
        composer.add_guard(MockGuard(allowed=False, reason="x"))
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED

    def test_multiple_hooks_all_called(self, composer):
        """Every registered Hook is called."""
        hook1 = MockHook()
        hook2 = MockHook()
        composer.add_hook(hook1).add_hook(hook2)
        composer.execute(lambda: "ok")

        assert len(hook1.success_calls) == 1
        assert len(hook2.success_calls) == 1

    def test_total_duration_ms_set(self, composer):
        """After execute(), result.total_duration_ms is set to a value >= 0."""
        import time

        hook = MockHook()
        composer.add_hook(hook)

        def slow_func():
            time.sleep(0.01)
            return "ok"

        result = composer.execute(slow_func)
        assert result.total_duration_ms > 0


# =============================================================================
# Behavior verification — execute(): Sink handling
# =============================================================================


class TestComposerSinkBehavior:
    """Sink handling behavior verification."""

    def test_sink_called_on_failure(self, composer):
        """Sink.handle_failure is called on FAILURE."""
        sink = MockSink(sink_id="dlq-001")
        composer.add_sink(sink)
        err = RuntimeError("fail")
        result = composer.execute(lambda: (_ for _ in ()).throw(err))

        assert len(sink.calls) == 1
        assert sink.calls[0][0] is err
        assert result.metadata["sink_id"] == "dlq-001"

    def test_sink_not_called_on_success(self, composer):
        """The Sink is not called on SUCCESS."""
        sink = MockSink()
        composer.add_sink(sink)
        composer.execute(lambda: "ok")

        assert len(sink.calls) == 0

    def test_sink_not_called_on_rejected(self, composer):
        """The Sink is not called on REJECTED."""
        sink = MockSink()
        composer.add_guard(MockGuard(allowed=False, reason="blocked"))
        composer.add_sink(sink)
        composer.execute(lambda: "ok")

        assert len(sink.calls) == 0

    def test_sink_receives_context(self, composer):
        """The context is passed to the Sink."""
        sink = MockSink()
        ctx = PolicyContext(order_id="ORD-456")
        composer.add_sink(sink)
        composer.execute(
            lambda: (_ for _ in ()).throw(RuntimeError("fail")), context=ctx
        )

        assert len(sink.calls) == 1
        assert sink.calls[0][1] is ctx

    def test_sink_fail_open(self, composer):
        """The result is still returned when the Sink raises (fail-open)."""
        composer.add_sink(MockFailingSink())
        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.FAILURE

    def test_sink_id_none_not_stored(self, composer):
        """When the Sink returns None, no sink_id is added to metadata."""
        sink = MockSink(sink_id=None)
        composer.add_sink(sink)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert "sink_id" not in sink.calls[0][2].metadata

    def test_multiple_sinks_all_called(self, composer):
        """Every registered Sink is called."""
        sink1 = MockSink(sink_id="s1")
        sink2 = MockSink(sink_id="s2")
        composer.add_sink(sink1).add_sink(sink2)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        assert len(sink1.calls) == 1
        assert len(sink2.calls) == 1


# =============================================================================
# Behavior verification — compose() convenience function
# =============================================================================


class TestComposeFunctionBehavior:
    """compose() convenience function behavior verification."""

    def test_compose_returns_policy_composer(self):
        """compose() returns a PolicyComposer instance."""
        result = compose(MockPolicy("p1"))
        assert isinstance(result, PolicyComposer)

    def test_compose_adds_policies_in_order(self):
        """compose() adds Policies in argument order."""
        p1 = MockPolicy("p1")
        p2 = MockPolicy("p2")
        result = compose(p1, p2)
        assert result._policies == [p1, p2]

    def test_compose_no_policies(self):
        """compose() with no arguments returns an empty PolicyComposer."""
        result = compose()
        assert isinstance(result, PolicyComposer)
        assert result._policies == []

    def test_compose_chaining_with_guard(self):
        """compose().add_guard() chaining works."""
        guard = MockGuard(allowed=False, reason="blocked")
        result = compose(MockPolicy()).add_guard(guard).execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED


class TestComposeAsyncFunctionBehavior:
    """compose_async() convenience function behavior verification."""

    def test_compose_async_returns_async_composer(self):
        """compose_async() returns an AsyncPolicyComposer instance."""
        result = compose_async(MockAsyncPolicy("p1"))
        assert isinstance(result, AsyncPolicyComposer)

    def test_compose_async_adds_policies_in_order(self):
        """compose_async() adds Policies in argument order."""
        p1 = MockAsyncPolicy("p1")
        p2 = MockAsyncPolicy("p2")
        result = compose_async(p1, p2)
        assert result._policies == [p1, p2]

    def test_compose_async_no_policies(self):
        """compose_async() with no arguments returns an empty AsyncPolicyComposer."""
        result = compose_async()
        assert isinstance(result, AsyncPolicyComposer)
        assert result._policies == []


# =============================================================================
# Behavior verification — AsyncPolicyComposer.execute()
# =============================================================================


class TestAsyncComposerExecuteBehavior:
    """AsyncPolicyComposer.execute() behavior verification."""

    @pytest.mark.asyncio
    async def test_success_without_policies(self, async_composer):
        """With no Policy, a succeeding async func yields the SUCCESS outcome."""

        async def func():
            return 42

        result = await async_composer.execute(func)
        assert result.success is True
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_failure_without_policies(self, async_composer):
        """With no Policy, a failing async func yields the FAILURE outcome."""
        err = ValueError("async error")

        async def failing():
            raise err

        result = await async_composer.execute(failing)
        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (CircuitBreakerOpenError("inner_api"), PolicyOutcome.REJECTED),
            (TimeoutPolicyError(5.0), PolicyOutcome.TIMEOUT),
        ],
        ids=["open_circuit", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_raise_without_policies_is_classified_as_a_chain_classifies_it(
        self, async_composer, error, expected
    ):
        async def failing():
            raise error

        result = await async_composer.execute(failing)

        assert result.outcome == expected
        assert result.error is error

    @pytest.mark.asyncio
    async def test_guard_rejection(self, async_composer):
        """A Guard denial yields the REJECTED outcome."""
        guard = MockGuard(allowed=False, reason="denied")
        async_composer.add_guard(guard)

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["rejected_by"] == guard.name

    @pytest.mark.asyncio
    async def test_guard_fail_open(self, async_composer):
        """A Guard exception passes through fail-open."""
        async_composer.add_guard(MockFailingGuard())

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_single_async_policy(self, async_composer):
        """A single AsyncPolicy wraps and runs func."""
        policy = MockAsyncPolicy("async_p")
        async_composer.add(policy)

        async def func():
            return "async_value"

        result = await async_composer.execute(func)
        assert result.success is True
        assert result.value == "async_value"
        assert policy.execute_count == 1

    @pytest.mark.asyncio
    async def test_hook_on_success(self, async_composer):
        """Hook.on_success is called on success."""
        hook = MockHook()
        async_composer.add_hook(hook)

        async def func():
            return "ok"

        await async_composer.execute(func)
        assert len(hook.success_calls) == 1

    @pytest.mark.asyncio
    async def test_hook_on_failure(self, async_composer):
        """Hook.on_failure is called on failure."""
        hook = MockHook()
        async_composer.add_hook(hook)

        async def func():
            raise RuntimeError("fail")

        await async_composer.execute(func)
        assert len(hook.failure_calls) == 1

    @pytest.mark.asyncio
    async def test_sink_called_on_failure(self, async_composer):
        """The Sink is called on FAILURE."""
        sink = MockSink(sink_id="async-sink-001")
        async_composer.add_sink(sink)

        async def func():
            raise RuntimeError("fail")

        result = await async_composer.execute(func)
        assert len(sink.calls) == 1
        assert result.metadata["sink_id"] == "async-sink-001"

    @pytest.mark.asyncio
    async def test_sink_not_called_on_success(self, async_composer):
        """The Sink is not called on SUCCESS."""
        sink = MockSink()
        async_composer.add_sink(sink)

        async def func():
            return "ok"

        await async_composer.execute(func)
        assert len(sink.calls) == 0

    @pytest.mark.asyncio
    async def test_total_duration_ms_set(self, async_composer):
        """After execute(), total_duration_ms is set to a value >= 0."""

        async def func():
            await asyncio.sleep(0.02)
            return "ok"

        result = await async_composer.execute(func)
        assert result.total_duration_ms > 0

    @pytest.mark.asyncio
    async def test_context_propagated(self, async_composer):
        """The context is passed to the Guard and the Sink."""
        received_contexts = []

        class ContextCapturingGuard:
            @property
            def name(self):
                return "ctx_guard"

            def check(self, context=None):
                received_contexts.append(context)
                return GuardResult(allowed=True)

        ctx = PolicyContext(order_id="ASYNC-ORD-1")
        async_composer.add_guard(ContextCapturingGuard())

        async def func():
            return "ok"

        await async_composer.execute(func, context=ctx)
        assert len(received_contexts) == 1
        assert received_contexts[0] is ctx

    @pytest.mark.asyncio
    async def test_executed_policies_tracked(self, async_composer):
        """Executed Policy names are recorded in executed_policies."""
        async_composer.add(MockAsyncPolicy("async_retry"))
        async_composer.add(MockAsyncPolicy("async_cb"))

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert "async_retry" in result.executed_policies
        assert "async_cb" in result.executed_policies


# =============================================================================
# Behavior verification — AsyncPolicyComposer: AsyncFallbackPolicy special handling in the chain
# =============================================================================


class TestAsyncComposerFallbackChainBehavior:
    """Behavior verification of AsyncFallbackPolicy special handling inside AsyncPolicyComposer."""

    @pytest.mark.asyncio
    async def test_async_fallback_applied_on_failure(self, async_composer):
        """AsyncFallbackPolicy propagates SUCCESS_WITH_FALLBACK through _FallbackApplied in the chain."""
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        fallback = AsyncFallbackPolicy(default_value="async_fallback")
        async_composer.add(fallback)

        async def failing():
            raise RuntimeError("async fail")

        result = await async_composer.execute(failing)
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "async_fallback"

    @pytest.mark.asyncio
    async def test_async_fallback_not_triggered_on_success(self, async_composer):
        """AsyncFallbackPolicy is not triggered when func succeeds."""
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        fallback = AsyncFallbackPolicy(default_value="fallback")
        async_composer.add(fallback)

        async def func():
            return "original"

        result = await async_composer.execute(func)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "original"


# =============================================================================
# Behavior — TIMEOUT outcome mapping (449)
# =============================================================================


class TestComposerTimeoutBehavior:
    """PolicyComposer maps TimeoutPolicyError → PolicyOutcome.TIMEOUT."""

    def test_timeout_policy_error_maps_to_timeout_outcome(self, composer):
        """TimeoutPolicyError from inner chain produces TIMEOUT outcome."""
        from baldur.core.exceptions import TimeoutPolicyError

        class TimeoutRaisingPolicy:
            @property
            def name(self):
                return "timeout"

            def execute(self, func, *args, context=None, **kwargs):
                raise TimeoutPolicyError(5.0)

        composer.add(TimeoutRaisingPolicy())
        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert isinstance(result.error, TimeoutPolicyError)
        assert result.error.timeout_seconds == 5.0

    def test_timeout_outcome_distinct_from_failure(self, composer):
        """TIMEOUT is not conflated with generic FAILURE."""
        from baldur.core.exceptions import TimeoutPolicyError

        class TimeoutRaisingPolicy:
            @property
            def name(self):
                return "timeout"

            def execute(self, func, *args, context=None, **kwargs):
                raise TimeoutPolicyError(10.0)

        composer.add(TimeoutRaisingPolicy())
        result = composer.execute(lambda: "ok")

        assert result.outcome != PolicyOutcome.FAILURE
        assert result.outcome == PolicyOutcome.TIMEOUT


class TestAsyncComposerTimeoutBehavior:
    """AsyncPolicyComposer maps TimeoutPolicyError → PolicyOutcome.TIMEOUT."""

    @pytest.mark.asyncio
    async def test_timeout_policy_error_maps_to_timeout_outcome(self, async_composer):
        """TimeoutPolicyError from async chain produces TIMEOUT outcome."""
        from baldur.core.exceptions import TimeoutPolicyError

        class AsyncTimeoutRaisingPolicy:
            @property
            def name(self):
                return "timeout"

            async def execute(self, func, *args, context=None, **kwargs):
                raise TimeoutPolicyError(3.0)

        async_composer.add(AsyncTimeoutRaisingPolicy())

        async def fn():
            return "ok"

        result = await async_composer.execute(fn)

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert isinstance(result.error, TimeoutPolicyError)
        assert result.error.timeout_seconds == 3.0


# =============================================================================
# Behavior — _classify_exception_outcome shared classifier (705 D6)
# =============================================================================


class TestClassifyExceptionOutcome:
    """The shared classifier maps a chain exception to its terminal outcome.

    One source feeds the composer terminal ladders, the fallback wrappers'
    synthesized predicate input, and standalone ``FallbackPolicy.execute``. A
    ``PolicyRejectedException`` (incl. the real ``CircuitBreakerOpenError``
    subclass) → REJECTED; a ``TimeoutPolicyError`` → TIMEOUT; anything else →
    FAILURE.
    """

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (PolicyRejectedException("rejected"), PolicyOutcome.REJECTED),
            (CircuitBreakerOpenError("payment"), PolicyOutcome.REJECTED),
            (TimeoutPolicyError(5.0), PolicyOutcome.TIMEOUT),
            (RuntimeError("boom"), PolicyOutcome.FAILURE),
            (ValueError("bad"), PolicyOutcome.FAILURE),
            (KeyError("missing"), PolicyOutcome.FAILURE),
        ],
    )
    def test_classify_maps_exception_to_terminal_outcome(self, exc, expected):
        """Each exception class resolves to its documented PolicyOutcome."""
        assert _classify_exception_outcome(exc) == expected

    def test_circuit_breaker_open_is_a_policy_rejected_subclass(self):
        """Guards the REJECTED mapping: CircuitBreakerOpenError IS a
        PolicyRejectedException, so it classifies as REJECTED, not FAILURE."""
        exc = CircuitBreakerOpenError("payment")
        assert isinstance(exc, PolicyRejectedException)
        assert _classify_exception_outcome(exc) == PolicyOutcome.REJECTED


# =============================================================================
# Behavior — fallback_wrapper predicate-input fidelity + D7 metadata merge (705)
# =============================================================================


class _MetadataFailingPolicy:
    """A regular policy that attaches metadata and surfaces the caught error.

    Mirrors how CB/Retry return a PolicyResult(FAILURE, error=…, metadata=…):
    the composer's ``policy_wrapper`` merges the metadata into ``chain_metadata``
    before re-raising the error, so the outermost fallback stage can absorb it.
    """

    @property
    def name(self) -> str:
        return "meta_inner"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        try:
            value = func(*args, **kwargs)
            return PolicyResult(
                value=value,
                outcome=PolicyOutcome.SUCCESS,
                metadata={"inner_key": "inner_val"},
            )
        except Exception as e:
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                metadata={"inner_key": "inner_val"},
            )


class TestComposerFallbackInputFidelity:
    """The fallback wrapper feeds the predicate the TRUE classified outcome
    (not an always-FAILURE lie), and the ``_FallbackApplied`` terminal merges
    the inner chain metadata under the fallback metadata (705 D6/D7)."""

    def _throw(self, exc: BaseException) -> Callable[[], Any]:
        def _raise() -> Any:
            raise exc

        return _raise

    def test_predicate_sees_timeout_outcome_and_activates(self, composer):
        """A TIMEOUT-only predicate activates when the chain raises a timeout —
        proving the wrapper synthesizes TIMEOUT, not FAILURE."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fb = FallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )
        composer.add(fb)

        result = composer.execute(self._throw(TimeoutPolicyError(1.0)))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "degraded"

    def test_predicate_sees_failure_outcome_and_declines(self, composer):
        """The same TIMEOUT-only predicate declines a plain failure — the
        wrapper passes FAILURE for a RuntimeError, so the fallback is skipped."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fb = FallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.TIMEOUT,
        )
        composer.add(fb)

        result = composer.execute(self._throw(RuntimeError("boom")))

        assert result.outcome == PolicyOutcome.FAILURE
        assert isinstance(result.error, RuntimeError)

    def test_predicate_sees_rejected_outcome_and_activates(self, composer):
        """A REJECTED-only predicate activates on a PolicyRejectedException —
        proving REJECTED classification survives into the predicate input."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fb = FallbackPolicy(
            default_value="degraded",
            predicate=lambda r: r.outcome == PolicyOutcome.REJECTED,
        )
        composer.add(fb)

        result = composer.execute(self._throw(PolicyRejectedException("nope")))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "degraded"

    def test_fallback_terminal_merges_inner_chain_metadata(self, composer):
        """D7: the served-fallback terminal carries BOTH the inner policy's
        metadata (from chain_metadata) and the fallback's own metadata."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        composer.add(FallbackPolicy(default_value="degraded"))
        composer.add(_MetadataFailingPolicy())

        result = composer.execute(self._throw(RuntimeError("boom")))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        # Inner-policy metadata survives the fallback absorb (D7 merge)...
        assert result.metadata["inner_key"] == "inner_val"
        # ...alongside the fallback's own served-path metadata.
        assert result.metadata["fallback_used"] is True

    def test_fallback_metadata_wins_on_key_collision(self, composer):
        """D7: on a colliding key the fallback result's value wins (last-write),
        matching ``{**chain_metadata, **fb_result.metadata}``."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        class _CollidingPolicy(_MetadataFailingPolicy):
            def execute(self, func, *args, context=None, **kwargs):
                try:
                    return PolicyResult(
                        value=func(*args, **kwargs), outcome=PolicyOutcome.SUCCESS
                    )
                except Exception as e:
                    # Collides on the ``fallback_used`` key that the fallback
                    # terminal also sets — the fallback's True must win.
                    return PolicyResult(
                        value=None,
                        outcome=PolicyOutcome.FAILURE,
                        error=e,
                        metadata={"fallback_used": "inner-should-lose"},
                    )

        composer.add(FallbackPolicy(default_value="degraded"))
        composer.add(_CollidingPolicy())

        result = composer.execute(self._throw(RuntimeError("boom")))

        assert result.metadata["fallback_used"] is True


# =============================================================================
# Behavior — open-circuit rejection capture (sink terminal routing)
# =============================================================================


class _CircuitOpenPolicy:
    """Policy that rejects the way an OPEN circuit breaker does.

    Mirrors the real breaker's reject shape: a REJECTED ``PolicyResult``
    carrying a ``CircuitBreakerOpenError`` plus the breaker's own metadata
    keys. The chain wrapper re-raises that error, so the composer terminal
    classifies it as REJECTED exactly as it does in production — which is what
    makes the routing under test the production routing.
    """

    def __init__(self, service_name: str = "payment_api") -> None:
        self._service_name = service_name

    @property
    def name(self) -> str:
        return "circuit_breaker"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            error=CircuitBreakerOpenError(self._service_name),
            metadata={"service_name": self._service_name, "state": "open"},
        )


class _AsyncCircuitOpenPolicy(_CircuitOpenPolicy):
    """Async twin of ``_CircuitOpenPolicy``."""

    async def execute(  # type: ignore[override]
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            error=CircuitBreakerOpenError(self._service_name),
            metadata={"service_name": self._service_name, "state": "open"},
        )


class _BulkheadFullPolicy:
    """Policy that rejects on a full bulkhead.

    Mirrors the real bulkhead's reject shape: a REJECTED ``PolicyResult``
    carrying a ``BulkheadFullError`` — a call that failed for want of capacity,
    which open-circuit arming leaves alone and failure arming parks.
    """

    @property
    def name(self) -> str:
        return "bulkhead"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            error=BulkheadFullError("payment_api", max_concurrent=2, active_count=2),
        )


class _ThreadRecordingSink:
    """Sink that records the thread it ran on, so the async composer's offload
    channel is observable without mocking the adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple[Exception, PolicyContext | None, PolicyResult]] = []
        self.thread_idents: list[int] = []

    def handle_failure(
        self,
        error: Exception,
        context: PolicyContext | None,
        policy_result: PolicyResult,
    ) -> str | None:
        self.thread_idents.append(threading.get_ident())
        self.calls.append((error, context, policy_result))
        return "sink-oc"


def _throwing(exc: BaseException) -> Callable[[], Any]:
    """Callable that raises ``exc`` — the exception-terminal entry point."""

    def _raise() -> Any:
        raise exc

    return _raise


class TestSinkTerminalRoutingBehavior:
    """``_terminal_reaches_sinks`` / ``_is_open_circuit_rejection`` decision table.

    Each arming flag is the boundary for its own terminals only: an
    open-circuit rejection crosses on open-circuit arming; a TIMEOUT and a
    rejection carrying any other error cross on failure arming; neither flag
    widens the other's terminals, and a guard veto never crosses.
    """

    @pytest.mark.parametrize(
        ("outcome", "error", "armed_oc", "armed_failures", "expected"),
        [
            # FAILURE is the historical sink terminal — arming is irrelevant.
            (PolicyOutcome.FAILURE, RuntimeError("boom"), False, False, True),
            (PolicyOutcome.FAILURE, RuntimeError("boom"), True, False, True),
            (PolicyOutcome.FAILURE, RuntimeError("boom"), False, True, True),
            # The open-circuit boundary: same terminal, arming off then on.
            (
                PolicyOutcome.REJECTED,
                CircuitBreakerOpenError("payment_api"),
                False,
                False,
                False,
            ),
            (
                PolicyOutcome.REJECTED,
                CircuitBreakerOpenError("payment_api"),
                True,
                False,
                True,
            ),
            # Failure arming does not widen open-circuit capture.
            (
                PolicyOutcome.REJECTED,
                CircuitBreakerOpenError("payment_api"),
                False,
                True,
                False,
            ),
            # Guard veto — REJECTED with no error at all, under either arming.
            (PolicyOutcome.REJECTED, None, True, False, False),
            (PolicyOutcome.REJECTED, None, False, True, False),
            # Other error-carrying rejections: open-circuit arming leaves them
            # out, failure arming lets them cross.
            (
                PolicyOutcome.REJECTED,
                BulkheadFullError("payment_api", 2, 2),
                True,
                False,
                False,
            ),
            (
                PolicyOutcome.REJECTED,
                BulkheadFullError("payment_api", 2, 2),
                False,
                True,
                True,
            ),
            (
                PolicyOutcome.REJECTED,
                PolicyRejectedException("blocked"),
                True,
                False,
                False,
            ),
            (
                PolicyOutcome.REJECTED,
                PolicyRejectedException("blocked"),
                False,
                True,
                True,
            ),
            # The TIMEOUT boundary: open-circuit arming alone leaves it out;
            # failure arming lets it cross.
            (PolicyOutcome.TIMEOUT, TimeoutPolicyError(5.0), True, False, False),
            (PolicyOutcome.TIMEOUT, TimeoutPolicyError(5.0), False, True, True),
            # A served fallback answered the caller — no flag routes it.
            (PolicyOutcome.SUCCESS_WITH_FALLBACK, None, True, True, False),
            (PolicyOutcome.SUCCESS, None, True, False, False),
        ],
        ids=[
            "failure_unarmed",
            "failure_oc_armed",
            "failure_failures_armed",
            "open_circuit_unarmed",
            "open_circuit_oc_armed",
            "open_circuit_not_widened_by_failure_arming",
            "guard_veto_oc_armed",
            "guard_veto_failures_armed",
            "bulkhead_full_oc_armed",
            "bulkhead_full_failures_armed",
            "policy_rejected_oc_armed",
            "policy_rejected_failures_armed",
            "timeout_oc_armed",
            "timeout_failures_armed",
            "served_fallback_fully_armed",
            "success",
        ],
    )
    def test_terminal_routing_depends_on_outcome_error_and_arming(
        self, outcome, error, armed_oc, armed_failures, expected
    ):
        result = PolicyResult(value=None, outcome=outcome, error=error)

        assert (
            _terminal_reaches_sinks(
                result,
                captures_open_circuit_rejections=armed_oc,
                captures_failures=armed_failures,
            )
            is expected
        )

    @pytest.mark.parametrize(
        ("outcome", "error", "expected"),
        [
            (PolicyOutcome.REJECTED, CircuitBreakerOpenError("payment_api"), True),
            (PolicyOutcome.REJECTED, None, False),
            (PolicyOutcome.REJECTED, BulkheadFullError("payment_api", 2, 2), False),
            (PolicyOutcome.REJECTED, PolicyRejectedException("blocked"), False),
            # The outcome half of the predicate: the same error on a non-REJECTED
            # terminal is not an open-circuit rejection.
            (PolicyOutcome.FAILURE, CircuitBreakerOpenError("payment_api"), False),
        ],
    )
    def test_open_circuit_rejection_predicate_narrows_rejected(
        self, outcome, error, expected
    ):
        result = PolicyResult(value=None, outcome=outcome, error=error)

        assert _is_open_circuit_rejection(result) is expected

    @pytest.mark.parametrize(
        ("outcome", "error", "expected"),
        [
            (PolicyOutcome.FAILURE, RuntimeError("boom"), True),
            (PolicyOutcome.TIMEOUT, TimeoutPolicyError(5.0), True),
            (PolicyOutcome.REJECTED, BulkheadFullError("payment_api", 2, 2), True),
            # A stage that ended without an error object: the composer
            # synthesizes this rejection, and the call still failed.
            (PolicyOutcome.REJECTED, PolicyRejectedException("synthesized"), True),
            # The open circuit has its own lane; a guard veto carries no error.
            (PolicyOutcome.REJECTED, CircuitBreakerOpenError("payment_api"), False),
            (PolicyOutcome.REJECTED, None, False),
            (PolicyOutcome.SUCCESS, None, False),
            (PolicyOutcome.SUCCESS_WITH_FALLBACK, None, False),
        ],
        ids=[
            "failure",
            "timeout",
            "bulkhead_full",
            "synthesized_rejection",
            "open_circuit",
            "guard_veto",
            "success",
            "served_fallback",
        ],
    )
    def test_failed_call_predicate_takes_every_error_carrying_terminal_but_the_open_circuit(
        self, outcome, error, expected
    ):
        """The one predicate routing and fill-in share, so they cannot disagree
        on which terminals are failed calls."""
        result = PolicyResult(value=None, outcome=outcome, error=error)

        assert _is_failed_call(result) is expected


class TestArmFailureVerdictContract:
    """``_arm_failure_verdict`` writes the retry stage's exhaustion verdict,
    sized for the attempts the call made, and files a verdict that names only
    the placeholder domain under the call-site name — the spec values are the
    entry's shape."""

    @pytest.mark.parametrize(
        ("outcome", "error"),
        [
            (PolicyOutcome.FAILURE, RuntimeError("upstream 500")),
            (PolicyOutcome.TIMEOUT, TimeoutPolicyError(5.0)),
            (PolicyOutcome.REJECTED, BulkheadFullError("payment_api", 2, 2)),
            (PolicyOutcome.REJECTED, PolicyRejectedException("synthesized")),
        ],
        ids=["failure", "timeout", "bulkhead_full", "synthesized_rejection"],
    )
    def test_unmarked_failed_call_gains_the_single_attempt_verdict(
        self, outcome, error
    ):
        result = PolicyResult(value=None, outcome=outcome, error=error)

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == {
            "should_dlq": True,
            "domain": "summarize",
            "max_attempts": 1,
            "retry_history": [],
            "reason": "max_attempts",
        }

    @pytest.mark.parametrize(
        ("total_attempts", "expected"),
        [(0, 1), (1, 1), (3, 3)],
        ids=["no_attempt_reported", "one_attempt", "bridge_count"],
    )
    def test_written_verdict_counts_the_attempts_the_call_made(
        self, total_attempts, expected
    ):
        """A caller-supplied retry stage writes no verdict; the one written for
        it carries that stage's own attempt count, never fewer than one."""
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=RuntimeError("upstream 500"),
            total_attempts=total_attempts,
        )

        _arm_failure_verdict(result, "summarize")

        assert result.metadata["max_attempts"] == expected

    @pytest.mark.parametrize(
        "stage_domain",
        [{"domain": "default"}, {}, {"domain": None}],
        ids=["placeholder", "missing", "none"],
    )
    def test_placeholder_domain_stage_verdict_is_filed_under_the_call_site_name(
        self, stage_domain
    ):
        """Only the domain is completed; the stage's decision and counts stay."""
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=RuntimeError("boom"),
            metadata={"should_dlq": True, "max_attempts": 3, **stage_domain},
        )

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == {
            "should_dlq": True,
            "max_attempts": 3,
            "domain": "summarize",
        }

    def test_placeholder_domain_declined_verdict_keeps_its_decision(self):
        """A stage's "do not store" survives the domain completion, so the
        sink still records it as declined under the name an operator queries."""
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=RuntimeError("boom"),
            metadata={"should_dlq": False, "domain": "default", "max_attempts": 2},
        )

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == {
            "should_dlq": False,
            "domain": "summarize",
            "max_attempts": 2,
        }

    def test_arming_keeps_the_keys_a_stage_already_merged(self):
        """The verdict joins the timeout stage's own keys; it replaces nothing."""
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.TIMEOUT,
            error=TimeoutPolicyError(5.0),
            metadata={"timeout_seconds": 5.0},
        )

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == {
            "timeout_seconds": 5.0,
            "should_dlq": True,
            "domain": "summarize",
            "max_attempts": 1,
            "retry_history": [],
            "reason": "max_attempts",
        }

    @pytest.mark.parametrize("verdict", [False, True], ids=["declined", "accepted"])
    def test_a_verdict_a_stage_wrote_is_left_as_written(self, verdict):
        """A present ``should_dlq`` means a stage decided — its domain and
        attempt count win too, so an explicit ``enable_dlq=False`` survives."""
        stage_metadata = {
            "should_dlq": verdict,
            "domain": "retry_domain",
            "max_attempts": 3,
        }
        result = PolicyResult(
            value=None,
            outcome=PolicyOutcome.FAILURE,
            error=RuntimeError("boom"),
            metadata=dict(stage_metadata),
        )

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == stage_metadata

    @pytest.mark.parametrize(
        ("outcome", "error"),
        [
            (PolicyOutcome.REJECTED, CircuitBreakerOpenError("payment_api")),
            (PolicyOutcome.REJECTED, None),
            (PolicyOutcome.SUCCESS, None),
            (PolicyOutcome.SUCCESS_WITH_FALLBACK, None),
        ],
        ids=["open_circuit_rejection", "guard_veto", "success", "served_fallback"],
    )
    def test_other_outcomes_are_left_untouched(self, outcome, error):
        result = PolicyResult(value=None, outcome=outcome, error=error)

        _arm_failure_verdict(result, "summarize")

        assert result.metadata == {}


class TestComposerRejectionCaptureBehavior:
    """``PolicyComposer.execute`` delivers an armed open-circuit rejection."""

    def test_armed_composer_delivers_open_circuit_rejection_to_sink(self, composer):
        # Given an armed composer whose breaker is OPEN
        sink = MockSink(sink_id="dlq-oc-1")
        composer.add(_CircuitOpenPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()

        # When the call is rejected
        result = composer.execute(lambda: "never runs")

        # Then the rejection reached the sink and the terminal is unchanged
        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)
        assert len(sink.calls) == 1
        assert sink.calls[0][0] is result.error

    def test_unarmed_composer_leaves_open_circuit_rejection_uncaptured(self, composer):
        """Negative half: without arming the same rejection reaches no sink."""
        sink = MockSink()
        composer.add(_CircuitOpenPolicy()).add_sink(sink)

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []

    def test_armed_composer_still_skips_guard_rejection(self, composer):
        """Re-pin: a guard veto carries no error, so it reaches no sink even armed."""
        sink = MockSink()
        composer.add_guard(MockGuard(allowed=False, reason="blocked"))
        composer.add_sink(sink)
        composer.capture_open_circuit_rejections()

        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.error is None
        assert sink.calls == []

    def test_armed_composer_skips_bulkhead_rejection(self, composer):
        """A bulkhead-full REJECTED terminal is not open-circuit capture."""
        sink = MockSink()
        composer.add(_BulkheadFullPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, BulkheadFullError)
        assert sink.calls == []

    def test_armed_composer_skips_timeout_terminal(self, composer):
        """A TIMEOUT terminal is a different loss shape — no capture."""
        sink = MockSink()
        # A stage in the chain makes this the shape a facade call builds; an
        # empty chain classifies the raise the same way.
        composer.add(MockPolicy("wrapper")).add_sink(sink)
        composer.capture_open_circuit_rejections()

        result = composer.execute(_throwing(TimeoutPolicyError(5.0)))

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert sink.calls == []

    def test_armed_composer_skips_served_fallback(self, composer):
        """A served fallback answers the caller, so nothing is parked."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        sink = MockSink()
        composer.add(FallbackPolicy(default_value="degraded"))
        composer.add(_CircuitOpenPolicy())
        composer.add_sink(sink)
        composer.capture_open_circuit_rejections()

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "degraded"
        assert sink.calls == []

    def test_sink_receives_rejecting_breaker_metadata(self, composer):
        """The sink needs the rejecting breaker's own keys to build the entry."""
        sink = MockSink()
        composer.add(_CircuitOpenPolicy(service_name="charge_gateway")).add_sink(sink)
        composer.capture_open_circuit_rejections()

        composer.execute(lambda: "never runs")

        delivered_result = sink.calls[0][2]
        assert delivered_result.metadata["service_name"] == "charge_gateway"
        assert delivered_result.metadata["state"] == "open"

    def test_sink_receives_context_on_rejection(self, composer):
        """The call's PolicyContext travels with the rejection — it carries the
        replay payload the entry is built from."""
        sink = MockSink()
        ctx = PolicyContext(order_id="ORD-9")
        composer.add(_CircuitOpenPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()

        composer.execute(lambda: "never runs", context=ctx)

        assert sink.calls[0][1] is ctx

    def test_rejection_capture_writes_sink_id_onto_result_metadata(self, composer):
        sink = MockSink(sink_id="dlq-oc-7")
        composer.add(_CircuitOpenPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()

        result = composer.execute(lambda: "never runs")

        assert result.metadata["sink_id"] == "dlq-oc-7"

    def test_sink_failure_leaves_the_rejection_intact(self, composer):
        """Capture is a side effect: a raising sink must not change the answer."""
        composer.add(_CircuitOpenPolicy()).add_sink(MockFailingSink())
        composer.capture_open_circuit_rejections()

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)

    def test_capture_open_circuit_rejections_returns_self_for_chaining(self, composer):
        assert composer.capture_open_circuit_rejections() is composer


class TestAsyncComposerRejectionCaptureBehavior:
    """``AsyncPolicyComposer`` routes the same terminal through its normalized
    sink channel — so a sync sink keeps running off the event loop."""

    @staticmethod
    async def _never_runs() -> str:
        return "never runs"

    def test_armed_async_composer_delivers_open_circuit_rejection_to_sink(
        self, async_composer
    ):
        sink = MockSink(sink_id="dlq-oc-async")
        async_composer.add(_AsyncCircuitOpenPolicy()).add_sink(sink)
        async_composer.capture_open_circuit_rejections()

        result = asyncio.run(async_composer.execute(self._never_runs))

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)
        assert len(sink.calls) == 1
        assert result.metadata["sink_id"] == "dlq-oc-async"

    def test_unarmed_async_composer_leaves_the_rejection_uncaptured(
        self, async_composer
    ):
        sink = MockSink()
        async_composer.add(_AsyncCircuitOpenPolicy()).add_sink(sink)

        result = asyncio.run(async_composer.execute(self._never_runs))

        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []

    def test_armed_async_composer_still_skips_guard_rejection(self, async_composer):
        sink = MockSink()
        async_composer.add_guard(MockGuard(allowed=False, reason="blocked"))
        async_composer.add_sink(sink)
        async_composer.capture_open_circuit_rejections()

        result = asyncio.run(async_composer.execute(self._never_runs))

        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []

    def test_rejection_capture_runs_the_sync_sink_off_the_event_loop(
        self, async_composer
    ):
        """Dispatch-channel identity: the rejection travels the add-time
        normalized channel, so the sync sink runs on the offload thread rather
        than blocking the loop that is still serving other requests."""
        sink = _ThreadRecordingSink()
        async_composer.add(_AsyncCircuitOpenPolicy()).add_sink(sink)
        async_composer.capture_open_circuit_rejections()

        loop_thread: list[int] = []

        async def _run():
            loop_thread.append(threading.get_ident())
            return await async_composer.execute(self._never_runs)

        asyncio.run(_run())

        assert len(sink.thread_idents) == 1
        assert sink.thread_idents[0] != loop_thread[0]

    def test_sync_sink_is_wrapped_by_the_offload_adapter_at_add_time(
        self, async_composer
    ):
        """The offload is inherited by construction — the composer stores the
        adapter, not the raw sink, so no dispatch path can bypass it."""
        sink = MockSink()
        async_composer.add_sink(sink)

        assert isinstance(async_composer._sinks[0], _SyncSinkToAsyncAdapter)

    def test_capture_open_circuit_rejections_returns_self_for_chaining(
        self, async_composer
    ):
        assert async_composer.capture_open_circuit_rejections() is async_composer


# =============================================================================
# Behavior — failure capture: the composer completes the store verdict
# =============================================================================


class _VerdictStage:
    """A stage that decides the store verdict itself, as a retry stage does
    when its attempts run out: it runs the call once and, on failure, reports
    its own ``should_dlq`` and ``domain``."""

    def __init__(self, *, should_dlq: bool, domain: str = "retry_domain") -> None:
        self._metadata = {"should_dlq": should_dlq, "domain": domain}

    @property
    def name(self) -> str:
        return "retry"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        try:
            return PolicyResult(
                value=func(*args, **kwargs), outcome=PolicyOutcome.SUCCESS
            )
        except Exception as e:
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                metadata=dict(self._metadata),
            )


class _AsyncVerdictStage(_VerdictStage):
    """Async twin of ``_VerdictStage``."""

    async def execute(  # type: ignore[override]
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        try:
            return PolicyResult(
                value=await func(*args, **kwargs), outcome=PolicyOutcome.SUCCESS
            )
        except Exception as e:
            return PolicyResult(
                value=None,
                outcome=PolicyOutcome.FAILURE,
                error=e,
                metadata=dict(self._metadata),
            )


class _AsyncBulkheadFullPolicy(_BulkheadFullPolicy):
    """Async twin of ``_BulkheadFullPolicy``."""

    async def execute(  # type: ignore[override]
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None,
            outcome=PolicyOutcome.REJECTED,
            error=BulkheadFullError("payment_api", max_concurrent=2, active_count=2),
        )


class _ErrorlessFailureStage:
    """A caller-supplied stage that ends in FAILURE without an error object
    after several attempts — the composer synthesizes the rejection the
    caller receives."""

    @property
    def name(self) -> str:
        return "custom_retry"

    def execute(
        self,
        func: Callable[..., Any],
        *args: Any,
        context: PolicyContext | None = None,
        **kwargs: Any,
    ) -> PolicyResult:
        return PolicyResult(
            value=None, outcome=PolicyOutcome.FAILURE, error=None, total_attempts=3
        )


class TestComposerFailureCaptureBehavior:
    """``PolicyComposer.execute`` completes the verdict on every failed call
    under the armed name and delivers it — the TIMEOUT and error-carrying
    rejection terminals included."""

    def test_armed_composer_delivers_failure_marked_for_its_domain(self, composer):
        # Given an armed composer with no stage — the empty-chain terminal
        sink = MockSink()
        composer.add_sink(sink).capture_failures("summarize")

        # When the call raises
        result = composer.execute(_throwing(RuntimeError("upstream 500")))

        # Then the sink received that terminal, carrying the verdict
        assert result.outcome == PolicyOutcome.FAILURE
        assert len(sink.calls) == 1
        assert sink.calls[0][2] is result
        assert result.metadata["should_dlq"] is True
        assert result.metadata["domain"] == "summarize"

    def test_armed_composer_delivers_timeout_terminal(self, composer):
        # A stage in the chain makes this the shape a facade call builds; an
        # empty chain classifies the raise the same way.
        sink = MockSink()
        composer.add(MockPolicy("wrapper")).add_sink(sink)
        composer.capture_failures("summarize")

        result = composer.execute(_throwing(TimeoutPolicyError(5.0)))

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert len(sink.calls) == 1
        assert isinstance(sink.calls[0][0], TimeoutPolicyError)
        assert sink.calls[0][2].metadata["should_dlq"] is True
        assert sink.calls[0][2].metadata["domain"] == "summarize"

    def test_armed_composer_delivers_bulkhead_full_rejection_once_with_the_verdict(
        self, composer
    ):
        sink = MockSink()
        composer.add(_BulkheadFullPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()
        composer.capture_failures("inventory")

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert len(sink.calls) == 1
        assert isinstance(sink.calls[0][0], BulkheadFullError)
        assert sink.calls[0][2].metadata["should_dlq"] is True
        assert sink.calls[0][2].metadata["domain"] == "inventory"
        assert sink.calls[0][2].metadata["max_attempts"] == 1

    def test_retry_stage_that_gave_up_on_a_full_bulkhead_is_parked_under_the_name(
        self, composer
    ):
        """The ``ha_pipeline`` shape: a full bulkhead inside a retry stage. The
        stage exhausts on the retryable refusal and writes its verdict under
        the placeholder domain; the composer files it under the armed name."""
        from baldur.services.retry_handler.models import RetryPolicyConfig
        from baldur.services.retry_handler.policy import RetryPolicy

        # Given a two-attempt retry stage around a bulkhead that is full
        sink = MockSink()
        composer.add(
            RetryPolicy(
                config=RetryPolicyConfig(max_attempts=2), sleeper=lambda _: None
            )
        )
        composer.add(_BulkheadFullPolicy()).add_sink(sink)
        composer.capture_failures("inventory")

        # When the call is refused on both attempts
        result = composer.execute(lambda: "never runs")

        # Then one delivery carries the stage's verdict under the armed name
        assert result.outcome == PolicyOutcome.REJECTED
        assert len(sink.calls) == 1
        delivered = sink.calls[0][2].metadata
        assert delivered["should_dlq"] is True
        assert delivered["domain"] == "inventory"
        assert delivered["max_attempts"] == 2

    def test_errorless_stage_failure_is_parked_as_a_synthesized_rejection(
        self, composer
    ):
        """No error object comes back, so the caller receives a synthesized
        rejection; the call still failed and is parked with the stage's count."""
        sink = MockSink()
        composer.add(_ErrorlessFailureStage()).add_sink(sink)
        composer.capture_failures("summarize")

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, PolicyRejectedException)
        assert len(sink.calls) == 1
        assert sink.calls[0][2].metadata["domain"] == "summarize"
        assert sink.calls[0][2].metadata["max_attempts"] == 3

    def test_failure_armed_composer_leaves_guard_veto_undelivered(self, composer):
        """A guard's refusal carries no error: the guard logs it, nothing parks."""
        sink = MockSink()
        composer.add_guard(MockGuard(allowed=False, reason="duplicate"))
        composer.add(MockPolicy("wrapper")).add_sink(sink)
        composer.capture_failures("summarize")

        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.REJECTED
        assert result.error is None
        assert sink.calls == []
        assert "should_dlq" not in result.metadata

    def test_unarmed_composer_delivers_failure_without_a_verdict(self, composer):
        """Negative half: without arming nothing writes the verdict, so the DLQ
        sink would drop this terminal."""
        sink = MockSink()
        composer.add_sink(sink)

        composer.execute(_throwing(RuntimeError("upstream 500")))

        assert len(sink.calls) == 1
        assert "should_dlq" not in sink.calls[0][2].metadata

    def test_unarmed_composer_leaves_bulkhead_full_rejection_undelivered(
        self, composer
    ):
        """Negative half of the bulkhead case: only failure arming widens the
        rejections that reach a sink."""
        sink = MockSink()
        composer.add(_BulkheadFullPolicy()).add_sink(sink)

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []

    def test_armed_composer_keeps_a_stage_verdict_that_declines(self, composer):
        sink = MockSink()
        composer.add(_VerdictStage(should_dlq=False)).add_sink(sink)
        composer.capture_failures("summarize")

        composer.execute(_throwing(RuntimeError("upstream 500")))

        delivered = sink.calls[0][2]
        assert delivered.metadata["should_dlq"] is False
        assert delivered.metadata["domain"] == "retry_domain"

    def test_armed_composer_files_a_placeholder_domain_verdict_under_its_name(
        self, composer
    ):
        sink = MockSink()
        composer.add(_VerdictStage(should_dlq=True, domain="default")).add_sink(sink)
        composer.capture_failures("summarize")

        composer.execute(_throwing(RuntimeError("upstream 500")))

        delivered = sink.calls[0][2]
        assert delivered.metadata["should_dlq"] is True
        assert delivered.metadata["domain"] == "summarize"

    def test_armed_composer_skips_served_fallback(self, composer):
        """A served fallback answered the caller, so nothing is marked or parked."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        sink = MockSink()
        composer.add(FallbackPolicy(default_value="degraded")).add_sink(sink)
        composer.capture_failures("summarize")

        result = composer.execute(_throwing(RuntimeError("upstream 500")))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert sink.calls == []
        assert "should_dlq" not in result.metadata

    def test_fully_armed_composer_leaves_open_circuit_rejection_unmarked(
        self, composer
    ):
        """The facade arms both captures; the rejection keeps its own store
        path and gains no final-failure verdict."""
        sink = MockSink()
        composer.add(_CircuitOpenPolicy()).add_sink(sink)
        composer.capture_open_circuit_rejections()
        composer.capture_failures("summarize")

        result = composer.execute(lambda: "never runs")

        assert result.outcome == PolicyOutcome.REJECTED
        assert len(sink.calls) == 1
        assert "should_dlq" not in result.metadata

    def test_capture_failures_returns_self_for_chaining(self, composer):
        assert composer.capture_failures("summarize") is composer


class TestAsyncComposerFailureCaptureBehavior:
    """``AsyncPolicyComposer`` completes and delivers the same terminals
    through its normalized sink channel (sync/async parity)."""

    def test_armed_async_composer_delivers_failure_marked_for_its_domain(
        self, async_composer
    ):
        async def _fails() -> str:
            raise RuntimeError("upstream 500")

        sink = MockSink()
        async_composer.add_sink(sink).capture_failures("asummarize")

        result = asyncio.run(async_composer.execute(_fails))

        assert result.outcome == PolicyOutcome.FAILURE
        assert len(sink.calls) == 1
        assert result.metadata["should_dlq"] is True
        assert result.metadata["domain"] == "asummarize"

    def test_armed_async_composer_delivers_timeout_terminal(self, async_composer):
        async def _times_out() -> str:
            raise TimeoutPolicyError(5.0)

        sink = MockSink()
        async_composer.add(MockAsyncPolicy("wrapper")).add_sink(sink)
        async_composer.capture_failures("asummarize")

        result = asyncio.run(async_composer.execute(_times_out))

        assert result.outcome == PolicyOutcome.TIMEOUT
        assert len(sink.calls) == 1
        assert sink.calls[0][2].metadata["should_dlq"] is True
        assert sink.calls[0][2].metadata["domain"] == "asummarize"

    def test_armed_async_composer_delivers_bulkhead_full_rejection_once(
        self, async_composer
    ):
        async def _never_runs() -> str:
            return "never runs"

        sink = MockSink()
        async_composer.add(_AsyncBulkheadFullPolicy()).add_sink(sink)
        async_composer.capture_open_circuit_rejections()
        async_composer.capture_failures("ainventory")

        result = asyncio.run(async_composer.execute(_never_runs))

        assert result.outcome == PolicyOutcome.REJECTED
        assert len(sink.calls) == 1
        assert isinstance(sink.calls[0][0], BulkheadFullError)
        assert sink.calls[0][2].metadata["should_dlq"] is True
        assert sink.calls[0][2].metadata["domain"] == "ainventory"

    def test_failure_armed_async_composer_leaves_guard_veto_undelivered(
        self, async_composer
    ):
        async def _ok() -> str:
            return "ok"

        sink = MockSink()
        async_composer.add_guard(MockGuard(allowed=False, reason="duplicate"))
        async_composer.add(MockAsyncPolicy("wrapper")).add_sink(sink)
        async_composer.capture_failures("asummarize")

        result = asyncio.run(async_composer.execute(_ok))

        assert result.outcome == PolicyOutcome.REJECTED
        assert sink.calls == []
        assert "should_dlq" not in result.metadata

    def test_unarmed_async_composer_delivers_failure_without_a_verdict(
        self, async_composer
    ):
        async def _fails() -> str:
            raise RuntimeError("upstream 500")

        sink = MockSink()
        async_composer.add_sink(sink)

        asyncio.run(async_composer.execute(_fails))

        assert len(sink.calls) == 1
        assert "should_dlq" not in sink.calls[0][2].metadata

    def test_armed_async_composer_keeps_a_stage_verdict_that_declines(
        self, async_composer
    ):
        async def _fails() -> str:
            raise RuntimeError("upstream 500")

        sink = MockSink()
        async_composer.add(_AsyncVerdictStage(should_dlq=False)).add_sink(sink)
        async_composer.capture_failures("asummarize")

        asyncio.run(async_composer.execute(_fails))

        delivered = sink.calls[0][2]
        assert delivered.metadata["should_dlq"] is False
        assert delivered.metadata["domain"] == "retry_domain"

    def test_armed_async_composer_files_a_placeholder_domain_verdict_under_its_name(
        self, async_composer
    ):
        async def _fails() -> str:
            raise RuntimeError("upstream 500")

        sink = MockSink()
        async_composer.add(_AsyncVerdictStage(should_dlq=True, domain="default"))
        async_composer.add_sink(sink).capture_failures("asummarize")

        asyncio.run(async_composer.execute(_fails))

        delivered = sink.calls[0][2]
        assert delivered.metadata["should_dlq"] is True
        assert delivered.metadata["domain"] == "asummarize"

    def test_armed_async_composer_skips_served_fallback(self, async_composer):
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        async def _fails() -> str:
            raise RuntimeError("upstream 500")

        sink = MockSink()
        async_composer.add(AsyncFallbackPolicy(default_value="degraded"))
        async_composer.add_sink(sink).capture_failures("asummarize")

        result = asyncio.run(async_composer.execute(_fails))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert sink.calls == []
        assert "should_dlq" not in result.metadata

    def test_capture_failures_returns_self_for_chaining(self, async_composer):
        assert async_composer.capture_failures("asummarize") is (async_composer)


# =============================================================================
# Behavior — 799 D1: fallback_trigger names the error the fallback answered
# =============================================================================

# A timeout that fires long before the held function would finish, and one
# that never fires before the function raises on its own.
_TIMEOUT_FIRES_S = 0.05
_TIMEOUT_IDLE_S = 5.0
# Upper bound on how long a held function waits for its release.
_HOLD_S = 5.0

_TRIGGER_CASES = [
    ("plain_failure", PolicyOutcome.FAILURE),
    ("wall_clock_timeout", PolicyOutcome.TIMEOUT),
    ("open_breaker", PolicyOutcome.REJECTED),
    ("own_builtin_timeout", PolicyOutcome.FAILURE),
]
_TRIGGER_IDS = [case for case, _ in _TRIGGER_CASES]


class TestComposerFallbackTriggerBehavior:
    """799 D1: the ``SUCCESS_WITH_FALLBACK`` result records which class of
    error the fallback answered as ``metadata["fallback_trigger"]`` — the
    classifier's value for the absorbed exception — so the idempotency hook can
    hold a timed-out call's key and release any other. A builtin
    ``TimeoutError`` the function raised itself passes the timeout stage
    unmodified and is a plain failure, not a timeout."""

    @pytest.fixture
    def release(self):
        """Event a held function waits on; set at teardown so the abandoned
        timeout-executor thread ends."""
        event = threading.Event()
        yield event
        event.set()

    @staticmethod
    def _sync_chain(case: str, release: threading.Event):
        """(inner stage or None, protected function) for one answered error."""
        from baldur.resilience.policies.timeout import TimeoutPolicy

        return {
            "plain_failure": (None, _throwing(RuntimeError("charge declined"))),
            "wall_clock_timeout": (
                TimeoutPolicy(_TIMEOUT_FIRES_S),
                lambda: release.wait(timeout=_HOLD_S),
            ),
            "open_breaker": (_CircuitOpenPolicy(), lambda: "unreached"),
            "own_builtin_timeout": (
                TimeoutPolicy(_TIMEOUT_IDLE_S),
                _throwing(TimeoutError("upstream read timed out")),
            ),
        }[case]

    @staticmethod
    def _async_chain(case: str):
        """Async twin of :meth:`_sync_chain`."""
        from baldur.resilience.policies.timeout import AsyncTimeoutPolicy

        async def _fails() -> str:
            raise RuntimeError("charge declined")

        async def _held() -> str:
            await asyncio.Event().wait()  # only the timeout's cancel ends it
            return "unreached"

        async def _unreached() -> str:
            return "unreached"

        async def _own_timeout() -> str:
            raise TimeoutError("upstream read timed out")

        return {
            "plain_failure": (None, _fails),
            "wall_clock_timeout": (AsyncTimeoutPolicy(_TIMEOUT_FIRES_S), _held),
            "open_breaker": (_AsyncCircuitOpenPolicy(), _unreached),
            "own_builtin_timeout": (AsyncTimeoutPolicy(_TIMEOUT_IDLE_S), _own_timeout),
        }[case]

    @pytest.mark.parametrize(
        ("case", "expected_trigger"), _TRIGGER_CASES, ids=_TRIGGER_IDS
    )
    def test_fallback_trigger_records_answered_error_class(
        self, composer, release, case, expected_trigger
    ):
        from baldur.resilience.policies.fallback import FallbackPolicy

        # Given — the fallback outermost, the stage that produces the error
        # inside it.
        stage, func = self._sync_chain(case, release)
        composer.add(FallbackPolicy(default_value="degraded"))
        if stage is not None:
            composer.add(stage)

        # When
        result = composer.execute(func)

        # Then
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.metadata["fallback_trigger"] == expected_trigger.value

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("case", "expected_trigger"), _TRIGGER_CASES, ids=_TRIGGER_IDS
    )
    async def test_async_fallback_trigger_records_answered_error_class(
        self, async_composer, case, expected_trigger
    ):
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        # Given
        stage, func = self._async_chain(case)
        async_composer.add(AsyncFallbackPolicy(default_value="degraded"))
        if stage is not None:
            async_composer.add(stage)

        # When
        result = await async_composer.execute(func)

        # Then
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.metadata["fallback_trigger"] == expected_trigger.value

    def test_fallback_trigger_absent_when_function_returns(self, composer):
        from baldur.resilience.policies.fallback import FallbackPolicy

        composer.add(FallbackPolicy(default_value="degraded"))

        result = composer.execute(lambda: "charged")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert "fallback_trigger" not in result.metadata

    @pytest.mark.asyncio
    async def test_async_fallback_trigger_absent_when_function_returns(
        self, async_composer
    ):
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        async def _charges() -> str:
            return "charged"

        async_composer.add(AsyncFallbackPolicy(default_value="degraded"))

        result = await async_composer.execute(_charges)

        assert result.outcome == PolicyOutcome.SUCCESS
        assert "fallback_trigger" not in result.metadata

    def test_fallback_trigger_answer_logs_fallback_applied_once(self, composer):
        """The shared result builder logs the degraded answer exactly once."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        composer.add(FallbackPolicy(default_value="degraded"))

        with capture_logs() as cap_logs:
            composer.execute(_throwing(RuntimeError("charge declined")))

        applied = [e for e in cap_logs if e["event"] == "policy_chain.fallback_applied"]
        assert len(applied) == 1
        assert applied[0]["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_async_fallback_trigger_answer_logs_fallback_applied_once(
        self, async_composer
    ):
        from baldur.resilience.policies.fallback import AsyncFallbackPolicy

        async def _fails() -> str:
            raise RuntimeError("charge declined")

        async_composer.add(AsyncFallbackPolicy(default_value="degraded"))

        with capture_logs() as cap_logs:
            await async_composer.execute(_fails)

        applied = [e for e in cap_logs if e["event"] == "policy_chain.fallback_applied"]
        assert len(applied) == 1
        assert applied[0]["error_type"] == "RuntimeError"
