"""
CircuitBreakerPolicy 단위 테스트 (#227).

테스트 대상:
- services/circuit_breaker/policy.py (CircuitBreakerPolicy, circuit_breaker 데코레이터)
- services/circuit_breaker/exceptions.py (CircuitBreakerOpenError)

코드 근거 기반 검증:
- policy.py L80-82: name == "circuit_breaker"
- policy.py L118-123: CB disabled → 바로 실행, SUCCESS
- policy.py L126-134: should_allow() == False → REJECTED + CircuitBreakerOpenError
- policy.py L137-142: 성공 → record_success() → SUCCESS
- policy.py L143-150: 실패 → _is_failure() 판단 → record_failure() → 예외 재전파
- policy.py L96-103: ignore_exceptions → record_failure() 미호출
- policy.py L68-78: failure_exceptions 필터링
- policy.py L158-186: @circuit_breaker() 데코레이터
- exceptions.py L10-20: CircuitBreakerOpenError 속성

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (name, outcome, executed_policies)
- 동작 검증(Behavior): 소스 참조 (config, 상수)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyContext,
    PolicyOutcome,
    PolicyResult,
)
from baldur.services.circuit_breaker.config import CircuitBreakerDecision
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import (
    CircuitBreakerPolicy,
    circuit_breaker,
)


def _reject_decision(state_str: str = "open") -> CircuitBreakerDecision:
    """Helper for D2 reject mock setup — returns a False decision with the
    given string state. Used inline at sites that previously set
    ``should_allow.return_value = False`` and (optionally)
    ``get_state.return_value = "open"``.
    """
    return CircuitBreakerDecision(
        allowed=False,
        state=MagicMock(state=state_str),
    )


# =============================================================================
# Fixtures — 1개 파일 전용이므로 파일 내부 배치 (§5.1)
# =============================================================================


@pytest.fixture
def mock_cb_service():
    """CircuitBreakerService Mock — 기본 동작: enabled + allow.

    Post-#485 D2: ``CircuitBreakerPolicy.execute`` calls
    ``should_allow_with_state`` (companion API) instead of the old
    ``should_allow`` + ``get_state`` pair, so the default mock pre-configures
    a CLOSED admit decision. Tests that exercise the reject path override
    ``should_allow_with_state.return_value`` with a False/state="open"
    ``CircuitBreakerDecision``.
    """
    from baldur.services.circuit_breaker.config import CircuitBreakerDecision

    service = MagicMock()
    service.is_enabled = True
    service.should_allow.return_value = True
    service.should_allow_with_state.return_value = CircuitBreakerDecision(
        allowed=True,
        state=MagicMock(state="closed"),
    )
    service.get_state.return_value = "closed"
    service.record_success.return_value = None
    service.record_failure.return_value = None
    return service


@pytest.fixture
def policy(mock_cb_service):
    """기본 CircuitBreakerPolicy 인스턴스."""
    return CircuitBreakerPolicy(
        service_name="test_api",
        cb_service=mock_cb_service,
    )


@pytest.fixture
def disabled_cb_service():
    """CB 비활성화 상태 Mock."""
    service = MagicMock()
    service.is_enabled = False
    return service


# =============================================================================
# 계약 검증 (Contract)
# =============================================================================


class TestCircuitBreakerPolicyContract:
    """CircuitBreakerPolicy 고정 식별자 및 결과 구조 계약 검증."""

    def test_name_is_circuit_breaker(self, policy):
        """policy.py L80-82: name property는 'circuit_breaker'이다."""
        assert policy.name == "circuit_breaker"

    def test_service_name_property(self, policy):
        """policy.py L84-87: service_name property는 생성자에 전달한 값이다."""
        assert policy.service_name == "test_api"

    def test_cb_service_property(self, policy, mock_cb_service):
        """policy.py L89-92: cb_service property는 주입된 서비스 인스턴스이다."""
        assert policy.cb_service is mock_cb_service

    def test_success_result_has_circuit_breaker_in_executed_policies(self, policy):
        """성공 결과의 executed_policies에 'circuit_breaker'가 포함된다."""
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_rejected_result_has_circuit_breaker_in_executed_policies(
        self, mock_cb_service
    ):
        """거부 결과의 executed_policies에 'circuit_breaker'가 포함된다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_disabled_result_has_circuit_breaker_in_executed_policies(
        self, disabled_cb_service
    ):
        """비활성화 결과의 executed_policies에 'circuit_breaker'가 포함된다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: "ok")
        assert "circuit_breaker" in result.executed_policies

    def test_success_outcome_is_success(self, policy):
        """성공 시 outcome은 PolicyOutcome.SUCCESS이다."""
        result = policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_rejected_outcome_is_rejected(self, mock_cb_service):
        """거부 시 outcome은 PolicyOutcome.REJECTED이다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: 42)
        assert result.outcome == PolicyOutcome.REJECTED

    def test_default_failure_exceptions(self):
        """policy.py L73: failure_exceptions 기본값은 (Exception,)이다."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(
                is_enabled=True, should_allow=MagicMock(return_value=True)
            ),
        )
        assert policy._failure_exceptions == (Exception,)

    def test_default_ignore_exceptions(self):
        """policy.py L74: ignore_exceptions 기본값은 빈 튜플이다."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(
                is_enabled=True, should_allow=MagicMock(return_value=True)
            ),
        )
        assert policy._ignore_exceptions == ()


