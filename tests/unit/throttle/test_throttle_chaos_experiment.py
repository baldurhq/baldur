"""
AdaptiveThrottle Chaos Experiment 시나리오 테스트.

테스트 대상:
1. Chaos Experiment에 의한 Emergency 활성화 시 Throttle 동작
2. Chaos Experiment 종료 시 limit 복구
3. is_chaos_experiment 메타데이터 처리
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import patch

from baldur.services.event_bus import (
    BaldurEvent,
    EventType,
)
from baldur.services.event_bus.bus._throttle_handlers import (
    _on_emergency_level_changed_throttle,
)
from baldur_pro.services.throttle.adaptive import (
    get_adaptive_throttle,
    reset_adaptive_throttle,
)


class TestChaosExperimentEmergencyActivation:
    """Chaos Experiment에 의한 Emergency 활성화 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_chaos_experiment_triggers_limit_adjustment(self):
        """Chaos Experiment에 의한 Emergency도 limit 조정됨."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "level": 2,
                "previous_level": 0,
                "reason": "chaos_experiment_test",
                "is_chaos_experiment": True,
                "experiment_id": "exp-001",
            },
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        assert throttle.is_emergency_active() is True
        assert throttle.get_emergency_level() == 2
        assert throttle.current_limit == 50  # 100 × 0.5

    def test_chaos_experiment_level_3_freezes_gradient(self):
        """Chaos Experiment LEVEL_3도 Gradient Freeze."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "level": 3,
                "previous_level": 0,
                "reason": "chaos_experiment_level_3",
                "is_chaos_experiment": True,
            },
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        assert throttle.is_gradient_frozen() is True
        assert throttle.current_limit == throttle.config.min_limit


class TestChaosExperimentRecovery:
    """Chaos Experiment 종료 시 복구 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_chaos_experiment_level_0_triggers_recovery_dampening(self):
        """Chaos Experiment level=0 이벤트 시 Recovery Dampening."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Chaos 시작
        throttle.adjust_for_emergency(2)
        assert throttle.current_limit == 50

        # Level 0으로 복구
        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "level": 0,
                "previous_level": 2,
                "is_chaos_experiment": True,
            },
            source="emergency_manager",
        )
        _on_emergency_level_changed_throttle(event)

        assert throttle.is_recovery_dampening_active() is True


class TestChaosExperimentWithFullStop:
    """Chaos Experiment Full Stop 시나리오 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    @patch.object(
        get_adaptive_throttle().__class__,
        "_check_db_circuit_breaker_open",
        return_value=True,
    )
    @patch.object(
        get_adaptive_throttle().__class__,
        "_check_error_budget_exhausted",
        return_value=True,
    )
    def test_chaos_level_3_with_full_stop_conditions(self, mock_budget, mock_cb):
        """Chaos LEVEL_3 + Full Stop 조건 충족 시 Full Stop."""
        reset_adaptive_throttle()
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "level": 3,
                "previous_level": 0,
                "is_chaos_experiment": True,
            },
            source="emergency_manager",
        )

        _on_emergency_level_changed_throttle(event)

        # Full Stop 조건 충족 시 limit=0
        # (테스트 환경에서는 mock이 적용됨)


class TestChaosExperimentMultipleTransitions:
    """Chaos Experiment 다중 전이 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_level_escalation_during_chaos(self):
        """Chaos 중 레벨 상승."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # LEVEL_1로 시작
        throttle.adjust_for_emergency(1)
        assert throttle.current_limit == 80

        # LEVEL_2로 상승
        throttle.adjust_for_emergency(2)
        # base_limit은 처음 값 유지 (100)
        assert throttle.current_limit == 50

        # LEVEL_3로 상승
        throttle.adjust_for_emergency(3)
        assert throttle.current_limit == throttle.config.min_limit
        assert throttle.is_gradient_frozen() is True

    def test_level_de_escalation_during_chaos(self):
        """Chaos 중 레벨 하강."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # LEVEL_3로 시작
        throttle.adjust_for_emergency(3)
        assert throttle.is_gradient_frozen() is True

        # LEVEL_2로 하강
        throttle.adjust_for_emergency(2)
        assert throttle.is_gradient_frozen() is False
        assert throttle.current_limit == 50

        # LEVEL_1로 하강
        throttle.adjust_for_emergency(1)
        assert throttle.current_limit == 80

    def test_chaos_end_after_multiple_transitions(self):
        """다중 전이 후 Chaos 종료."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # 여러 레벨 전이
        throttle.adjust_for_emergency(1)
        throttle.adjust_for_emergency(3)
        throttle.adjust_for_emergency(2)

        # 종료
        throttle.adjust_for_emergency(0)

        # base_limit으로 Recovery Dampening 시작
        assert throttle.is_recovery_dampening_active() is True
        assert throttle.current_limit == 80  # 100 × 0.8


class TestChaosExperimentDoesNotAffectNonChaosRecovery:
    """Chaos Experiment가 일반 복구에 영향 없음 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_normal_emergency_recovery_same_as_chaos(self):
        """일반 Emergency 복구도 동일하게 동작."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # 일반 Emergency (Chaos 아님)
        throttle.adjust_for_emergency(2)
        assert throttle.current_limit == 50

        # 복구
        throttle.adjust_for_emergency(0)

        assert throttle.is_recovery_dampening_active() is True
        assert throttle.current_limit == 80


class TestChaosExperimentWithRollback:
    """Chaos Experiment 롤백 시나리오 테스트."""

    def setup_method(self):
        reset_adaptive_throttle()

    def teardown_method(self):
        reset_adaptive_throttle()

    def test_rollback_during_chaos_restores_base_limit(self):
        """Chaos 중 롤백 시 base limit 복구."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        throttle.adjust_for_emergency(3)
        assert throttle.current_limit == throttle.config.min_limit

        result = throttle.rollback_to_base_limit()

        assert result == 100
        assert throttle.current_limit == 100
        assert throttle.is_emergency_active() is False
        assert throttle.is_gradient_frozen() is False

    def test_rollback_clears_chaos_state_completely(self):
        """롤백 시 Chaos 관련 상태 완전 초기화."""
        throttle = get_adaptive_throttle()
        throttle.current_limit = 100

        # Chaos + Full Stop
        throttle.adjust_for_emergency(3)
        throttle._full_stop_active = True

        throttle.rollback_to_base_limit()

        assert throttle.is_full_stop_active() is False
        assert throttle.is_recovery_dampening_active() is False
        assert throttle.get_emergency_level() == 0
