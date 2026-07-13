"""
PolicyComposer / AsyncPolicyComposer / compose / compose_async 단위 테스트 (#231).

테스트 대상:
- resilience/policies/composer.py
  (PolicyComposer, AsyncPolicyComposer, compose, compose_async, _FallbackApplied)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (_FallbackApplied 구조, 초기 상태)
- 동작 검증(Behavior): 소스 참조 (PolicyOutcome, PolicyResult 등)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

import asyncio
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
    _classify_exception_outcome,
    _FallbackApplied,
    compose,
    compose_async,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError

# =============================================================================
# Mock 구현체 — Protocol 준수
# =============================================================================


class MockPolicy:
    """ResiliencePolicy Protocol 준수 Mock — 함수를 그대로 실행."""

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
    """요청을 거부하는 Policy — REJECTED 반환."""

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
    """AsyncResiliencePolicy Protocol 준수 Mock."""

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
    """PolicyGuard Protocol 준수 Mock."""

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
    """check()에서 예외를 던지는 Guard — Fail-Open 테스트용."""

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
    """PolicyHook Protocol 준수 Mock — 호출 기록."""

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
    """on_success/on_failure/on_reject에서 예외를 던지는 Hook — Fail-Open 테스트."""

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
    """FailureSink Protocol 준수 Mock — 호출 기록."""

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
    """handle_failure에서 예외를 던지는 Sink — Fail-Open 테스트."""

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
    """빈 PolicyComposer 인스턴스."""
    return PolicyComposer()


@pytest.fixture
def async_composer():
    """빈 AsyncPolicyComposer 인스턴스."""
    return AsyncPolicyComposer()


# =============================================================================
# 계약 검증 — _FallbackApplied 내부 시그널
# =============================================================================


class TestFallbackAppliedContract:
    """_FallbackApplied 내부 시그널 예외 계약 검증."""

    def test_is_base_exception_subclass(self):
        """_FallbackApplied is a BaseException subclass (not Exception)."""
        assert issubclass(_FallbackApplied, BaseException)
        assert not issubclass(_FallbackApplied, Exception)

    def test_has_result_attribute(self):
        """_FallbackApplied 인스턴스는 result 속성을 가진다."""
        result = PolicyResult(value="test", outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        exc = _FallbackApplied(result)
        assert exc.result is result

    def test_message(self):
        """_FallbackApplied 기본 메시지는 'Fallback applied'이다."""
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
# 계약 검증 — PolicyComposer 초기 상태
# =============================================================================


class TestPolicyComposerInitContract:
    """PolicyComposer 초기 상태 계약 검증."""

    def test_policies_empty(self, composer):
        """초기 _policies 리스트는 비어있다."""
        assert composer._policies == []

    def test_guards_empty(self, composer):
        """초기 _guards 리스트는 비어있다."""
        assert composer._guards == []

    def test_hooks_empty(self, composer):
        """초기 _hooks 리스트는 비어있다."""
        assert composer._hooks == []

    def test_sinks_empty(self, composer):
        """초기 _sinks 리스트는 비어있다."""
        assert composer._sinks == []


class TestAsyncPolicyComposerInitContract:
    """AsyncPolicyComposer 초기 상태 계약 검증."""

    def test_policies_empty(self, async_composer):
        """초기 _policies 리스트는 비어있다."""
        assert async_composer._policies == []

    def test_guards_empty(self, async_composer):
        """초기 _guards 리스트는 비어있다."""
        assert async_composer._guards == []

    def test_hooks_empty(self, async_composer):
        """초기 _hooks 리스트는 비어있다."""
        assert async_composer._hooks == []

    def test_sinks_empty(self, async_composer):
        """초기 _sinks 리스트는 비어있다."""
        assert async_composer._sinks == []


# =============================================================================
# 동작 검증 — Builder API
# =============================================================================


class TestPolicyComposerBuilderBehavior:
    """PolicyComposer Builder API 동작 검증."""

    def test_add_returns_self(self, composer):
        """add()는 self를 반환하여 체이닝을 지원한다."""
        policy = MockPolicy()
        result = composer.add(policy)
        assert result is composer

    def test_add_appends_policy(self, composer):
        """add()는 _policies에 Policy를 추가한다."""
        policy = MockPolicy()
        composer.add(policy)
        assert composer._policies == [policy]

    def test_add_multiple_policies_preserves_order(self, composer):
        """add()는 추가 순서를 보존한다."""
        p1 = MockPolicy("p1")
        p2 = MockPolicy("p2")
        p3 = MockPolicy("p3")
        composer.add(p1).add(p2).add(p3)
        assert composer._policies == [p1, p2, p3]

    def test_add_async_policy_structural_match(self, composer):
        """@runtime_checkable Protocol은 구조적 매칭이므로 async policy도 추가된다.

        ResiliencePolicy와 AsyncResiliencePolicy 모두 name+execute 속성만 검사.
        isinstance 조건: AsyncResiliencePolicy AND NOT ResiliencePolicy 이지만,
        구조적으로 동일한 시그니처이므로 guard가 발동되지 않는다.
        타입 안전성은 Mypy 정적 분석에서 담당한다.
        """
        async_policy = MockAsyncPolicy()
        # 구조적 매칭으로 두 Protocol 모두 만족 → guard 비발동 → 추가됨
        composer.add(async_policy)
        assert async_policy in composer._policies

    def test_add_guard_returns_self(self, composer):
        """add_guard()는 self를 반환한다."""
        guard = MockGuard()
        result = composer.add_guard(guard)
        assert result is composer

    def test_add_guard_appends_guard(self, composer):
        """add_guard()는 _guards에 Guard를 추가한다."""
        guard = MockGuard()
        composer.add_guard(guard)
        assert composer._guards == [guard]

    def test_add_hook_returns_self(self, composer):
        """add_hook()는 self를 반환한다."""
        hook = MockHook()
        result = composer.add_hook(hook)
        assert result is composer

    def test_add_hook_appends_hook(self, composer):
        """add_hook()는 _hooks에 Hook을 추가한다."""
        hook = MockHook()
        composer.add_hook(hook)
        assert composer._hooks == [hook]

    def test_add_sink_returns_self(self, composer):
        """add_sink()는 self를 반환한다."""
        sink = MockSink()
        result = composer.add_sink(sink)
        assert result is composer

    def test_add_sink_appends_sink(self, composer):
        """add_sink()는 _sinks에 Sink를 추가한다."""
        sink = MockSink()
        composer.add_sink(sink)
        assert composer._sinks == [sink]


class TestAsyncPolicyComposerBuilderBehavior:
    """AsyncPolicyComposer Builder API 동작 검증."""

    def test_add_returns_self(self, async_composer):
        """add()는 self를 반환한다."""
        policy = MockAsyncPolicy()
        result = async_composer.add(policy)
        assert result is async_composer

    def test_add_appends_policy(self, async_composer):
        """add()는 _policies에 Policy를 추가한다."""
        policy = MockAsyncPolicy()
        async_composer.add(policy)
        assert async_composer._policies == [policy]

    def test_add_guard_returns_self(self, async_composer):
        """add_guard()는 self를 반환한다."""
        guard = MockGuard()
        result = async_composer.add_guard(guard)
        assert result is async_composer

    def test_add_hook_returns_self(self, async_composer):
        """add_hook()는 self를 반환한다."""
        hook = MockHook()
        result = async_composer.add_hook(hook)
        assert result is async_composer

    def test_add_sink_returns_self(self, async_composer):
        """add_sink()는 self를 반환한다."""
        sink = MockSink()
        result = async_composer.add_sink(sink)
        assert result is async_composer


# =============================================================================
# 동작 검증 — execute(): Policy 없음
# =============================================================================


class TestComposerExecuteNoPolicyBehavior:
    """Policy 없이 execute() 동작 검증."""

    def test_success_without_policies(self, composer):
        """Policy 없이 func 성공 시 SUCCESS outcome."""
        result = composer.execute(lambda: 42)
        assert result.success is True
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_failure_without_policies(self, composer):
        """Policy 없이 func 실패 시 FAILURE outcome."""
        err = ValueError("test error")

        def failing():
            raise err

        result = composer.execute(failing)
        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err


# =============================================================================
# 동작 검증 — execute(): Guard 검증
# =============================================================================


class TestComposerGuardBehavior:
    """Guard 검증 동작 테스트."""

    def test_guard_allowed(self, composer):
        """Guard가 허용하면 func이 실행된다."""
        guard = MockGuard(allowed=True)
        composer.add_guard(guard)
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"
        assert guard.check_count == 1

    def test_guard_rejected(self, composer):
        """Guard가 거부하면 REJECTED outcome이며 func은 실행되지 않는다."""
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
        """Guard에서 예외 발생 시 Fail-Open으로 통과한다."""
        composer.add_guard(MockFailingGuard())
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"

    def test_multiple_guards_short_circuit(self, composer):
        """첫 번째 Guard가 거부하면 두 번째 Guard는 호출되지 않는다."""
        guard1 = MockGuard(allowed=False, reason="blocked", guard_name="g1")
        guard2 = MockGuard(allowed=True, guard_name="g2")
        composer.add_guard(guard1).add_guard(guard2)

        result = composer.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED
        assert guard1.check_count == 1
        assert guard2.check_count == 0

    def test_guard_receives_context(self, composer):
        """Guard.check()에 context가 전달된다."""
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
# 동작 검증 — execute(): Guard reject metadata propagation (#567 D2)
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
# 동작 검증 — execute(): guard fail-open WARN, sync↔async parity (#567 D7)
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
# 동작 검증 — execute(): Policy 체인
# =============================================================================


class TestComposerPolicyChainBehavior:
    """Policy 체인 실행 동작 검증."""

    def test_single_policy_wraps_func(self, composer):
        """단일 Policy가 func을 래핑하여 실행한다."""
        policy = MockPolicy("p1")
        composer.add(policy)
        result = composer.execute(lambda: "value")

        assert result.success is True
        assert result.value == "value"
        assert policy.execute_count == 1

    def test_multiple_policies_nesting_order(self, composer):
        """Policy 추가 순서가 바깥→안쪽 실행 순서 (첫 번째가 가장 바깥)."""
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
        """Policy 내부에서 func 실패 시 error가 상위로 전파된다."""
        err = RuntimeError("func failed")

        def failing():
            raise err

        policy = MockPolicy()
        composer.add(policy)
        result = composer.execute(failing)

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err

    def test_rejecting_policy_returns_rejected(self, composer):
        """Policy가 REJECTED를 반환하면 PolicyRejectedException → REJECTED outcome."""
        composer.add(MockRejectingPolicy())
        result = composer.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, PolicyRejectedException)

    def test_executed_policies_tracked(self, composer):
        """실행된 Policy 이름이 executed_policies에 기록된다."""
        composer.add(MockPolicy("retry"))
        composer.add(MockPolicy("circuit_breaker"))
        result = composer.execute(lambda: "ok")

        assert "retry" in result.executed_policies
        assert "circuit_breaker" in result.executed_policies

    def test_context_passed_to_policy(self, composer):
        """Policy.execute()에 context가 전달된다."""
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
# 동작 검증 — execute(): FallbackPolicy 체인 내 특별 처리
# =============================================================================


class TestComposerFallbackChainBehavior:
    """Composer 내 FallbackPolicy 특별 처리 동작 검증."""

    def test_fallback_applied_signal_produces_success_with_fallback(self, composer):
        """FallbackPolicy가 체인 내에서 _FallbackApplied로 SUCCESS_WITH_FALLBACK을 전파한다."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fallback = FallbackPolicy(default_value="fallback_value")
        composer.add(fallback)

        err = RuntimeError("original error")
        result = composer.execute(lambda: (_ for _ in ()).throw(err))

        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback_value"

    def test_fallback_not_triggered_on_success(self, composer):
        """func 성공 시 FallbackPolicy는 트리거되지 않는다."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        fallback = FallbackPolicy(default_value="fallback_value")
        composer.add(fallback)
        result = composer.execute(lambda: "original_value")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "original_value"

    def test_fallback_with_predicate_not_matching(self, composer):
        """predicate가 False를 반환하면 Fallback이 적용되지 않는다."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        # predicate: 항상 False → Fallback 비활성화
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
        """일반 Policy + FallbackPolicy 조합: Policy 실패 시 Fallback이 적용된다."""
        from baldur.resilience.policies.fallback import FallbackPolicy

        composer.add(MockPolicy("wrapper"))
        composer.add(FallbackPolicy(default_value="fallback_result"))

        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.SUCCESS_WITH_FALLBACK
        assert result.value == "fallback_result"


