"""
CircuitBreakerPolicy 훅 통합 + _should_open_circuit window cap + Protocol 상속 테스트 (#227).

테스트 대상:
- policy.py: _invoke_hooks(), hooks 파라미터, _create_default_service(), ResiliencePolicy[T] 상속
- service.py: _should_open_circuit() — sliding_window_size cap 로직

코드 근거:
- CircuitBreakerPolicy._invoke_hooks(): Fail-Open으로 모든 훅 호출
- execute(): on_execute(시작), on_reject(CB OPEN 거부), on_success(성공), on_failure(실패) 시점 훅 호출
- CircuitBreakerPolicy(ResiliencePolicy[T]): 명시적 Protocol 상속 → isinstance() 통과
- hooks 파라미터: None이면 빈 리스트(transition-only, #494). 외부 사용자는 `hooks=[…]`로 주입 가능
- _create_default_service(): ProviderRegistry "layered" 시도 → 실패 시 기본값 fallback
- _should_open_circuit(): sliding_window_size > 0이고 total_calls > window_size면 cap 적용
  count-based threshold는 failure_count 원본 사용

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): Protocol 상속, hooks 기본값
- 동작 검증(Behavior): 소스 참조, Mock 호출 순서 검증
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import (
    PolicyOutcome,
    PolicyResult,
    ResiliencePolicy,
)
from baldur.services.circuit_breaker.config import (
    CircuitBreakerConfig,
    CircuitBreakerDecision,
)
from baldur.services.circuit_breaker.exceptions import CircuitBreakerOpenError
from baldur.services.circuit_breaker.policy import (
    CircuitBreakerPolicy,
)
from baldur.services.event_bus import EventType


def _reject_decision(state_str: str = "open") -> CircuitBreakerDecision:
    """Helper for D2 reject mock setup — see test_circuit_breaker_policy.py."""
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
    ``should_allow_with_state`` (companion API). The default mock returns a
    CLOSED admit decision; reject-path tests override it.
    """
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
def mock_hook():
    """범용 Mock Hook — 모든 메서드를 가진 MagicMock."""
    hook = MagicMock()
    hook.on_execute = MagicMock()
    hook.on_success = MagicMock()
    hook.on_failure = MagicMock()
    hook.on_retry = MagicMock()
    hook.on_reject = MagicMock()
    return hook


@pytest.fixture
def policy_with_mock_hook(mock_cb_service, mock_hook):
    """mock_hook이 주입된 CircuitBreakerPolicy."""
    return CircuitBreakerPolicy(
        service_name="test_api",
        cb_service=mock_cb_service,
        hooks=[mock_hook],
    )


# =============================================================================
# ResiliencePolicy[T] Protocol 상속 계약 검증 (Contract)
# =============================================================================


