"""
단위 테스트 — TrafficGate의 Deadline 만료 체크.

테스트 항목:
- deadline 만료 시 TrafficGate가 거부
- deadline 미설정 시 기존 동작 유지
- deadline 넉넉 시 기존 파이프라인 통과
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.scaling.deadline_context import (
    _request_deadline,
    set_deadline,
)
from baldur.scaling.traffic_gate import TrafficGate, reset_traffic_gate


@pytest.fixture(autouse=True)
def _reset_state():
    """각 테스트 전후로 TrafficGate 싱글톤과 deadline ContextVar를 초기화."""
    reset_traffic_gate()
    _request_deadline.set(None)
    yield
    reset_traffic_gate()
    _request_deadline.set(None)


class TestTrafficGateDeadlineBehavior:
    """TrafficGate Deadline 만료 체크 동작 검증."""

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
        """deadline 만료 시 거부."""
        set_deadline(0.0)  # 즉시 만료
        gate = TrafficGate(rate_controller=mock_rate_controller)

        decision = gate.should_allow(priority=50)

        assert decision.allowed is False
        assert decision.gate == "DeadlineContext"
        assert "expired" in decision.reason.lower()

    def test_no_deadline_allows_through(self, mock_rate_controller):
        """deadline 미설정 시 기존 파이프라인 동작."""
        gate = TrafficGate(rate_controller=mock_rate_controller)

        decision = gate.should_allow(priority=50)

        assert decision.allowed is True

    def test_plenty_deadline_allows_through(self, mock_rate_controller):
        """deadline 넉넉 시 허용."""
        set_deadline(5000.0)
        gate = TrafficGate(rate_controller=mock_rate_controller)

        decision = gate.should_allow(priority=50)

        assert decision.allowed is True