# =============================================================================
# 동작 검증 — execute(): Hook 호출
# =============================================================================


class TestComposerHookBehavior:
    """Hook 호출 동작 검증."""

    def test_on_success_called(self, composer):
        """성공 시 Hook.on_success가 호출된다."""
        hook = MockHook()
        composer.add_hook(hook)
        composer.execute(lambda: "ok")

        assert len(hook.success_calls) == 1
        assert hook.success_calls[0][0] == "composer"

    def test_on_failure_called(self, composer):
        """실패 시 Hook.on_failure가 호출된다."""
        hook = MockHook()
        composer.add_hook(hook)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        assert len(hook.failure_calls) == 1
        assert hook.failure_calls[0][0] == "composer"

    def test_on_reject_called(self, composer):
        """Guard 거부 시 Hook.on_reject가 호출된다."""
        hook = MockHook()
        guard = MockGuard(allowed=False, reason="blocked", guard_name="test_guard")
        composer.add_guard(guard).add_hook(hook)
        composer.execute(lambda: "ok")

        assert len(hook.reject_calls) == 1
        assert hook.reject_calls[0] == ("test_guard", "blocked")

    def test_hook_fail_open_on_success(self, composer):
        """Hook.on_success가 예외를 던져도 결과에 영향 없다 (Fail-Open)."""
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: "ok")
        assert result.success is True
        assert result.value == "ok"

    def test_hook_fail_open_on_failure(self, composer):
        """Hook.on_failure가 예외를 던져도 결과에 영향 없다 (Fail-Open)."""
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.FAILURE

    def test_hook_fail_open_on_reject(self, composer):
        """Hook.on_reject가 예외를 던져도 결과에 영향 없다 (Fail-Open)."""
        composer.add_guard(MockGuard(allowed=False, reason="x"))
        composer.add_hook(MockFailingHook())
        result = composer.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED

    def test_multiple_hooks_all_called(self, composer):
        """여러 Hook이 모두 호출된다."""
        hook1 = MockHook()
        hook2 = MockHook()
        composer.add_hook(hook1).add_hook(hook2)
        composer.execute(lambda: "ok")

        assert len(hook1.success_calls) == 1
        assert len(hook2.success_calls) == 1

    def test_total_duration_ms_set(self, composer):
        """execute() 후 result.total_duration_ms가 0 이상 값으로 설정된다."""
        import time

        hook = MockHook()
        composer.add_hook(hook)

        def slow_func():
            time.sleep(0.01)
            return "ok"

        result = composer.execute(slow_func)
        assert result.total_duration_ms > 0