class TestCircuitBreakerPolicyProtocolContract:
    """CircuitBreakerPolicy의 ResiliencePolicy[T] Protocol 상속 검증 — policy.py L37."""

    def test_isinstance_resilience_policy(self, mock_cb_service):
        """CircuitBreakerPolicy 인스턴스는 ResiliencePolicy isinstance 검사를 통과한다."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert isinstance(policy, ResiliencePolicy)

    def test_has_name_property(self, mock_cb_service):
        """ResiliencePolicy Protocol 요구: name property 존재."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert hasattr(policy, "name")
        assert isinstance(policy.name, str)

    def test_has_execute_method(self, mock_cb_service):
        """ResiliencePolicy Protocol 요구: execute method 존재."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        assert hasattr(policy, "execute")
        assert callable(policy.execute)

    def test_mro_includes_resilience_policy(self):
        """CircuitBreakerPolicy MRO에 ResiliencePolicy가 포함된다."""
        mro_names = [cls.__name__ for cls in CircuitBreakerPolicy.__mro__]
        assert "ResiliencePolicy" in mro_names


# =============================================================================
# hooks 파라미터 계약 검증 (Contract)
# =============================================================================


class TestCircuitBreakerPolicyHooksParamContract:
    """hooks 파라미터 기본값 및 주입 계약 검증."""

    def test_default_hooks_is_empty(self, mock_cb_service):
        """hooks=None → 빈 리스트 (transition-only, #494)."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
        )
        # Post-#494: per-reject hook bodies live nowhere by default. State
        # transitions are published by ``CircuitBreakerService``;
        # ``baldur_circuit_breaker_blocked_total`` covers per-reject volume.
        assert policy._hooks == []

    def test_custom_hooks_override_defaults(self, mock_cb_service):
        """hooks=[custom] → _hooks가 커스텀 훅으로 설정된다."""
        custom_hook = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[custom_hook],
        )
        assert policy._hooks == [custom_hook]

    def test_empty_hooks_list_accepted(self, mock_cb_service):
        """hooks=[] → 빈 리스트가 설정된다 (훅 없음)."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[],
        )
        assert policy._hooks == []


# =============================================================================
# _invoke_hooks 동작 검증 (Behavior)
# =============================================================================


class TestInvokeHooksBehavior:
    """_invoke_hooks() Fail-Open 동작 검증."""

    def test_calls_hook_method_with_args(self, mock_cb_service):
        """_invoke_hooks()는 지정된 메서드를 올바른 인자로 호출한다."""
        hook = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook],
        )
        policy._invoke_hooks("on_execute", "test_service", 1)
        hook.on_execute.assert_called_once_with("test_service", 1)

    def test_calls_all_hooks(self, mock_cb_service):
        """여러 훅이 있으면 모두 호출한다."""
        hook1 = MagicMock()
        hook2 = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook1, hook2],
        )
        policy._invoke_hooks("on_reject", "svc", "reason")
        hook1.on_reject.assert_called_once_with("svc", "reason")
        hook2.on_reject.assert_called_once_with("svc", "reason")

    def test_fail_open_swallows_hook_exception(self, mock_cb_service):
        """hooks.py Fail-Open: 훅 예외가 _invoke_hooks()를 중단시키지 않는다."""
        hook = MagicMock()
        hook.on_execute.side_effect = RuntimeError("hook crashed")
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook],
        )
        # 예외가 전파되지 않아야 함
        policy._invoke_hooks("on_execute", "svc", 1)

    def test_subsequent_hooks_called_after_first_fails(self, mock_cb_service):
        """첫 번째 훅이 실패해도 후속 훅은 정상 호출된다."""
        hook1 = MagicMock()
        hook1.on_reject.side_effect = RuntimeError("hook1 failed")
        hook2 = MagicMock()
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[hook1, hook2],
        )
        policy._invoke_hooks("on_reject", "svc", "reason")
        # hook1 실패 후에도 hook2가 호출된다
        hook2.on_reject.assert_called_once_with("svc", "reason")

    def test_empty_hooks_no_error(self, mock_cb_service):
        """hooks=[] 일 때 _invoke_hooks()는 에러 없이 반환한다."""
        policy = CircuitBreakerPolicy(
            service_name="test",
            cb_service=mock_cb_service,
            hooks=[],
        )
        policy._invoke_hooks("on_execute", "svc", 1)


# =============================================================================
# execute() 훅 호출 순서 동작 검증 (Behavior)
# =============================================================================


class TestPolicyExecuteHooksIntegrationBehavior:
    """execute()에서 훅이 올바른 시점에 호출되는지 검증."""

    def test_on_execute_called_when_cb_enabled(self, policy_with_mock_hook, mock_hook):
        """CB enabled 시 on_execute가 호출된다."""
        policy_with_mock_hook.execute(lambda: "ok")
        mock_hook.on_execute.assert_called_once_with("test_api", 1)

    def test_on_execute_not_called_when_cb_disabled(self, mock_hook):
        """CB disabled 시 on_execute가 호출되지 않는다 (early return)."""
        disabled_service = MagicMock()
        disabled_service.is_enabled = False
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=disabled_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_execute.assert_not_called()

    def test_on_reject_called_when_should_allow_false(self, mock_cb_service, mock_hook):
        """should_allow() False 시 on_reject가 호출된다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_reject.assert_called_once_with("test_api", "circuit_open")

    def test_on_success_called_on_successful_execution(
        self, policy_with_mock_hook, mock_hook
    ):
        """성공 시 on_success가 호출된다."""
        policy_with_mock_hook.execute(lambda: "result")
        assert mock_hook.on_success.call_count == 1
        call_args = mock_hook.on_success.call_args
        assert call_args[0][0] == "test_api"
        # 두 번째 인자는 PolicyResult
        assert isinstance(call_args[0][1], PolicyResult)

    def test_on_failure_called_on_exception(self, policy_with_mock_hook, mock_hook):
        """실패 시 on_failure가 호출된다."""
        with pytest.raises(ValueError):
            policy_with_mock_hook.execute(
                lambda: (_ for _ in ()).throw(ValueError("fail"))
            )
        mock_hook.on_failure.assert_called_once()
        call_args = mock_hook.on_failure.call_args
        assert call_args[0][0] == "test_api"
        assert isinstance(call_args[0][1], ValueError)
        assert call_args[0][2] == 1  # attempt

    def test_on_success_not_called_on_rejection(self, mock_cb_service, mock_hook):
        """거부 시 on_success는 호출되지 않는다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_success.assert_not_called()

    def test_on_failure_not_called_on_rejection(self, mock_cb_service, mock_hook):
        """거부 시 on_failure는 호출되지 않는다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[mock_hook],
        )
        policy.execute(lambda: "ok")
        mock_hook.on_failure.assert_not_called()

    def test_hook_failure_does_not_affect_execution_result(self, mock_cb_service):
        """훅 예외가 execute() 결과에 영향을 주지 않는다 (Fail-Open)."""
        failing_hook = MagicMock()
        failing_hook.on_execute.side_effect = RuntimeError("hook crash")
        failing_hook.on_success.side_effect = RuntimeError("hook crash")
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[failing_hook],
        )
        result = policy.execute(lambda: "safe_result")
        assert result.value == "safe_result"
        assert result.outcome == PolicyOutcome.SUCCESS

    def test_hook_failure_on_reject_does_not_affect_result(self, mock_cb_service):
        """on_reject 훅 실패가 거부 결과에 영향을 주지 않는다."""
        mock_cb_service.should_allow.return_value = False
        mock_cb_service.should_allow_with_state.return_value = _reject_decision()
        failing_hook = MagicMock()
        failing_hook.on_execute = MagicMock()
        failing_hook.on_reject.side_effect = RuntimeError("hook crash")
        policy = CircuitBreakerPolicy(
            service_name="test_api",
            cb_service=mock_cb_service,
            hooks=[failing_hook],
        )
        result = policy.execute(lambda: "ok")
        assert result.outcome == PolicyOutcome.REJECTED
        assert isinstance(result.error, CircuitBreakerOpenError)