# =============================================================================
# CB 비활성화 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyDisabledBehavior:
    """CB 비활성화 시 동작 검증 — policy.py L118-123."""

    def test_disabled_cb_executes_function_directly(self, disabled_cb_service):
        """CB disabled → 함수를 바로 실행한다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: "direct_result")
        assert result.value == "direct_result"

    def test_disabled_cb_returns_success(self, disabled_cb_service):
        """CB disabled → outcome은 SUCCESS이다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        result = policy.execute(lambda: 123)
        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.success is True

    def test_disabled_cb_does_not_call_should_allow(self, disabled_cb_service):
        """CB disabled → should_allow()를 호출하지 않는다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        policy.execute(lambda: "ok")
        disabled_cb_service.should_allow.assert_not_called()

    def test_disabled_cb_does_not_call_record_success(self, disabled_cb_service):
        """CB disabled → record_success()를 호출하지 않는다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )
        policy.execute(lambda: "ok")
        disabled_cb_service.record_success.assert_not_called()

    def test_disabled_cb_passes_args_and_kwargs(self, disabled_cb_service):
        """CB disabled → args, kwargs가 함수에 전달된다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=disabled_cb_service
        )

        def func(a, b, key=None):
            return (a, b, key)

        result = policy.execute(func, 1, 2, key="val")
        assert result.value == (1, 2, "val")


# =============================================================================
# CB OPEN — 거부 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyRejectedBehavior:
    """CB OPEN 상태에서 거부 동작 검증 — policy.py L126-134."""

    def test_rejected_when_should_allow_false(self, mock_cb_service):
        """should_allow() == False → REJECTED 반환."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "should_not_run")
        assert result.rejected is True

    def test_rejected_error_is_circuit_breaker_open_error(self, mock_cb_service):
        """거부 시 error는 CircuitBreakerOpenError 인스턴스이다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert isinstance(result.error, CircuitBreakerOpenError)

    def test_rejected_error_has_service_name(self, mock_cb_service):
        """거부 시 error.service_name은 policy의 service_name과 동일하다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="payment_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.error.service_name == "payment_api"

    def test_rejected_metadata_contains_service_name(self, mock_cb_service):
        """거부 시 metadata에 service_name이 포함된다 — policy.py L131."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        mock_cb_service.get_state.return_value = "open"
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.metadata["service_name"] == "test_api"

    def test_rejected_metadata_contains_state(self, mock_cb_service):
        """거부 시 metadata에 state가 포함된다 — policy.py L132."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        mock_cb_service.get_state.return_value = "open"
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.metadata["state"] == "open"

    def test_rejected_does_not_execute_function(self, mock_cb_service):
        """거부 시 func은 실행되지 않는다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        func = MagicMock()
        policy.execute(func)
        func.assert_not_called()

    def test_rejected_value_is_none(self, mock_cb_service):
        """거부 시 value는 None이다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api", cb_service=mock_cb_service
        )
        result = policy.execute(lambda: "nope")
        assert result.value is None


