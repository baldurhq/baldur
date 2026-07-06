"""
단위 테스트 — TrafficGate의 Dynamic Fast-Fail (RTT 기반).

테스트 항목:
- deadline 만료 시 거부 (기존 동작 유지)
- estimated > remaining 시 Fast-Fail 거부
- metadata["tier_id"]로 Tier별 Calculator 조회
- deadline 미설정 시 기존 파이프라인 통과
- ImportError 시 기존 동작 유지 (Fail-Open)
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock

import pytest

from baldur.scaling.deadline_context import (
    _request_deadline,
    set_deadline,
)
from baldur.scaling.traffic_gate import TrafficGate, reset_traffic_gate
from baldur_pro.services.throttle.gradient import (
    get_gradient_calculator,
    reset_gradient_calculators,
)


@pytest.fixture(autouse=True)
def _reset_state():
    """각 테스트 전후로 TrafficGate 싱글톤, Calculator 레지스트리, deadline ContextVar를 초기화."""
    reset_traffic_gate()
    reset_gradient_calculators()
    _request_deadline.set(None)
    yield
    reset_traffic_gate()
    reset_gradient_calculators()
    _request_deadline.set(None)


class TestTrafficGateDynamicFastFailBehavior:
    """TrafficGate Dynamic Fast-Fail 동작 검증."""

    @pytest.fixture
    def mock_rate_controller(self):
        from baldur.scaling.config import BackpressureLevel

        controller = MagicMock()
        state = MagicMock()
        state.level = BackpressureLevel.NONE
        controller.get_state.return_value = state
        controller.should_process.return_value = True
        return controller

    def test_expired_deadline_rejected(self, mock_rate_controller):
        """deadline 만료 시 거부 (기존 동작 유지)."""
        set_deadline(0.0)  # 즉시 만료
        gate = TrafficGate(rate_controller=mock_rate_controller)

        decision = gate.should_allow(priority=50)

        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
        assert "expired" in decision.reason.lower()

    def test_fast_fail_when_estimated_exceeds_remaining(self, mock_rate_controller):
        """estimated > remaining 시 Fast-Fail 거부."""
        # 300ms deadline 설정 (buffer 50ms 차감 → 실질 250ms)
        set_deadline(300.0)

        # RTT를 높게 설정하여 estimated가 remaining을 초과하도록 함
        calc = get_gradient_calculator("admission_control:standard")
        calc.add_sample(500.0)
        calc.add_sample(500.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        # estimated ≈ 500 * 1.5 = 750ms > remaining 250ms → Fast-Fail
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
        assert "fast-fail" in decision.reason.lower()
        assert decision.metadata.get("fast_fail") is True

    def test_fast_fail_with_tier_metadata(self, mock_rate_controller):
        """metadata["tier_id"]를 사용하여 Tier별 Calculator를 조회한다."""
        set_deadline(300.0)  # 실질 250ms

        # critical tier Calculator에 높은 RTT 설정
        calc = get_gradient_calculator("admission_control:critical")
        calc.add_sample(500.0)
        calc.add_sample(500.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        decision = gate.should_allow(
            priority=10,
            metadata={"tier_id": "critical"},
        )

        # critical Calculator에 데이터가 있으므로 Fast-Fail 발생
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"

    def test_no_deadline_allows_through(self, mock_rate_controller):
        """deadline 미설정 시 기존 파이프라인 동작 (Fast-Fail 미발생)."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        assert decision.allowed is True

    def test_plenty_deadline_allows_through(self, mock_rate_controller):
        """deadline 넉넉하고 estimated가 작으면 허용."""
        set_deadline(10000.0)  # 10초

        calc = get_gradient_calculator("admission_control:standard")
        calc.add_sample(100.0)
        calc.add_sample(100.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        # estimated ≈ 150ms < remaining 9950ms → 허용
        assert decision.allowed is True

    def test_fast_fail_metadata_contains_estimated_ms(self, mock_rate_controller):
        """Fast-Fail 시 metadata에 estimated_ms가 포함된다."""
        set_deadline(100.0)  # 매우 짧은 deadline

        calc = get_gradient_calculator("admission_control:standard")
        calc.add_sample(300.0)
        calc.add_sample(300.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        assert decision.allowed is False
        assert "estimated_ms" in decision.metadata
        assert isinstance(decision.metadata["estimated_ms"], float)

    def test_default_tier_used_when_metadata_missing(self, mock_rate_controller):
        """metadata에 tier_id가 없으면 'standard' tier를 기본으로 사용한다."""
        set_deadline(100.0)  # 매우 짧은 deadline (buffer 후 50ms)

        # standard tier Calculator에 높은 RTT 설정
        calc = get_gradient_calculator("admission_control:standard")
        calc.add_sample(500.0)
        calc.add_sample(500.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        # metadata 없이 호출
        decision = gate.should_allow(priority=50)

        # standard 기본 Calculator 사용 → Fast-Fail 발생
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"

    def test_cold_start_with_short_deadline_fast_fails(self, mock_rate_controller):
        """Cold Start 시에도 Tier별 기본값으로 Fast-Fail이 작동한다."""
        # standard 기본값 = 200ms, deadline = 100ms (buffer 50ms → 실질 50ms)
        set_deadline(100.0)

        gate = TrafficGate(rate_controller=mock_rate_controller)
        decision = gate.should_allow(
            priority=50,
            metadata={"tier_id": "standard"},
        )

        # Cold Start → estimated = 200ms (standard 기본값)
        # remaining ≈ 50ms < 200ms → Fast-Fail
        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