# =============================================================================
# _create_default_service 동작 검증 (Behavior)
# =============================================================================


class TestCreateDefaultServiceBehavior:
    """_create_default_service() 동작 검증."""

    def test_create_default_service_returns_circuit_breaker_service(self):
        """_create_default_service()는 CircuitBreakerService를 반환한다."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_circuit_breaker_repo.side_effect = ValueError(
                "not registered"
            )
            service = CircuitBreakerPolicy._create_default_service()
            assert isinstance(service, CircuitBreakerService)

    def test_create_default_service_tries_layered_first(self):
        """_create_default_service()는 'layered' repo를 먼저 시도한다."""
        mock_repo = MagicMock()
        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_circuit_breaker_repo.return_value = mock_repo
            CircuitBreakerPolicy._create_default_service()
            mock_registry.get_circuit_breaker_repo.assert_called_once_with(
                name="layered"
            )

    def test_create_default_service_fallback_on_value_error(self):
        """'layered' 미등록(ValueError) 시 repository=None으로 fallback한다."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_circuit_breaker_repo.side_effect = ValueError("not found")
            service = CircuitBreakerPolicy._create_default_service()
            assert isinstance(service, CircuitBreakerService)

    def test_create_default_service_fallback_on_import_error(self):
        """ProviderRegistry import 실패(ImportError) 시 repository=None으로 fallback한다."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        with patch.dict("sys.modules", {"baldur.factory": None}):
            # ImportError는 try/except에서 잡히므로 정상 동작
            service = CircuitBreakerPolicy._create_default_service()
            assert isinstance(service, CircuitBreakerService)

    def test_create_default_service_passes_config(self):
        """config 파라미터가 CircuitBreakerService에 전달된다."""
        config = CircuitBreakerConfig(failure_threshold=10)
        with patch("baldur.factory.ProviderRegistry") as mock_registry:
            mock_registry.get_circuit_breaker_repo.side_effect = ValueError("nope")
            service = CircuitBreakerPolicy._create_default_service(config=config)
            assert service.config.failure_threshold == config.failure_threshold


# =============================================================================
# _should_open_circuit sliding_window_size cap 동작 검증 (Behavior)
# =============================================================================


class TestShouldOpenCircuitWindowCapBehavior:
    """_should_open_circuit() sliding_window_size cap 로직 검증."""

    @pytest.fixture
    def cb_service(self):
        """테스트용 CircuitBreakerService."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,  # count-based는 높게 설정하여 비활성
            failure_rate_threshold=50.0,  # rate-based 활성화
            sliding_window_size=10,
            minimum_calls=5,
        )
        return CircuitBreakerService(config=config, repository=repo)

    def test_total_calls_capped_to_window_size(self, cb_service):
        """total_calls > window_size → cap 적용."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # window_size=10, total_calls=200 → 200 > 10이므로 cap
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=100,
            success_count=100,
        )
        # failure_rate = 100 / 10 * 100 = 1000% → cap 적용 후 rate 계산
        # 그러나 failure_count(100) > window_size(10)이므로 rate가 100% 넘어감
        # rate = failure_count / capped_total * 100 = 100 / 10 * 100 = 1000%
        # → True (open)
        result = cb_service._should_open_circuit(state)
        assert result is True

    def test_no_cap_when_total_within_window(self, cb_service):
        """total_calls <= window_size → cap 미적용."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # window_size=10, total_calls=8 → 8 <= 10이므로 cap 미적용
        # failure_rate = 2/8 * 100 = 25% < 50.0% → False
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=2,
            success_count=6,
        )
        result = cb_service._should_open_circuit(state)
        assert result is False

    def test_no_cap_when_window_size_zero(self):
        """window_size=0 → cap 로직 비활성."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        config = CircuitBreakerConfig(
            enabled=True,
            failure_rate_threshold=50.0,
            sliding_window_size=0,  # cap 비활성
            minimum_calls=5,
            failure_threshold=100,
        )
        repo = InMemoryCircuitBreakerStateRepository(sliding_window_size=0)
        service = CircuitBreakerService(config=config, repository=repo)

        # total_calls=200, window_size=0 → cap 조건(window_size > 0) 불충족
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=50,
            success_count=150,
        )
        # failure_rate = 50/200 * 100 = 25.0% < 50.0% → False
        result = service._should_open_circuit(state)
        assert result is False

    def test_count_based_uses_original_failure_count(self):
        """count-based threshold는 failure_count 원본을 사용한다."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=5,  # count-based 활성화
            failure_rate_threshold=0.0,  # rate-based 비활성
            sliding_window_size=10,
            minimum_calls=3,
        )
        repo = InMemoryCircuitBreakerStateRepository()
        service = CircuitBreakerService(config=config, repository=repo)

        # failure_count=6 >= failure_threshold=5 → True
        # total_calls=20 > window_size=10 → cap 적용하지만 count-based는 원본 사용
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=6,
            success_count=14,
        )
        result = service._should_open_circuit(state)
        assert result is True

    def test_rate_based_with_cap_calculates_correct_rate(self, cb_service):
        """cap 적용 후 failure_rate가 올바르게 계산된다."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # window_size=10, failure_rate_threshold=50.0%
        # failure_count=3, success_count=20 → total=23 > 10 → cap→10
        # failure_rate = 3/10 * 100 = 30.0% < 50.0% → False
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=3,
            success_count=20,
        )
        result = cb_service._should_open_circuit(state)
        assert result is False

    def test_rate_threshold_exceeded_with_cap(self, cb_service):
        """cap 적용 후에도 rate >= threshold이면 True를 반환한다."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # window_size=10, failure_rate_threshold=50.0%
        # failure_count=6, success_count=20 → total=26 > 10 → cap→10
        # failure_rate = 6/10 * 100 = 60.0% >= 50.0% → True
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=6,
            success_count=20,
        )
        result = cb_service._should_open_circuit(state)
        assert result is True

    def test_minimum_calls_check_before_cap(self, cb_service):
        """minimum_calls 미충족 시 False (cap 이전에 total_calls < minimum_calls 체크)."""
        from baldur.interfaces.repositories import CircuitBreakerStateData

        # minimum_calls=5, total_calls=3 < 5 → False
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=3,
            success_count=0,
        )
        result = cb_service._should_open_circuit(state)
        assert result is False

    def test_cap_applied_before_minimum_calls_check(self):
        """cap은 minimum_calls 체크 전에 적용된다."""
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.interfaces.repositories import CircuitBreakerStateData
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        config = CircuitBreakerConfig(
            enabled=True,
            failure_rate_threshold=50.0,
            sliding_window_size=3,  # cap = 3
            minimum_calls=5,  # minimum = 5 > cap → 항상 minimum_calls < total 불충족
            failure_threshold=100,
        )
        repo = InMemoryCircuitBreakerStateRepository()
        service = CircuitBreakerService(config=config, repository=repo)

        # total_calls=100, cap→3 < minimum_calls(5) → False
        state = CircuitBreakerStateData(
            service_name="svc",
            failure_count=50,
            success_count=50,
        )
        result = service._should_open_circuit(state)
        assert result is False