# =============================================================================
# 성공 경로 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerPolicySuccessBehavior:
    """성공 경로 동작 검증 — policy.py L137-142."""

    def test_success_returns_function_value(self, policy):
        """성공 시 func의 반환값이 result.value에 담긴다."""
        result = policy.execute(lambda: "success_value")
        assert result.value == "success_value"

    def test_success_calls_record_success(self, policy, mock_cb_service):
        """성공 시 record_success(service_name, hint_state=...)를 호출한다 (490 D4)."""
        policy.execute(lambda: "ok")
        decision = mock_cb_service.should_allow_with_state.return_value
        mock_cb_service.record_success.assert_called_once_with(
            "test_api", hint_state=decision.state
        )

    def test_success_does_not_call_record_failure(self, policy, mock_cb_service):
        """성공 시 record_failure()를 호출하지 않는다."""
        policy.execute(lambda: "ok")
        mock_cb_service.record_failure.assert_not_called()

    def test_success_calls_should_allow_with_service_name(
        self, policy, mock_cb_service
    ):
        """should_allow_with_state()에 service_name을 전달한다 (#485 D2)."""
        policy.execute(lambda: "ok")
        mock_cb_service.should_allow_with_state.assert_called_once_with("test_api")

    def test_success_passes_args_to_function(self, policy):
        """args가 함수에 정확히 전달된다."""

        def add(a, b):
            return a + b

        result = policy.execute(add, 3, 7)
        assert result.value == 10

    def test_success_passes_kwargs_to_function(self, policy):
        """kwargs가 함수에 정확히 전달된다."""

        def greet(name, prefix="Hello"):
            return f"{prefix}, {name}"

        result = policy.execute(greet, "world", prefix="Hi")
        assert result.value == "Hi, world"

    def test_success_result_is_success_property_true(self, policy):
        """성공 result의 .success property는 True이다."""
        result = policy.execute(lambda: "ok")
        assert result.success is True

    def test_success_result_rejected_property_false(self, policy):
        """성공 result의 .rejected property는 False이다."""
        result = policy.execute(lambda: "ok")
        assert result.rejected is False


# =============================================================================
# 실패 경로 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyFailureBehavior:
    """실패 경로 동작 검증 — policy.py L143-155."""

    def test_failure_calls_record_failure_with_error_context(
        self, policy, mock_cb_service
    ):
        """실패 시 record_failure(service_name, error_context=..., hint_state=...) 호출 (490 D4)."""
        with pytest.raises(ValueError):
            policy.execute(self._raise_value_error)
        decision = mock_cb_service.should_allow_with_state.return_value
        mock_cb_service.record_failure.assert_called_once_with(
            "test_api",
            error_context={"error": "bad value", "type": "ValueError"},
            hint_state=decision.state,
        )

    def test_failure_reraises_exception(self, policy):
        """실패 시 예외를 상위로 재전파한다 — policy.py L155: raise."""
        with pytest.raises(ValueError, match="bad value"):
            policy.execute(self._raise_value_error)

    def test_failure_does_not_call_record_success(self, policy, mock_cb_service):
        """실패 시 record_success()를 호출하지 않는다."""
        with pytest.raises(ValueError):
            policy.execute(self._raise_value_error)
        mock_cb_service.record_success.assert_not_called()

    def test_failure_error_context_type_field(self, policy, mock_cb_service):
        """error_context의 type 필드는 예외 클래스명이다."""
        with pytest.raises(RuntimeError):
            policy.execute(self._raise_runtime_error)
        call_args = mock_cb_service.record_failure.call_args
        assert call_args[1]["error_context"]["type"] == "RuntimeError"

    def test_failure_error_context_error_field(self, policy, mock_cb_service):
        """error_context의 error 필드는 str(e)이다."""
        with pytest.raises(RuntimeError):
            policy.execute(self._raise_runtime_error)
        call_args = mock_cb_service.record_failure.call_args
        assert call_args[1]["error_context"]["error"] == "runtime fail"

    @staticmethod
    def _raise_value_error():
        raise ValueError("bad value")

    @staticmethod
    def _raise_runtime_error():
        raise RuntimeError("runtime fail")


