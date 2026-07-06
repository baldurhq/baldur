"""
resilience_policy.py 단위 테스트

225_POLICY_COMPOSITION_INTERFACE_DESIGN.md에 정의된 인터페이스 계약을 검증한다.

테스트 범위:
- PolicyOutcome: Enum 멤버 및 문자열 비교 (계약 검증)
- PolicyResult: 필드 기본값, success/rejected 프로퍼티, Generic 타입 (동작 검증)
- PolicyContext: frozen 불변성, with_updates Copy-on-Write (동작 검증)
- GuardResult: 필드 구조 (동작 검증)
- ResiliencePolicy / AsyncResiliencePolicy: runtime_checkable 구조적 하위타입 (계약 검증)
- PolicyGuard: runtime_checkable 구조적 하위타입 (계약 검증)
- PolicyHook: runtime_checkable 구조적 하위타입 (계약 검증)
- FailureSink: runtime_checkable 구조적 하위타입 (계약 검증)

설계 원칙:
- Protocol 기반이므로 ABC TypeError가 아닌 isinstance() 구조적 검사를 테스트
- 하드코딩은 계약 검증(Enum 값, 기본값)에만 사용
- 동작 검증은 소스 상수/타입 참조
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

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
    PolicyResult,
    ResiliencePolicy,
)

# =============================================================================
# PolicyOutcome — 계약 검증
# =============================================================================


class TestPolicyOutcomeContract:
    """PolicyOutcome Enum 멤버 및 값 계약 검증.

    225 문서 §2.1에 정의된 5개 Outcome 값이 정확히 구현되었는지 확인한다.
    """

    def test_success_value(self):
        """SUCCESS 값은 'success'이다."""
        assert PolicyOutcome.SUCCESS == "success"

    def test_success_with_fallback_value(self):
        """SUCCESS_WITH_FALLBACK 값은 'fallback'이다."""
        assert PolicyOutcome.SUCCESS_WITH_FALLBACK == "fallback"

    def test_rejected_value(self):
        """REJECTED 값은 'rejected'이다."""
        assert PolicyOutcome.REJECTED == "rejected"

    def test_failure_value(self):
        """FAILURE 값은 'failure'이다."""
        assert PolicyOutcome.FAILURE == "failure"

    def test_timeout_value(self):
        """TIMEOUT 값은 'timeout'이다."""
        assert PolicyOutcome.TIMEOUT == "timeout"

    def test_member_count(self):
        """225 문서에 정의된 5개 멤버만 존재한다."""
        assert len(PolicyOutcome) == 5

    def test_is_str_enum(self):
        """PolicyOutcome은 str Enum이다 (문자열 비교 가능)."""
        assert isinstance(PolicyOutcome.SUCCESS, str)


# =============================================================================
# PolicyResult — 계약 검증 + 동작 검증
# =============================================================================


class TestPolicyResultContract:
    """PolicyResult 필드 기본값 계약 검증.

    225 문서 §2.1에 정의된 기본값이 코드에 반영되었는지 확인한다.
    """

    def test_default_value_is_none(self):
        """기본 value는 None이다."""
        result = PolicyResult()
        assert result.value is None

    def test_default_outcome_is_success(self):
        """기본 outcome은 PolicyOutcome.SUCCESS이다."""
        result = PolicyResult()
        assert result.outcome is PolicyOutcome.SUCCESS

    def test_default_error_is_none(self):
        """기본 error는 None이다."""
        result = PolicyResult()
        assert result.error is None

    def test_default_executed_policies_is_empty_list(self):
        """기본 executed_policies는 빈 리스트이다."""
        result = PolicyResult()
        assert result.executed_policies == []

    def test_default_total_attempts_is_one(self):
        """기본 total_attempts는 1이다."""
        result = PolicyResult()
        assert result.total_attempts == 1

    def test_default_total_duration_ms_is_zero(self):
        """기본 total_duration_ms는 0.0이다."""
        result = PolicyResult()
        assert result.total_duration_ms == 0.0

    def test_default_metadata_is_empty_dict(self):
        """기본 metadata는 빈 딕셔너리이다."""
        result = PolicyResult()
        assert result.metadata == {}


class TestPolicyResultBehavior:
    """PolicyResult 프로퍼티 및 동작 검증."""

    # ── success 프로퍼티 ──

    def test_success_true_on_success_outcome(self):
        """outcome=SUCCESS 일 때 success 프로퍼티는 True이다."""
        result = PolicyResult(outcome=PolicyOutcome.SUCCESS)
        assert result.success is True

    def test_success_true_on_fallback_outcome(self):
        """outcome=SUCCESS_WITH_FALLBACK 일 때 success 프로퍼티는 True이다."""
        result = PolicyResult(outcome=PolicyOutcome.SUCCESS_WITH_FALLBACK)
        assert result.success is True

    def test_success_false_on_failure_outcome(self):
        """outcome=FAILURE 일 때 success 프로퍼티는 False이다."""
        result = PolicyResult(outcome=PolicyOutcome.FAILURE)
        assert result.success is False

    def test_success_false_on_rejected_outcome(self):
        """outcome=REJECTED 일 때 success 프로퍼티는 False이다."""
        result = PolicyResult(outcome=PolicyOutcome.REJECTED)
        assert result.success is False

    def test_success_false_on_timeout_outcome(self):
        """outcome=TIMEOUT 일 때 success 프로퍼티는 False이다."""
        result = PolicyResult(outcome=PolicyOutcome.TIMEOUT)
        assert result.success is False

    # ── rejected 프로퍼티 ──

    def test_rejected_true_on_rejected_outcome(self):
        """outcome=REJECTED 일 때 rejected 프로퍼티는 True이다."""
        result = PolicyResult(outcome=PolicyOutcome.REJECTED)
        assert result.rejected is True

    def test_rejected_false_on_non_rejected_outcomes(self):
        """REJECTED 외 모든 outcome에서 rejected는 False이다."""
        non_rejected = [
            PolicyOutcome.SUCCESS,
            PolicyOutcome.SUCCESS_WITH_FALLBACK,
            PolicyOutcome.FAILURE,
            PolicyOutcome.TIMEOUT,
        ]
        for outcome in non_rejected:
            result = PolicyResult(outcome=outcome)
            assert result.rejected is False, (
                f"outcome={outcome}에서 rejected가 False여야 함"
            )

    # ── 값 저장 ──

    def test_stores_value(self):
        """value에 임의 값이 저장된다."""
        result = PolicyResult(value=42)
        assert result.value == 42

    def test_stores_error(self):
        """error에 예외 객체가 저장된다."""
        err = ValueError("test error")
        result = PolicyResult(outcome=PolicyOutcome.FAILURE, error=err)
        assert result.error is err

    def test_stores_executed_policies(self):
        """executed_policies에 Policy 이름 목록이 저장된다."""
        result = PolicyResult(executed_policies=["retry", "circuit_breaker"])
        assert result.executed_policies == ["retry", "circuit_breaker"]

    def test_stores_metadata(self):
        """metadata에 딕셔너리가 저장된다."""
        meta = {"dlq_id": 123, "fallback_mode": "cache"}
        result = PolicyResult(metadata=meta)
        assert result.metadata == meta

    # ── mutable default 격리 ──

    def test_executed_policies_default_isolation(self):
        """서로 다른 인스턴스의 executed_policies가 공유되지 않는다."""
        r1 = PolicyResult()
        r2 = PolicyResult()
        r1.executed_policies.append("retry")
        assert r2.executed_policies == []

    def test_metadata_default_isolation(self):
        """서로 다른 인스턴스의 metadata가 공유되지 않는다."""
        r1 = PolicyResult()
        r2 = PolicyResult()
        r1.metadata["key"] = "value"
        assert r2.metadata == {}

    # ── Generic 타입 ──

    def test_generic_string_value(self):
        """PolicyResult[str] 타입 사용 가능."""
        result: PolicyResult[str] = PolicyResult(value="hello")
        assert result.value == "hello"

    def test_generic_dict_value(self):
        """PolicyResult[dict] 타입 사용 가능."""
        data = {"order_id": "ORD-001"}
        result: PolicyResult[dict] = PolicyResult(value=data)
        assert result.value == data


# =============================================================================
# PolicyContext — 계약 검증 + 동작 검증
# =============================================================================


class TestPolicyContextContract:
    """PolicyContext 필드 기본값 계약 검증.

    225 문서 §2.2에 정의된 기본값이 코드에 반영되었는지 확인한다.
    """

    def test_default_order_id_is_none(self):
        ctx = PolicyContext()
        assert ctx.order_id is None

    def test_default_payment_id_is_none(self):
        ctx = PolicyContext()
        assert ctx.payment_id is None

    def test_default_user_id_is_none(self):
        ctx = PolicyContext()
        assert ctx.user_id is None

    def test_default_tier_id_is_none(self):
        ctx = PolicyContext()
        assert ctx.tier_id is None

    def test_default_region_is_none(self):
        ctx = PolicyContext()
        assert ctx.region is None

    def test_default_domain_is_empty_string(self):
        ctx = PolicyContext()
        assert ctx.domain == ""

    def test_default_trace_id_is_none(self):
        ctx = PolicyContext()
        assert ctx.trace_id is None

    def test_default_extra_is_empty_dict(self):
        ctx = PolicyContext()
        assert ctx.extra == {}

    def test_is_frozen_dataclass(self):
        """PolicyContext는 frozen=True 데이터클래스이다."""
        assert dataclasses.is_dataclass(PolicyContext)
        # frozen dataclass 여부는 필드 대입 시 FrozenInstanceError로 확인
        ctx = PolicyContext()
        with pytest.raises(FrozenInstanceError):
            ctx.order_id = "test"  # type: ignore[misc]


class TestPolicyContextBehavior:
    """PolicyContext 동작 검증 (Immutability, Copy-on-Write)."""

    def test_frozen_prevents_field_mutation(self):
        """frozen=True로 필드 직접 대입이 차단된다."""
        ctx = PolicyContext(order_id="ORD-001")
        with pytest.raises(FrozenInstanceError):
            ctx.tier_id = "critical"  # type: ignore[misc]

    def test_with_updates_returns_new_instance(self):
        """with_updates()는 새로운 인스턴스를 반환한다."""
        original = PolicyContext(order_id="ORD-001", domain="payment")
        updated = original.with_updates(tier_id="critical")
        assert updated is not original

    def test_with_updates_preserves_unchanged_fields(self):
        """with_updates()는 변경하지 않은 필드를 보존한다."""
        original = PolicyContext(order_id="ORD-001", domain="payment")
        updated = original.with_updates(tier_id="critical")
        assert updated.order_id == original.order_id
        assert updated.domain == original.domain

    def test_with_updates_applies_changes(self):
        """with_updates()는 지정 필드를 변경한다."""
        original = PolicyContext(tier_id="standard")
        updated = original.with_updates(tier_id="critical", region="us-east-1")
        assert updated.tier_id == "critical"
        assert updated.region == "us-east-1"

    def test_with_updates_does_not_mutate_original(self):
        """with_updates() 후 원본 인스턴스는 변경되지 않는다."""
        original = PolicyContext(tier_id="standard")
        original.with_updates(tier_id="critical")
        assert original.tier_id == "standard"

    def test_stores_all_business_identifiers(self):
        """비즈니스 식별자 필드가 정상 저장된다."""
        ctx = PolicyContext(
            order_id="ORD-001",
            payment_id="PAY-002",
            user_id="USR-003",
        )
        assert ctx.order_id == "ORD-001"
        assert ctx.payment_id == "PAY-002"
        assert ctx.user_id == "USR-003"

    def test_stores_policy_criteria(self):
        """Policy 판정 기준 필드가 정상 저장된다."""
        ctx = PolicyContext(tier_id="critical", region="ap-northeast-2")
        assert ctx.tier_id == "critical"
        assert ctx.region == "ap-northeast-2"

    def test_stores_domain_and_trace(self):
        """도메인 및 추적 필드가 정상 저장된다."""
        ctx = PolicyContext(domain="payment", trace_id="abc-123")
        assert ctx.domain == "payment"
        assert ctx.trace_id == "abc-123"

    def test_stores_extra_dict(self):
        """extra 확장 필드에 임의 데이터가 저장된다."""
        ctx = PolicyContext(extra={"snapshot_data": {"key": "val"}})
        assert ctx.extra == {"snapshot_data": {"key": "val"}}

    def test_extra_default_isolation(self):
        """서로 다른 인스턴스의 extra가 공유되지 않는다."""
        ctx1 = PolicyContext()
        ctx2 = PolicyContext()
        # frozen이므로 extra 내부 값은 Python 한계로 변경 가능하나
        # 참조 자체는 별개 dict 인스턴스
        assert ctx1.extra is not ctx2.extra


# =============================================================================
# GuardResult — 계약 검증 + 동작 검증
# =============================================================================


class TestGuardResultContract:
    """GuardResult 필드 기본값 계약 검증."""

    def test_default_reason_is_none(self):
        result = GuardResult(allowed=True)
        assert result.reason is None

    def test_default_metadata_is_empty_dict(self):
        result = GuardResult(allowed=True)
        assert result.metadata == {}


class TestGuardResultBehavior:
    """GuardResult 동작 검증."""

    def test_allowed_true(self):
        """allowed=True로 통과 결과를 생성한다."""
        result = GuardResult(allowed=True)
        assert result.allowed is True

    def test_allowed_false_with_reason(self):
        """allowed=False로 거부 결과와 사유를 생성한다."""
        result = GuardResult(allowed=False, reason="error budget exhausted")
        assert result.allowed is False
        assert result.reason == "error budget exhausted"

    def test_stores_metadata(self):
        """metadata에 추가 정보가 저장된다."""
        meta = {"remaining_budget": 0.05}
        result = GuardResult(allowed=False, reason="low budget", metadata=meta)
        assert result.metadata == meta

    def test_metadata_default_isolation(self):
        """서로 다른 인스턴스의 metadata가 공유되지 않는다."""
        r1 = GuardResult(allowed=True)
        r2 = GuardResult(allowed=True)
        r1.metadata["key"] = "value"
        assert r2.metadata == {}


# =============================================================================
# ResiliencePolicy Protocol — 계약 검증
# =============================================================================


class TestResiliencePolicyContract:
    """ResiliencePolicy Protocol 구조적 하위타입 계약 검증.

    225 문서 §2.3에 정의된 Protocol을 올바르게 구현하는 클래스가
    isinstance() 검사를 통과하는지 확인한다.
    """

    def test_runtime_checkable(self):
        """ResiliencePolicy는 runtime_checkable Protocol이다."""
        assert hasattr(ResiliencePolicy, "__protocol_attrs__") or hasattr(
            ResiliencePolicy, "__abstractmethods__"
        )

    def test_conforming_class_passes_isinstance(self):
        """Protocol 서명을 구현한 클래스는 isinstance 검사를 통과한다."""

        class StubPolicy:
            @property
            def name(self) -> str:
                return "stub"

            def execute(
                self,
                func: Callable[..., Any],
                *args: Any,
                context: PolicyContext | None = None,
                **kwargs: Any,
            ) -> PolicyResult:
                return PolicyResult(value=func(*args, **kwargs))

        stub = StubPolicy()
        assert isinstance(stub, ResiliencePolicy)

    def test_non_conforming_class_fails_isinstance(self):
        """Protocol 서명을 구현하지 않은 클래스는 isinstance 검사를 실패한다."""

        class NotAPolicy:
            pass

        assert not isinstance(NotAPolicy(), ResiliencePolicy)

    def test_stub_execute_returns_policy_result(self):
        """Protocol 구현체의 execute()가 PolicyResult를 반환한다."""

        class StubPolicy:
            @property
            def name(self) -> str:
                return "stub"

            def execute(
                self,
                func: Callable[..., Any],
                *args: Any,
                context: PolicyContext | None = None,
                **kwargs: Any,
            ) -> PolicyResult:
                return PolicyResult(value=func(*args, **kwargs))

        stub = StubPolicy()
        result = stub.execute(lambda x: x * 2, 5)
        assert isinstance(result, PolicyResult)
        assert result.value == 10

    def test_stub_execute_with_context(self):
        """Protocol 구현체의 execute()가 context를 전달받을 수 있다."""

        class StubPolicy:
            @property
            def name(self) -> str:
                return "stub"

            def execute(
                self,
                func: Callable[..., Any],
                *args: Any,
                context: PolicyContext | None = None,
                **kwargs: Any,
            ) -> PolicyResult:
                meta = {}
                if context:
                    meta["domain"] = context.domain
                return PolicyResult(value=func(*args, **kwargs), metadata=meta)

        ctx = PolicyContext(domain="payment")
        stub = StubPolicy()
        result = stub.execute(lambda: "ok", context=ctx)
        assert result.metadata["domain"] == "payment"


# =============================================================================
# AsyncResiliencePolicy Protocol — 계약 검증
# =============================================================================


class TestAsyncResiliencePolicyContract:
    """AsyncResiliencePolicy Protocol 구조적 하위타입 계약 검증.

    225 문서 §2.3에 정의된 비동기 Protocol을 검증한다.
    """

    def test_runtime_checkable(self):
        """AsyncResiliencePolicy는 runtime_checkable Protocol이다."""
        assert hasattr(AsyncResiliencePolicy, "__protocol_attrs__") or hasattr(
            AsyncResiliencePolicy, "__abstractmethods__"
        )

    def test_conforming_async_class_passes_isinstance(self):
        """비동기 Protocol 서명을 구현한 클래스는 isinstance 검사를 통과한다."""

        class StubAsyncPolicy:
            @property
            def name(self) -> str:
                return "async_stub"

            async def execute(
                self,
                func: Callable[..., Any],
                *args: Any,
                context: PolicyContext | None = None,
                **kwargs: Any,
            ) -> PolicyResult:
                return PolicyResult(value=await func(*args, **kwargs))

        stub = StubAsyncPolicy()
        assert isinstance(stub, AsyncResiliencePolicy)

    def test_non_conforming_class_fails_isinstance(self):
        """Protocol 서명을 구현하지 않은 클래스는 isinstance 검사를 실패한다."""

        class NotAnAsyncPolicy:
            pass

        assert not isinstance(NotAnAsyncPolicy(), AsyncResiliencePolicy)

    @pytest.mark.asyncio
    async def test_stub_async_execute_returns_policy_result(self):
        """비동기 Protocol 구현체의 execute()가 PolicyResult를 반환한다."""

        class StubAsyncPolicy:
            @property
            def name(self) -> str:
                return "async_stub"

            async def execute(
                self,
                func: Callable[..., Any],
                *args: Any,
                context: PolicyContext | None = None,
                **kwargs: Any,
            ) -> PolicyResult:
                result = await func(*args, **kwargs)
                return PolicyResult(value=result)

        async def async_fn(x: int) -> int:
            return x * 3

        stub = StubAsyncPolicy()
        result = await stub.execute(async_fn, 7)
        assert isinstance(result, PolicyResult)
        assert result.value == 21


# =============================================================================
# PolicyGuard Protocol — 계약 검증
# =============================================================================


class TestPolicyGuardContract:
    """PolicyGuard Protocol 구조적 하위타입 계약 검증.

    225 문서 §2.4에 정의된 Guard Protocol을 검증한다.
    """

    def test_runtime_checkable(self):
        """PolicyGuard는 runtime_checkable Protocol이다."""
        assert hasattr(PolicyGuard, "__protocol_attrs__") or hasattr(
            PolicyGuard, "__abstractmethods__"
        )

    def test_conforming_guard_passes_isinstance(self):
        """Protocol 서명을 구현한 클래스는 isinstance 검사를 통과한다."""

        class StubGuard:
            @property
            def name(self) -> str:
                return "stub_guard"

            def check(self, context: PolicyContext | None = None) -> GuardResult:
                return GuardResult(allowed=True)

        assert isinstance(StubGuard(), PolicyGuard)

    def test_non_conforming_class_fails_isinstance(self):
        """Protocol 서명을 구현하지 않은 클래스는 isinstance 검사를 실패한다."""

        class NotAGuard:
            pass

        assert not isinstance(NotAGuard(), PolicyGuard)

    def test_guard_check_with_none_context(self):
        """Guard는 context=None으로 호출 가능하다 (전역 상태 체크)."""

        class StubKillSwitchGuard:
            @property
            def name(self) -> str:
                return "kill_switch"

            def check(self, context: PolicyContext | None = None) -> GuardResult:
                # context 무시, 전역 상태만 체크
                return GuardResult(allowed=True)

        guard = StubKillSwitchGuard()
        result = guard.check(context=None)
        assert result.allowed is True

    def test_guard_check_with_context(self):
        """Guard는 context를 받아 tier_id 기반 판정이 가능하다."""

        class StubErrorBudgetGuard:
            @property
            def name(self) -> str:
                return "error_budget"

            def check(self, context: PolicyContext | None = None) -> GuardResult:
                tier_id = context.tier_id if context else None
                if tier_id == "critical":
                    return GuardResult(allowed=True)
                return GuardResult(allowed=False, reason="budget exhausted")

        guard = StubErrorBudgetGuard()

        # context 없으면 글로벌 판정 (거부)
        result_none = guard.check(context=None)
        assert result_none.allowed is False

        # context.tier_id="critical"이면 허용
        ctx = PolicyContext(tier_id="critical")
        result_critical = guard.check(context=ctx)
        assert result_critical.allowed is True


# =============================================================================
# PolicyHook Protocol — 계약 검증
# =============================================================================


class TestPolicyHookContract:
    """PolicyHook Protocol 구조적 하위타입 계약 검증.

    225 문서 §2.5에 정의된 Hook Protocol을 검증한다.
    """

    def test_runtime_checkable(self):
        """PolicyHook은 runtime_checkable Protocol이다."""
        assert hasattr(PolicyHook, "__protocol_attrs__") or hasattr(
            PolicyHook, "__abstractmethods__"
        )

    def test_conforming_hook_passes_isinstance(self):
        """Protocol 서명을 구현한 클래스는 isinstance 검사를 통과한다."""

        class StubHook:
            def on_execute(self, policy_name: str, attempt: int) -> None:
                pass

            def on_success(self, policy_name: str, result: PolicyResult) -> None:
                pass

            def on_failure(
                self, policy_name: str, error: Exception, attempt: int
            ) -> None:
                pass

            def on_retry(self, policy_name: str, attempt: int, delay: float) -> None:
                pass

            def on_reject(self, policy_name: str, reason: str) -> None:
                pass

        assert isinstance(StubHook(), PolicyHook)

    def test_non_conforming_class_fails_isinstance(self):
        """Protocol 서명을 구현하지 않은 클래스는 isinstance 검사를 실패한다."""

        class NotAHook:
            pass

        assert not isinstance(NotAHook(), PolicyHook)

    def test_hook_methods_callable(self):
        """Hook의 5개 메서드가 모두 호출 가능하다."""
        events: list[str] = []

        class RecordingHook:
            def on_execute(self, policy_name: str, attempt: int) -> None:
                events.append(f"execute:{policy_name}:{attempt}")

            def on_success(self, policy_name: str, result: PolicyResult) -> None:
                events.append(f"success:{policy_name}")

            def on_failure(
                self, policy_name: str, error: Exception, attempt: int
            ) -> None:
                events.append(f"failure:{policy_name}:{attempt}")

            def on_retry(self, policy_name: str, attempt: int, delay: float) -> None:
                events.append(f"retry:{policy_name}:{attempt}:{delay}")

            def on_reject(self, policy_name: str, reason: str) -> None:
                events.append(f"reject:{policy_name}:{reason}")

        hook = RecordingHook()
        hook.on_execute("retry", 1)
        hook.on_success("retry", PolicyResult(value="ok"))
        hook.on_failure("retry", ValueError("err"), 2)
        hook.on_retry("retry", 2, 5.0)
        hook.on_reject("circuit_breaker", "open")

        assert len(events) == 5
        assert events[0] == "execute:retry:1"
        assert events[1] == "success:retry"
        assert events[2] == "failure:retry:2"
        assert events[3] == "retry:retry:2:5.0"
        assert events[4] == "reject:circuit_breaker:open"

    def test_on_retry_receives_delay_info(self):
        """on_retry는 재시도 예정 시점에 delay 정보를 전달받는다.

        226 문서 §8.6 코드 근거: handler.py L540-542
        "재시도 예정" 시점에 별도 로그를 남기는 기존 패턴.
        on_failure와 의미론적으로 분리 — 마지막 시도 실패 시 on_retry 미호출.
        """
        retry_events: list[tuple[str, int, float]] = []

        class RetryTrackingHook:
            def on_execute(self, policy_name: str, attempt: int) -> None:
                pass

            def on_success(self, policy_name: str, result: PolicyResult) -> None:
                pass

            def on_failure(
                self, policy_name: str, error: Exception, attempt: int
            ) -> None:
                pass

            def on_retry(self, policy_name: str, attempt: int, delay: float) -> None:
                retry_events.append((policy_name, attempt, delay))

            def on_reject(self, policy_name: str, reason: str) -> None:
                pass

        hook = RetryTrackingHook()
        hook.on_retry("retry", 1, 2.5)
        hook.on_retry("retry", 2, 5.0)

        assert len(retry_events) == 2
        assert retry_events[0] == ("retry", 1, 2.5)
        assert retry_events[1] == ("retry", 2, 5.0)


# =============================================================================
# FailureSink Protocol — 계약 검증
# =============================================================================


class TestFailureSinkContract:
    """FailureSink Protocol 구조적 하위타입 계약 검증.

    225 문서 §2.6에 정의된 Sink Protocol을 검증한다.
    """

    def test_runtime_checkable(self):
        """FailureSink는 runtime_checkable Protocol이다."""
        assert hasattr(FailureSink, "__protocol_attrs__") or hasattr(
            FailureSink, "__abstractmethods__"
        )

    def test_conforming_sink_passes_isinstance(self):
        """Protocol 서명을 구현한 클래스는 isinstance 검사를 통과한다."""

        class StubSink:
            def handle_failure(
                self,
                error: Exception,
                context: PolicyContext | None,
                policy_result: PolicyResult,
            ) -> str | None:
                return None

        assert isinstance(StubSink(), FailureSink)

    def test_non_conforming_class_fails_isinstance(self):
        """Protocol 서명을 구현하지 않은 클래스는 isinstance 검사를 실패한다."""

        class NotASink:
            pass

        assert not isinstance(NotASink(), FailureSink)

    def test_sink_returns_failure_id(self):
        """Sink는 실패 기록 ID를 반환할 수 있다 (DLQ ID 등)."""

        class StubDLQSink:
            def handle_failure(
                self,
                error: Exception,
                context: PolicyContext | None,
                policy_result: PolicyResult,
            ) -> str | None:
                if context and context.order_id:
                    return f"dlq-{context.order_id}"
                return None

        sink = StubDLQSink()

        # context 있으면 DLQ ID 반환
        ctx = PolicyContext(order_id="ORD-001")
        err = RuntimeError("all retries exhausted")
        policy_result = PolicyResult(outcome=PolicyOutcome.FAILURE, error=err)
        dlq_id = sink.handle_failure(err, ctx, policy_result)
        assert dlq_id == "dlq-ORD-001"

    def test_sink_returns_none_without_context(self):
        """context=None일 때 Sink는 None을 반환할 수 있다."""

        class StubDLQSink:
            def handle_failure(
                self,
                error: Exception,
                context: PolicyContext | None,
                policy_result: PolicyResult,
            ) -> str | None:
                if context and context.order_id:
                    return f"dlq-{context.order_id}"
                return None

        sink = StubDLQSink()
        err = RuntimeError("test")
        policy_result = PolicyResult(outcome=PolicyOutcome.FAILURE, error=err)
        result = sink.handle_failure(err, None, policy_result)
        assert result is None


# =============================================================================
# __init__.py 공개 인터페이스 — 계약 검증
# =============================================================================


class TestPublicExportsContract:
    """interfaces/__init__.py에서 resilience_policy 타입이 올바르게 export된다."""

    def test_all_types_importable_from_interfaces(self):
        """모든 공개 타입이 baldur.interfaces에서 import 가능하다."""
        from baldur.interfaces import (
            AsyncResiliencePolicy,
            FailureSink,
            GuardResult,
            PolicyContext,
            PolicyGuard,
            PolicyHook,
            PolicyOutcome,
            PolicyResult,
            ResiliencePolicy,
        )

        # import 자체가 성공하면 통과
        assert PolicyOutcome is not None
        assert PolicyResult is not None
        assert PolicyContext is not None
        assert GuardResult is not None
        assert ResiliencePolicy is not None
        assert AsyncResiliencePolicy is not None
        assert PolicyGuard is not None
        assert PolicyHook is not None
        assert FailureSink is not None

    def test_types_in_all_list(self):
        """모든 resilience_policy 타입이 __all__에 포함되어 있다."""
        from baldur.interfaces import __all__ as all_exports

        expected = [
            "PolicyOutcome",
            "PolicyResult",
            "PolicyContext",
            "GuardResult",
            "ResiliencePolicy",
            "AsyncResiliencePolicy",
            "PolicyGuard",
            "PolicyHook",
            "FailureSink",
        ]
        for name in expected:
            assert name in all_exports, f"{name}이 __all__에 포함되어야 함"


# =============================================================================
# Async side-effect channel Protocols — 672 D1/D2 contract
# =============================================================================


class TestAsyncChannelProtocols:
    """AsyncPolicyGuard / AsyncPolicyHook / AsyncFailureSink are runtime_checkable
    Protocols whose channel methods are declared async (the AsyncPolicyComposer
    awaits them natively). Method names mirror the sync trio (check / on_* /
    handle_failure) — NOT an acheck / ahandle_* prefix.
    """

    def test_async_channel_protocols_are_runtime_checkable(self):
        for proto in (AsyncPolicyGuard, AsyncPolicyHook, AsyncFailureSink):
            assert hasattr(proto, "__protocol_attrs__") or hasattr(
                proto, "__abstractmethods__"
            ), f"{proto.__name__} must be a runtime_checkable Protocol"

    def test_async_channel_protocols_declare_async_methods(self):
        """The channel methods are declared with ``async def`` — this is what
        lets the composer ``await`` them without a thread hop."""
        assert inspect.iscoroutinefunction(AsyncPolicyGuard.check)
        for hook_method in (
            "on_execute",
            "on_success",
            "on_failure",
            "on_retry",
            "on_reject",
        ):
            assert inspect.iscoroutinefunction(getattr(AsyncPolicyHook, hook_method)), (
                f"AsyncPolicyHook.{hook_method} must be async"
            )
        assert inspect.iscoroutinefunction(AsyncFailureSink.handle_failure)

    def test_conforming_async_guard_passes_isinstance(self):
        class StubAsyncGuard:
            @property
            def name(self) -> str:
                return "async_guard"

            async def check(self, context: PolicyContext | None = None) -> GuardResult:
                return GuardResult(allowed=True)

        assert isinstance(StubAsyncGuard(), AsyncPolicyGuard)

    def test_non_conforming_async_guard_fails_isinstance(self):
        class NotAnAsyncGuard:
            pass

        assert not isinstance(NotAnAsyncGuard(), AsyncPolicyGuard)

    @pytest.mark.asyncio
    async def test_conforming_async_guard_check_awaits_to_guard_result(self):
        class StubAsyncGuard:
            @property
            def name(self) -> str:
                return "async_guard"

            async def check(self, context: PolicyContext | None = None) -> GuardResult:
                return GuardResult(allowed=context is not None)

        guard = StubAsyncGuard()
        allowed = await guard.check(PolicyContext(order_id="o"))
        rejected = await guard.check(None)
        assert allowed.allowed is True
        assert rejected.allowed is False

    def test_conforming_async_hook_passes_isinstance(self):
        class StubAsyncHook:
            async def on_execute(self, policy_name, attempt, context=None) -> None: ...
            async def on_success(self, policy_name, result, context=None) -> None: ...
            async def on_failure(
                self, policy_name, error, attempt, context=None
            ) -> None: ...
            async def on_retry(
                self, policy_name, attempt, delay, context=None
            ) -> None: ...
            async def on_reject(self, guard_name, reason, context=None) -> None: ...

        assert isinstance(StubAsyncHook(), AsyncPolicyHook)

    def test_conforming_async_sink_passes_isinstance(self):
        class StubAsyncSink:
            async def handle_failure(self, error, context, policy_result) -> str | None:
                return None

        assert isinstance(StubAsyncSink(), AsyncFailureSink)

    @pytest.mark.asyncio
    async def test_conforming_async_sink_handle_failure_awaits_to_id(self):
        class StubAsyncSink:
            async def handle_failure(self, error, context, policy_result) -> str | None:
                return f"dlq-{context.order_id}" if context else None

        sink = StubAsyncSink()
        err = RuntimeError("terminal")
        ctx = PolicyContext(order_id="o-9")
        result = PolicyResult(outcome=PolicyOutcome.FAILURE, error=err)
        assert await sink.handle_failure(err, ctx, result) == "dlq-o-9"

    def test_async_channel_protocols_exported_from_interfaces(self):
        from baldur.interfaces import (
            AsyncFailureSink as ExportedSink,
        )
        from baldur.interfaces import (
            AsyncPolicyGuard as ExportedGuard,
        )
        from baldur.interfaces import (
            AsyncPolicyHook as ExportedHook,
        )
        from baldur.interfaces import __all__ as all_exports

        assert ExportedGuard is AsyncPolicyGuard
        assert ExportedHook is AsyncPolicyHook
        assert ExportedSink is AsyncFailureSink
        for name in ("AsyncPolicyGuard", "AsyncPolicyHook", "AsyncFailureSink"):
            assert name in all_exports