# =============================================================================
# _should_open_circuit threshold boundaries (§8.1)
# =============================================================================


class TestShouldOpenCircuitThresholdBoundaries:
    """Exact-boundary coverage for the rate-based trip gate.

    The window-cap tests above exercise only coarse over/under rates (60% and
    30% against a 50% threshold), so a ``>=`` → ``>`` drift on the rate gate
    still trips at 60% and goes undetected — yet at exactly the threshold it
    would now fail to trip (a failing dependency held open one tick too long).
    Pin the gate at its exact boundary. (The count and minimum_calls gates are
    already pinned at their boundaries by the integration paths in
    ``test_service`` / ``test_protection``; only the rate gate was unguarded.)
    """

    @staticmethod
    def _service(**cfg):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        base = {"enabled": True, "sliding_window_size": 0}
        base.update(cfg)
        return CircuitBreakerService(
            config=CircuitBreakerConfig(**base),
            repository=InMemoryCircuitBreakerStateRepository(),
        )

    @staticmethod
    def _state(failure, success):
        from baldur.interfaces.repositories import CircuitBreakerStateData

        return CircuitBreakerStateData(
            service_name="svc", failure_count=failure, success_count=success
        )

    def test_rate_threshold_trips_exactly_at_threshold(self):
        """failure_rate == failure_rate_threshold trips (the ``>=`` boundary, not ``>``)."""
        # count gate held out of reach so only the rate gate decides.
        svc = self._service(
            failure_threshold=100, failure_rate_threshold=50.0, minimum_calls=1
        )
        assert svc._should_open_circuit(self._state(5, 5)) is True  # at: 50% >= 50%
        assert svc._should_open_circuit(self._state(4, 6)) is False  # below: 40%