# =============================================================================
# 예외 필터링 동작 검증 (Behavior) — _is_failure()
# =============================================================================


class TestCircuitBreakerPolicyExceptionFilterBehavior:
    """예외 필터링 동작 검증 — policy.py L96-103, §7.2."""

    def test_ignore_exceptions_skips_record_failure(self, mock_cb_service):
        """ignore_exceptions에 해당하는 예외는 record_failure()를 호출하지 않는다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("ignored")))
        mock_cb_service.record_failure.assert_not_called()

    def test_ignore_exceptions_still_reraises(self, mock_cb_service):
        """ignore_exceptions에 해당해도 예외는 상위로 재전파된다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError, match="ignored"):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("ignored")))

    def test_failure_exceptions_only_counts_specified_types(self, mock_cb_service):
        """failure_exceptions=(ValueError,) → ValueError만 record_failure 호출."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(ValueError,),
        )
        # ValueError → record_failure 호출
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("counted")))
        assert mock_cb_service.record_failure.call_count == 1

    def test_failure_exceptions_ignores_non_matching_types(self, mock_cb_service):
        """failure_exceptions=(ValueError,) → RuntimeError는 record_failure 미호출."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(ValueError,),
        )
        with pytest.raises(RuntimeError):
            policy.execute(lambda: (_ for _ in ()).throw(RuntimeError("not counted")))
        mock_cb_service.record_failure.assert_not_called()

    def test_ignore_takes_precedence_over_failure(self, mock_cb_service):
        """ignore_exceptions이 failure_exceptions보다 우선한다 — policy.py L100-101."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(Exception,),
            ignore_exceptions=(ValueError,),
        )
        with pytest.raises(ValueError):
            policy.execute(lambda: (_ for _ in ()).throw(ValueError("both")))
        mock_cb_service.record_failure.assert_not_called()

    def test_is_failure_with_subclass(self, mock_cb_service):
        """failure_exceptions의 서브클래스도 실패로 카운팅된다."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            failure_exceptions=(OSError,),
        )
        with pytest.raises(ConnectionError):  # ConnectionError는 OSError의 서브클래스
            policy.execute(lambda: (_ for _ in ()).throw(ConnectionError("subclass")))
        assert mock_cb_service.record_failure.call_count == 1

    def test_is_failure_method_directly(self):
        """_is_failure() 메서드 직접 검증."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=MagicMock(),
            failure_exceptions=(ValueError, TypeError),
            ignore_exceptions=(KeyError,),
        )
        assert policy._is_failure(ValueError("v")) is True
        assert policy._is_failure(TypeError("t")) is True
        assert policy._is_failure(KeyError("k")) is False
        assert policy._is_failure(RuntimeError("r")) is False


# =============================================================================
# PolicyContext 전달 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerPolicyContextBehavior:
    """PolicyContext 전달 동작 검증."""

    def test_execute_accepts_context_parameter(self, policy):
        """execute()에 context 파라미터를 전달할 수 있다."""
        ctx = PolicyContext(order_id="order-123", trace_id="trace-abc")
        result = policy.execute(lambda: "with_context", context=ctx)
        assert result.value == "with_context"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_execute_works_without_context(self, policy):
        """context=None (기본값)으로도 정상 동작한다."""
        result = policy.execute(lambda: "no_context")
        assert result.value == "no_context"
        assert result.outcome == PolicyOutcome.SUCCESS


# =============================================================================
# CircuitBreakerOpenError 계약 검증 (Contract)
# =============================================================================


class TestCircuitBreakerOpenErrorContract:
    """CircuitBreakerOpenError 예외 계약 검증 — exceptions.py."""

    def test_service_name_attribute(self):
        """exceptions.py L18: service_name 속성이 설정된다."""
        error = CircuitBreakerOpenError("payment_api")
        assert error.service_name == "payment_api"

    def test_default_message_format(self):
        """exceptions.py L19: 기본 메시지 형식."""
        error = CircuitBreakerOpenError("payment_api")
        assert str(error) == "Circuit breaker 'payment_api' is OPEN"

    def test_custom_message(self):
        """exceptions.py L18: 사용자 정의 메시지."""
        error = CircuitBreakerOpenError("api", message="custom msg")
        assert str(error) == "custom msg"

    def test_inherits_from_exception(self):
        """exceptions.py L10: Exception을 상속한다."""
        error = CircuitBreakerOpenError("test")
        assert isinstance(error, Exception)

    def test_is_not_base_exception(self):
        """Exception 상속 — BaseException이 아닌 일반 Exception 계열."""
        from baldur.core.exceptions import BaldurError, CircuitBreakerError

        error = CircuitBreakerOpenError("test")
        assert isinstance(error, Exception)
        assert isinstance(error, CircuitBreakerError)
        assert isinstance(error, BaldurError)


# =============================================================================
# circuit_breaker 데코레이터 동작 검증 (Behavior)
# =============================================================================


class TestCircuitBreakerDecoratorBehavior:
    """@circuit_breaker() 데코레이터 동작 검증 — policy.py L158-186."""

    def test_decorator_wraps_function_with_policy(self):
        """데코레이터 적용 시 wrapper에 .policy 속성이 부착된다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("test_api", cb_service=mock_service)
        def my_func():
            return "hello"

        assert hasattr(my_func, "policy")
        assert isinstance(my_func.policy, CircuitBreakerPolicy)

    def test_decorator_preserves_function_name(self):
        """데코레이터는 원래 함수 이름을 보존한다 (@wraps)."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True

        @circuit_breaker("test_api", cb_service=mock_service)
        def original_function():
            """Original docstring."""
            return "ok"

        assert original_function.__name__ == "original_function"
        assert original_function.__doc__ == "Original docstring."

    def test_decorator_uses_qualname_when_service_name_none(self):
        """service_name=None → func.__qualname__을 기본값으로 사용한다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker(service_name=None, cb_service=mock_service)
        def my_special_func():
            return "ok"

        # __qualname__은 테스트 클래스 내 정의이므로 클래스명.함수명 형태
        expected_qualname = (
            my_special_func.__wrapped__.__qualname__
            if hasattr(my_special_func, "__wrapped__")
            else "my_special_func"
        )
        assert my_special_func.policy.service_name == expected_qualname

    def test_decorator_returns_policy_result(self):
        """데코레이터 적용 함수 호출 시 PolicyResult를 반환한다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("test_api", cb_service=mock_service)
        def my_func():
            return 42

        result = my_func()
        assert isinstance(result, PolicyResult)
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_decorator_explicit_service_name(self):
        """명시적 service_name이 Policy에 전달된다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = True
        mock_service.record_success.return_value = None

        @circuit_breaker("payment_api", cb_service=mock_service)
        def pay():
            return "paid"

        assert pay.policy.service_name == "payment_api"

    def test_decorator_passes_failure_exceptions(self):
        """failure_exceptions가 Policy에 전달된다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True

        @circuit_breaker(
            "test_api",
            cb_service=mock_service,
            failure_exceptions=(ValueError, TypeError),
        )
        def func():
            return "ok"

        assert func.policy._failure_exceptions == (ValueError, TypeError)

    def test_decorator_passes_ignore_exceptions(self):
        """ignore_exceptions가 Policy에 전달된다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True

        @circuit_breaker(
            "test_api",
            cb_service=mock_service,
            ignore_exceptions=(KeyError,),
        )
        def func():
            return "ok"

        assert func.policy._ignore_exceptions == (KeyError,)

    def test_decorator_rejected_when_cb_open(self):
        """CB OPEN 시 데코레이터 함수도 REJECTED를 반환한다."""
        mock_service = MagicMock()
        mock_service.is_enabled = True
        mock_service.should_allow.return_value = False
        mock_service.should_allow_with_state.return_value = _reject_decision()
        mock_service.get_state.return_value = "open"

        @circuit_breaker("test_api", cb_service=mock_service)
        def func():
            return "should_not_run"

        result = func()
        assert result.rejected is True
        assert isinstance(result.error, CircuitBreakerOpenError)


