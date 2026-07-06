"""
ConnectionHealthMonitor Bulkhead 통합 테스트.

PartitionState에 bulkhead_states 추가 후 동작을 검증합니다:
- get_partition_state()에서 bulkhead 상태 수집
- has_bulkhead_pressure 속성 동작
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import pytest

from baldur.core.connection_health import (
    ConnectionType,
    DefaultConnectionHealthMonitor,
    PartitionState,
)
from baldur.settings.bulkhead import reset_bulkhead_settings
from baldur_pro.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """각 테스트 전후로 싱글톤 초기화."""
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()


class TestPartitionStateBulkheadStates:
    """PartitionState의 bulkhead_states 필드 테스트."""

    def test_partition_state_has_bulkhead_states_field(self):
        """PartitionState에 bulkhead_states 필드 존재."""
        state = PartitionState()

        assert hasattr(state, "bulkhead_states")
        assert state.bulkhead_states == {}

    def test_partition_state_bulkhead_pressure_false_when_empty(self):
        """bulkhead_states가 비어있으면 has_bulkhead_pressure=False."""
        state = PartitionState()

        assert state.has_bulkhead_pressure is False

    def test_partition_state_bulkhead_pressure_false_when_low_utilization(self):
        """사용률이 80% 이하면 has_bulkhead_pressure=False."""
        state = PartitionState(
            bulkhead_states={
                "database": {"utilization_percent": 50.0},
                "cache": {"utilization_percent": 30.0},
            }
        )

        assert state.has_bulkhead_pressure is False

    def test_partition_state_bulkhead_pressure_true_when_high_utilization(self):
        """사용률이 80% 초과면 has_bulkhead_pressure=True."""
        state = PartitionState(
            bulkhead_states={
                "database": {"utilization_percent": 50.0},
                "cache": {"utilization_percent": 85.0},  # 80% 초과
            }
        )

        assert state.has_bulkhead_pressure is True


class TestConnectionHealthMonitorBulkheadCollection:
    """DefaultConnectionHealthMonitor의 Bulkhead 상태 수집 테스트."""

    def test_get_partition_state_includes_bulkhead_states(self):
        """get_partition_state()에서 bulkhead_states 포함."""
        monitor = DefaultConnectionHealthMonitor()

        state = monitor.get_partition_state()

        assert hasattr(state, "bulkhead_states")
        assert isinstance(state.bulkhead_states, dict)

        # 기본 격벽들이 포함되어야 함
        assert "database" in state.bulkhead_states
        assert "cache" in state.bulkhead_states
        assert "external_api" in state.bulkhead_states
        assert "message_queue" in state.bulkhead_states

    def test_bulkhead_states_have_required_fields(self):
        """bulkhead_states의 각 항목에 필수 필드 존재."""
        monitor = DefaultConnectionHealthMonitor()

        state = monitor.get_partition_state()

        for _name, bh_state in state.bulkhead_states.items():
            assert "type" in bh_state
            assert "max_concurrent" in bh_state
            assert "active_count" in bh_state
            assert "waiting_count" in bh_state
            assert "rejected_count" in bh_state
            assert "available_permits" in bh_state
            assert "utilization_percent" in bh_state

    def test_bulkhead_states_reflect_current_state(self):
        """bulkhead_states가 현재 상태 반영."""
        monitor = DefaultConnectionHealthMonitor()
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        # 일부 슬롯 점유
        db_bulkhead.try_acquire()
        db_bulkhead.try_acquire()

        state = monitor.get_partition_state()

        assert state.bulkhead_states["database"]["active_count"] == 2

        # 정리
        db_bulkhead.release()
        db_bulkhead.release()

    def test_collect_bulkhead_states_method(self):
        """_collect_bulkhead_states() 메서드 직접 테스트."""
        monitor = DefaultConnectionHealthMonitor()

        states = monitor._collect_bulkhead_states()

        assert isinstance(states, dict)
        assert len(states) >= 4  # 최소 4개의 기본 격벽