# =============================================================================
# _evaluate_admission gates (downstream checker + recovery-timeout boundary)
# =============================================================================


class TestEvaluateAdmissionGates:
    """Admission gates that survived the full circuit-breaker suite: the
    downstream-checker polarity and the OPEN recovery-timeout boundary."""

    @staticmethod
    def _service(**cfg):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        base = {"enabled": True}
        base.update(cfg)
        return CircuitBreakerService(
            config=CircuitBreakerConfig(**base),
            repository=InMemoryCircuitBreakerStateRepository(),
        )

    def test_downstream_checker_false_preempts_to_fallback(self):
        """A registered downstream checker returning False preempts the call
        (``allowed=False``); returning True does not block a healthy CLOSED
        circuit. Pins the ``if not checker`` polarity — a flip would invert
        fail-open vs fail-closed on the preemptive-fallback path."""
        svc = self._service()

        svc.register_downstream_checker(lambda service_name: True)
        assert svc.should_allow_with_state("svc").allowed is True

        svc.register_downstream_checker(lambda service_name: False)
        assert svc.should_allow_with_state("svc").allowed is False

    def test_open_circuit_at_exact_recovery_timeout_admits_probe(self):
        """At elapsed == recovery_timeout the OPEN circuit is no longer
        short-circuit rejected (strict ``<``): it proceeds to acquire a
        half-open probe slot and admits. A ``<`` → ``<=`` drift would block
        recovery one tick too long."""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import patch

        svc = self._service(recovery_timeout=60, half_open_max_calls=1)
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        # Materialize the entry first; update_state only persists an existing one.
        svc.get_or_create_state("svc")
        svc.repository.update_state(service_name="svc", state="open", opened_at=t0)

        # elapsed = now(t0 + 60) - opened_at(t0) == recovery_timeout(60) exactly.
        with patch(
            "baldur.services.circuit_breaker.service.utc_now",
            return_value=t0 + timedelta(seconds=60),
        ):
            decision = svc.should_allow_with_state("svc")

        assert decision.allowed is True