# =============================================================================
# Behavior — sync helper extraction parity (670 D2 / R1)
# =============================================================================


class TestCircuitBreakerHelperExtractionBehavior:
    """The extracted ``_admit`` / ``_direct_result`` / ``_on_success`` /
    ``_on_failure`` helpers preserve the sync state machine (behavior-preserving
    R1 refactor) and are the single source of truth the async wrapper reuses."""

    def test_admit_returns_direct_verdict_when_disabled(self, disabled_cb_service):
        """CB disabled → ``_admit`` verdict is 'direct' (run once, never record)."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=disabled_cb_service,
        )

        verdict, reject_result, hint_state = policy._admit()

        assert verdict == "direct"
        assert reject_result is None
        assert hint_state is None

    def test_admit_returns_run_verdict_with_hint_state_when_allowed(
        self, policy, mock_cb_service
    ):
        """Admitted → verdict 'run' and hint_state is the decision's state object."""
        decision = mock_cb_service.should_allow_with_state.return_value

        verdict, reject_result, hint_state = policy._admit()

        assert verdict == "run"
        assert reject_result is None
        # 490 D4: the loaded state is threaded through so record_* skips a refetch.
        assert hint_state is decision.state

    def test_admit_returns_reject_verdict_when_open(self, mock_cb_service):
        """CB OPEN → verdict 'reject' with a REJECTED CircuitBreakerOpenError result."""
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
        )

        verdict, reject_result, hint_state = policy._admit()

        assert verdict == "reject"
        assert reject_result.outcome == PolicyOutcome.REJECTED
        assert isinstance(reject_result.error, CircuitBreakerOpenError)
        assert hint_state is None

    def test_direct_result_builds_success_without_recording(
        self, policy, mock_cb_service
    ):
        """``_direct_result`` wraps the value in SUCCESS and records nothing."""
        result = policy._direct_result("value")

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "value"
        assert result.executed_policies == ["circuit_breaker"]
        mock_cb_service.record_success.assert_not_called()
        mock_cb_service.record_failure.assert_not_called()

    def test_on_success_records_with_hint_state(self, policy, mock_cb_service):
        """``_on_success`` forwards the hint_state to ``record_success``."""
        sentinel = object()

        result = policy._on_success("value", sentinel)

        assert result.outcome == PolicyOutcome.SUCCESS
        mock_cb_service.record_success.assert_called_once_with(
            "test_api", hint_state=sentinel
        )

    def test_on_failure_records_when_counted_as_failure(self, policy, mock_cb_service):
        """``_on_failure`` records a counted failure (default failure_exceptions)."""
        sentinel = object()
        error = RuntimeError("boom")

        policy._on_failure(error, sentinel)

        mock_cb_service.record_failure.assert_called_once()
        assert mock_cb_service.record_failure.call_args.kwargs["hint_state"] is sentinel

    def test_on_failure_skips_record_for_ignored_exception(self, mock_cb_service):
        """``_on_failure`` skips ``record_failure`` for an ignored exception type."""
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            ignore_exceptions=(KeyError,),
        )

        policy._on_failure(KeyError("ignored"), None)

        mock_cb_service.record_failure.assert_not_called()

    def test_closed_success_does_zero_extra_state_acquire(
        self, policy, mock_cb_service
    ):
        """R1: a CLOSED steady-state success does NOT re-acquire state.

        ``should_allow_with_state`` is the single fetch; ``record_success``
        reuses its hint_state, so neither ``get_or_create_state`` nor the legacy
        ``get_state`` lookup runs on the hot path.
        """
        result = policy.execute(lambda: "ok")

        assert result.outcome == PolicyOutcome.SUCCESS
        mock_cb_service.should_allow_with_state.assert_called_once_with("test_api")
        mock_cb_service.get_or_create_state.assert_not_called()
        mock_cb_service.get_state.assert_not_called()