# =============================================================================
# 동작 검증 — execute(): Sink 처리
# =============================================================================


class TestComposerSinkBehavior:
    """Sink 처리 동작 검증."""

    def test_sink_called_on_failure(self, composer):
        """FAILURE 시 Sink.handle_failure가 호출된다."""
        sink = MockSink(sink_id="dlq-001")
        composer.add_sink(sink)
        err = RuntimeError("fail")
        result = composer.execute(lambda: (_ for _ in ()).throw(err))

        assert len(sink.calls) == 1
        assert sink.calls[0][0] is err
        assert result.metadata["sink_id"] == "dlq-001"

    def test_sink_not_called_on_success(self, composer):
        """SUCCESS 시 Sink는 호출되지 않는다."""
        sink = MockSink()
        composer.add_sink(sink)
        composer.execute(lambda: "ok")

        assert len(sink.calls) == 0

    def test_sink_not_called_on_rejected(self, composer):
        """REJECTED 시 Sink는 호출되지 않는다."""
        sink = MockSink()
        composer.add_guard(MockGuard(allowed=False, reason="blocked"))
        composer.add_sink(sink)
        composer.execute(lambda: "ok")

        assert len(sink.calls) == 0

    def test_sink_receives_context(self, composer):
        """Sink에 context가 전달된다."""
        sink = MockSink()
        ctx = PolicyContext(order_id="ORD-456")
        composer.add_sink(sink)
        composer.execute(
            lambda: (_ for _ in ()).throw(RuntimeError("fail")), context=ctx
        )

        assert len(sink.calls) == 1
        assert sink.calls[0][1] is ctx

    def test_sink_fail_open(self, composer):
        """Sink 예외 시에도 결과가 반환된다 (Fail-Open)."""
        composer.add_sink(MockFailingSink())
        result = composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert result.outcome == PolicyOutcome.FAILURE

    def test_sink_id_none_not_stored(self, composer):
        """Sink가 None을 반환하면 metadata에 sink_id가 추가되지 않는다."""
        sink = MockSink(sink_id=None)
        composer.add_sink(sink)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))
        assert "sink_id" not in sink.calls[0][2].metadata

    def test_multiple_sinks_all_called(self, composer):
        """여러 Sink가 모두 호출된다."""
        sink1 = MockSink(sink_id="s1")
        sink2 = MockSink(sink_id="s2")
        composer.add_sink(sink1).add_sink(sink2)
        composer.execute(lambda: (_ for _ in ()).throw(RuntimeError("fail")))

        assert len(sink1.calls) == 1
        assert len(sink2.calls) == 1