# =============================================================================
# Per-transition emission guard (#494 D2 regression-prevention)
# =============================================================================


class TestCBOpenedEmittedPerTransitionBehavior:
    """``CIRCUIT_BREAKER_OPENED`` is emitted per state-transition, not per-reject.

    Post-#494 the policy default ``hooks=[]`` removes per-reject EventBus
    emission. The publisher-side contract is: a single ``closed→open``
    transition must emit ``CIRCUIT_BREAKER_OPENED`` exactly once even when N
    subsequent ``policy.execute()`` calls are rejected while the breaker
    sits in OPEN. (A separate ``half_open→open`` re-trip would legitimately
    emit again — that is out of scope for this test.)
    """

    def test_one_transition_n_rejects_emits_once(self):
        from baldur.adapters.memory.circuit_breaker import (
            InMemoryCircuitBreakerStateRepository,
        )
        from baldur.services.circuit_breaker.service import (
            CircuitBreakerService,
        )

        repo = InMemoryCircuitBreakerStateRepository()
        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=2,
            failure_rate_threshold=0.0,
            minimum_calls=1,
            sliding_window_size=10,
            recovery_timeout=86_400,
        )
        cb_service = CircuitBreakerService(config=config, repository=repo)

        bus = MagicMock()
        cb_service._event_bus = bus

        policy = CircuitBreakerPolicy(
            service_name="svc",
            cb_service=cb_service,
        )

        def boom():
            raise RuntimeError("trip")

        # Drive failures up to threshold — second failure trips closed→open.
        for _ in range(2):
            with pytest.raises(RuntimeError):
                policy.execute(boom)

        # N rejected calls — CB is OPEN with bumped recovery_timeout.
        for _ in range(20):
            result = policy.execute(lambda: "ok")
            assert result.outcome == PolicyOutcome.REJECTED

        opened_emits = [
            call
            for call in bus.emit.call_args_list
            if call.args and call.args[0] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_emits) == 1, (
            f"expected exactly 1 CIRCUIT_BREAKER_OPENED emission, got "
            f"{len(opened_emits)}"
        )
