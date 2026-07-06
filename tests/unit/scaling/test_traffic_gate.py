"""
Unit tests for TrafficGate and TrafficDecision.

테스트 항목:
- TrafficDecision 데이터클래스
- TrafficGate should_allow 기본 동작
- TrafficGate와 LoadShedding 통합
- TrafficGate 싱글톤
"""

from unittest.mock import Mock

import pytest

from baldur.scaling.config import (
    BackpressureLevel,
    BackpressureSettings,
    reset_backpressure_settings,
)
from baldur.scaling.rate_controller import (
    RateController,
    reset_rate_controller,
)
from baldur.scaling.traffic_gate import (
    TrafficDecision,
    TrafficGate,
    get_traffic_gate,
)


class TestTrafficDecision:
    """TrafficDecision 데이터클래스 테스트."""

    def test_allowed_decision(self):
        """허용 결정 생성."""
        decision = TrafficDecision(
            allowed=True,
            reason="Allowed",
            level=BackpressureLevel.NONE,
            gate="TrafficGate",
        )

        assert decision.allowed is True
        assert decision.reason == "Allowed"
        assert decision.level == BackpressureLevel.NONE
        assert decision.gate == "TrafficGate"
        assert decision.metadata is None

    def test_rejected_decision(self):
        """거부 결정 생성."""
        decision = TrafficDecision(
            allowed=False,
            reason="Rate limit exceeded",
            level=BackpressureLevel.HIGH,
            gate="RateController",
            metadata={"priority": 5},
        )

        assert decision.allowed is False
        assert decision.reason == "Rate limit exceeded"
        assert decision.level == BackpressureLevel.HIGH
        assert decision.gate == "RateController"
        assert decision.metadata == {"priority": 5}


class TestTrafficGate:
    """TrafficGate 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_should_allow_when_rate_available(self):
        """Rate 가용 시 허용."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(
            rate_controller=controller,
        )

        decision = gate.should_allow()

        assert decision.allowed is True
        assert decision.gate == "TrafficGate"

    def test_should_reject_when_rate_exceeded(self):
        """Rate 초과 시 거부."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=1.0,  # 매우 낮은 rate
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(
            rate_controller=controller,
        )

        # 첫 번째는 성공
        decision1 = gate.should_allow()
        assert decision1.allowed is True

        # 두 번째는 거부 (토큰 부족)
        decision2 = gate.should_allow()
        assert decision2.allowed is False
        assert decision2.gate == "RateController"

    def test_get_level(self):
        """현재 레벨 조회."""
        settings = BackpressureSettings()
        controller = RateController(settings=settings)
        gate = TrafficGate(
            rate_controller=controller,
        )

        level = gate.get_level()
        assert level == BackpressureLevel.NONE

    def test_metadata_passed_through(self):
        """메타데이터가 결과에 전달되는지 확인."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)
        gate = TrafficGate(
            rate_controller=controller,
        )

        metadata = {"request_id": "abc123", "user_id": "user1"}
        decision = gate.should_allow(metadata=metadata)

        assert decision.metadata == metadata


class TestTrafficGateWithLoadShedding:
    """TrafficGate와 LoadShedding 통합 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        yield
        reset_rate_controller()
        reset_backpressure_settings()

    def test_load_shedding_rejects_first(self):
        """LoadShedding이 먼저 거부할 수 있음."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # Mock LoadShedding
        mock_load_shedding = Mock()
        mock_load_shedding.should_accept.return_value = {"accepted": False}

        gate = TrafficGate(
            rate_controller=controller,
            load_shedding=mock_load_shedding,
        )

        decision = gate.should_allow(priority=10)

        assert decision.allowed is False
        assert decision.gate == "CascadeLoadShedding"
        mock_load_shedding.should_accept.assert_called_once()

    def test_load_shedding_allows_then_rate_controller_checks(self):
        """LoadShedding 허용 후 RateController 확인."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # Mock LoadShedding (허용)
        mock_load_shedding = Mock()
        mock_load_shedding.should_accept.return_value = {"accepted": True}

        gate = TrafficGate(
            rate_controller=controller,
            load_shedding=mock_load_shedding,
        )

        decision = gate.should_allow(priority=5)

        assert decision.allowed is True
        assert decision.gate == "TrafficGate"

    def test_load_shedding_error_handled_gracefully(self):
        """LoadShedding 오류 시 graceful 처리."""
        settings = BackpressureSettings(
            backpressure_enabled=True,
            max_rate_per_second=10000.0,
        )
        controller = RateController(settings=settings)

        # Mock LoadShedding (예외 발생)
        mock_load_shedding = Mock()
        mock_load_shedding.should_accept.side_effect = Exception("Load shedding error")

        gate = TrafficGate(
            rate_controller=controller,
            load_shedding=mock_load_shedding,
        )

        # 예외가 발생해도 RateController로 진행
        decision = gate.should_allow()

        assert decision.allowed is True
        assert decision.gate == "TrafficGate"


class TestTrafficGateSingleton:
    """TrafficGate 싱글톤 테스트."""

    @pytest.fixture(autouse=True)
    def reset_singletons(self):
        """각 테스트 전후로 싱글톤 리셋."""
        reset_rate_controller()
        reset_backpressure_settings()
        # traffic_gate 전역 인스턴스도 리셋 필요
        import baldur.scaling.traffic_gate as traffic_gate_module

        traffic_gate_module._traffic_gate = None
        yield
        reset_rate_controller()
        reset_backpressure_settings()
        traffic_gate_module._traffic_gate = None

    def test_singleton_returns_same_instance(self):
        """싱글톤이 동일한 인스턴스를 반환하는지 확인."""
        gate1 = get_traffic_gate()
        gate2 = get_traffic_gate()
        assert gate1 is gate2