# =============================================================================
# 동작 검증 — compose() 편의 함수
# =============================================================================


class TestComposeFunctionBehavior:
    """compose() 편의 함수 동작 검증."""

    def test_compose_returns_policy_composer(self):
        """compose()는 PolicyComposer 인스턴스를 반환한다."""
        result = compose(MockPolicy("p1"))
        assert isinstance(result, PolicyComposer)

    def test_compose_adds_policies_in_order(self):
        """compose()는 인자 순서대로 Policy를 추가한다."""
        p1 = MockPolicy("p1")
        p2 = MockPolicy("p2")
        result = compose(p1, p2)
        assert result._policies == [p1, p2]

    def test_compose_no_policies(self):
        """인자 없이 compose() 호출 시 빈 PolicyComposer를 반환한다."""
        result = compose()
        assert isinstance(result, PolicyComposer)
        assert result._policies == []

    def test_compose_chaining_with_guard(self):
        """compose().add_guard() 체이닝이 동작한다."""
        guard = MockGuard(allowed=False, reason="blocked")
        result = compose(MockPolicy()).add_guard(guard).execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED


class TestComposeAsyncFunctionBehavior:
    """compose_async() 편의 함수 동작 검증."""

    def test_compose_async_returns_async_composer(self):
        """compose_async()는 AsyncPolicyComposer 인스턴스를 반환한다."""
        result = compose_async(MockAsyncPolicy("p1"))
        assert isinstance(result, AsyncPolicyComposer)

    def test_compose_async_adds_policies_in_order(self):
        """compose_async()는 인자 순서대로 Policy를 추가한다."""
        p1 = MockAsyncPolicy("p1")
        p2 = MockAsyncPolicy("p2")
        result = compose_async(p1, p2)
        assert result._policies == [p1, p2]

    def test_compose_async_no_policies(self):
        """인자 없이 compose_async() 호출 시 빈 AsyncPolicyComposer를 반환한다."""
        result = compose_async()
        assert isinstance(result, AsyncPolicyComposer)
        assert result._policies == []


