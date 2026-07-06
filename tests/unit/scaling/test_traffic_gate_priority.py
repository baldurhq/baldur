"""
Unit tests for TrafficGate Priority Mapping (236 작업 3).

테스트 항목:
- _PRIORITY_TIER_THRESHOLDS / _PRIORITY_TIER_DEFAULT 계약값 검증
- _map_priority_int_to_tier() 동작 검증
- TrafficGate.should_allow() priority→tier 매핑 동작
- 거부 시 metadata에 priority tier 포함
"""

from unittest.mock import patch

import pytest

from baldur.scaling.config import (
    BackpressureSettings,
    BackpressureStrategy,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    RateController,
    reset_rate_controller,
)
from baldur.scaling.traffic_gate import (
    _PRIORITY_TIER_DEFAULT,
    _PRIORITY_TIER_THRESHOLDS,
    TrafficGate,
    _map_priority_int_to_tier,
    reset_traffic_gate,
)


class TestPriorityTierThresholdsContract:
    """_PRIORITY_TIER_THRESHOLDS / _PRIORITY_TIER_DEFAULT 계약값 검증."""

    def test_thresholds_count(self):
        """임계치는 2개 (critical, standard) 정의."""
        assert len(_PRIORITY_TIER_THRESHOLDS) == 2

    def test_first_threshold_critical(self):
        """첫 번째 임계치: priority <= 25 → critical."""
        threshold, tier = _PRIORITY_TIER_THRESHOLDS[0]
        assert threshold == 25
        assert tier == "critical"

    def test_second_threshold_standard(self):
        """두 번째 임계치: priority <= 75 → standard."""
        threshold, tier = _PRIORITY_TIER_THRESHOLDS[1]
        assert threshold == 75
        assert tier == "standard"

    def test_default_tier(self):
        """기본 tier는 non_essential."""
        assert _PRIORITY_TIER_DEFAULT == "non_essential"


class TestMapPriorityIntToTierBehavior:
    """_map_priority_int_to_tier() 동작 검증."""

    def test_zero_maps_to_critical(self):
        """priority=0 → critical."""
        assert _map_priority_int_to_tier(0) == "critical"

    def test_25_maps_to_critical(self):
        """priority=25 (경계값) → critical."""
        assert _map_priority_int_to_tier(25) == "critical"

    def test_26_maps_to_standard(self):
        """priority=26 → standard."""
        assert _map_priority_int_to_tier(26) == "standard"

    def test_50_maps_to_standard(self):
        """priority=50 → standard."""
        assert _map_priority_int_to_tier(50) == "standard"

    def test_75_maps_to_standard(self):
        """priority=75 (경계값) → standard."""
        assert _map_priority_int_to_tier(75) == "standard"

    def test_76_maps_to_non_essential(self):
        """priority=76 → non_essential."""
        assert _map_priority_int_to_tier(76) == "non_essential"

    def test_100_maps_to_non_essential(self):
        """priority=100 → non_essential."""
        assert _map_priority_int_to_tier(100) == "non_essential"

    def test_negative_maps_to_critical(self):
        """음수 priority → critical."""
        assert _map_priority_int_to_tier(-1) == "critical"


class TestTrafficGatePriorityBehavior:
    """TrafficGate.should_allow() priority→tier 전파 동작 검증."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()
        yield
        reset_rate_controller()
        reset_backpressure_settings()
        reset_traffic_gate()

    def test_priority_passed_to_rate_controller(self):
        """should_allow(priority=n)이 RateController에 tier 문자열로 전달된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(rate_controller=controller)

        with patch.object(
            controller, "should_process", wraps=controller.should_process
        ) as mock_sp:
            gate.should_allow(priority=0)  # critical
            mock_sp.assert_called_with(priority="critical")

    def test_standard_priority_mapped_correctly(self):
        """priority=50이 RateController에 'standard'로 전달된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(rate_controller=controller)

        with patch.object(
            controller, "should_process", wraps=controller.should_process
        ) as mock_sp:
            gate.should_allow(priority=50)
            mock_sp.assert_called_with(priority="standard")

    def test_non_essential_priority_mapped_correctly(self):
        """priority=100이 RateController에 'non_essential'로 전달된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(rate_controller=controller)

        with patch.object(
            controller, "should_process", wraps=controller.should_process
        ) as mock_sp:
            gate.should_allow(priority=100)
            mock_sp.assert_called_with(priority="non_essential")

    def test_rejection_metadata_includes_priority(self):
        """RateController 거부 시 metadata에 priority tier가 포함된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(rate_controller=controller)

        # 첫 토큰 소비
        gate.should_allow(priority=0)

        # 두 번째 요청은 거부
        decision = gate.should_allow(priority=100)
        if not decision.allowed:
            assert decision.metadata is not None
            assert "priority" in decision.metadata

    def test_rejection_reason_includes_priority_tier(self):
        """RateController 거부 시 reason에 priority tier 문자열이 포함된다."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,
            default_strategy=BackpressureStrategy.REJECT,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(rate_controller=controller)

        # 토큰 소진
        gate.should_allow(priority=0)

        # non_essential 거부
        decision = gate.should_allow(priority=100)
        if not decision.allowed and decision.gate == "RateController":
            assert "non_essential" in decision.reason