# =============================================================================
# Behavior — @circuit_breaker async dual-dispatch (670 D5 / G7)
# =============================================================================


def _real_low_threshold_service(failure_threshold: int = 2):
    """A real CircuitBreakerService (InMemory) that opens after ``failure_threshold``."""
    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.services.circuit_breaker.config import CircuitBreakerConfig
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    config = CircuitBreakerConfig(
        enabled=True,
        failure_threshold=failure_threshold,
        minimum_calls=1,
        failure_rate_threshold=0,
        recovery_timeout=60,
    )
    return CircuitBreakerService(
        config=config,
        repository=InMemoryCircuitBreakerStateRepository(),
    )


class TestCircuitBreakerDecoratorAsyncBehavior:
    """``@circuit_breaker`` on an ``async def`` protects it (no silent bypass)."""

    def test_decorator_async_returns_policy_result(self):
        """An async-decorated function, when awaited, returns a PolicyResult."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_success.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return 42

        result = asyncio.run(call_async())

        assert isinstance(result, PolicyResult)
        assert result.value == 42
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_decorator_async_wrapper_exposes_sync_policy(self):
        """``wrapper.policy`` exposes the underlying sync CircuitBreakerPolicy."""
        service = MagicMock()
        service.is_enabled = True

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return "ok"

        assert isinstance(call_async.policy, CircuitBreakerPolicy)
        assert call_async.policy.cb_service is service

    def test_decorator_async_records_awaited_success(self):
        """The awaited outcome (not coroutine creation) drives ``record_success``."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_success.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            return "done"

        asyncio.run(call_async())

        service.record_success.assert_called_once()
        service.record_failure.assert_not_called()

    def test_decorator_async_records_awaited_failure_and_reraises(self):
        """An awaited failure records a real failure and re-raises (no false success)."""
        service = MagicMock()
        service.is_enabled = True
        service.should_allow_with_state.return_value = CircuitBreakerDecision(
            allowed=True, state=MagicMock(state="closed")
        )
        service.record_failure.return_value = None

        @circuit_breaker("async_api", cb_service=service)
        async def call_async():
            raise RuntimeError("async boom")

        with pytest.raises(RuntimeError, match="async boom"):
            asyncio.run(call_async())

        service.record_failure.assert_called_once()
        service.record_success.assert_not_called()

    def test_decorator_async_preserves_function_name(self):
        """functools.wraps preserves the async function's name and docstring."""
        service = MagicMock()
        service.is_enabled = True

        @circuit_breaker("async_api", cb_service=service)
        async def original_async():
            """Async docstring."""
            return "ok"

        assert original_async.__name__ == "original_async"
        assert original_async.__doc__ == "Async docstring."

    def test_decorator_async_opens_after_threshold_failures(self):
        """Accumulated awaited failures open the breaker → later calls REJECTED.

        The G7 fix: before 670 the sync wrapper wrapped an un-awaited coroutine
        and recorded a false success, so the breaker never opened on an async
        target. Here real awaited failures accumulate and trip it.
        """
        from baldur.services.circuit_breaker.config import CircuitState

        service = _real_low_threshold_service(failure_threshold=2)

        @circuit_breaker("async_open_api", cb_service=service)
        async def boom():
            raise RuntimeError("dependency down")

        rejected_seen = False
        for _ in range(5):
            try:
                result = asyncio.run(boom())
                if result.rejected:
                    rejected_seen = True
            except RuntimeError:
                pass

        assert rejected_seen is True
        assert service.get_or_create_state("async_open_api").state == CircuitState.OPEN