# =============================================================================
# 동작 검증 — AsyncPolicyComposer.execute()
# =============================================================================


class TestAsyncComposerExecuteBehavior:
    """AsyncPolicyComposer.execute() 동작 검증."""

    @pytest.mark.asyncio
    async def test_success_without_policies(self, async_composer):
        """Policy 없이 async func 성공 시 SUCCESS outcome."""

        async def func():
            return 42

        result = await async_composer.execute(func)
        assert result.success is True
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_failure_without_policies(self, async_composer):
        """Policy 없이 async func 실패 시 FAILURE outcome."""
        err = ValueError("async error")

        async def failing():
            raise err

        result = await async_composer.execute(failing)
        assert result.success is False
        assert result.outcome == PolicyOutcome.FAILURE
        assert result.error is err

    @pytest.mark.asyncio
    async def test_guard_rejection(self, async_composer):
        """Guard 거부 시 REJECTED outcome."""
        guard = MockGuard(allowed=False, reason="denied")
        async_composer.add_guard(guard)

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert result.outcome == PolicyOutcome.REJECTED
        assert result.metadata["rejected_by"] == guard.name

    @pytest.mark.asyncio
    async def test_guard_fail_open(self, async_composer):
        """Guard 예외 시 Fail-Open."""
        async_composer.add_guard(MockFailingGuard())

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_single_async_policy(self, async_composer):
        """단일 AsyncPolicy가 func을 래핑하여 실행한다."""
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
        """성공 시 Hook.on_success가 호출된다."""
        hook = MockHook()
        async_composer.add_hook(hook)

        async def func():
            return "ok"

        await async_composer.execute(func)
        assert len(hook.success_calls) == 1

    @pytest.mark.asyncio
    async def test_hook_on_failure(self, async_composer):
        """실패 시 Hook.on_failure가 호출된다."""
        hook = MockHook()
        async_composer.add_hook(hook)

        async def func():
            raise RuntimeError("fail")

        await async_composer.execute(func)
        assert len(hook.failure_calls) == 1

    @pytest.mark.asyncio
    async def test_sink_called_on_failure(self, async_composer):
        """FAILURE 시 Sink가 호출된다."""
        sink = MockSink(sink_id="async-sink-001")
        async_composer.add_sink(sink)

        async def func():
            raise RuntimeError("fail")

        result = await async_composer.execute(func)
        assert len(sink.calls) == 1
        assert result.metadata["sink_id"] == "async-sink-001"

    @pytest.mark.asyncio
    async def test_sink_not_called_on_success(self, async_composer):
        """SUCCESS 시 Sink는 호출되지 않는다."""
        sink = MockSink()
        async_composer.add_sink(sink)

        async def func():
            return "ok"

        await async_composer.execute(func)
        assert len(sink.calls) == 0

    @pytest.mark.asyncio
    async def test_total_duration_ms_set(self, async_composer):
        """execute() 후 total_duration_ms가 0 이상 값으로 설정된다."""

        async def func():
            await asyncio.sleep(0.02)
            return "ok"

        result = await async_composer.execute(func)
        assert result.total_duration_ms > 0

    @pytest.mark.asyncio
    async def test_context_propagated(self, async_composer):
        """context가 Guard/Sink에 전달된다."""
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
        """실행된 Policy 이름이 executed_policies에 기록된다."""
        async_composer.add(MockAsyncPolicy("async_retry"))
        async_composer.add(MockAsyncPolicy("async_cb"))

        async def func():
            return "ok"

        result = await async_composer.execute(func)
        assert "async_retry" in result.executed_policies
        assert "async_cb" in result.executed_policies


# =============================================================================
# 동작 검증 — AsyncPolicyComposer: AsyncFallbackPolicy 체인 내 특별 처리
# =============================================================================


class TestAsyncComposerFallbackChainBehavior:
    """AsyncPolicyComposer 내 AsyncFallbackPolicy 특별 처리 동작 검증."""

    @pytest.mark.asyncio
    async def test_async_fallback_applied_on_failure(self, async_composer):
        """AsyncFallbackPolicy가 체인 내에서 _FallbackApplied로 SUCCESS_WITH_FALLBACK을 전파한다."""
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
        """func 성공 시 AsyncFallbackPolicy는 트리거되지 않는다."""
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
