"""
ConnectionHealthMonitor bulkhead integration tests.

Verifies behavior after bulkhead_states joined PartitionState:
- bulkhead state collection in get_partition_state()
- has_bulkhead_pressure property behavior
"""

from __future__ import annotations

import pytest

from baldur.core.connection_health import (
    ConnectionType,
    DefaultConnectionHealthMonitor,
    PartitionState,
)
from baldur.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.settings.bulkhead import reset_bulkhead_settings


@pytest.fixture(autouse=True)
def _empty_provider_slot(monkeypatch):
    """Pin the resolution chain to its fallback leg for this module.

    The collection path resolves the registry via the chain; forcing the
    provider slot empty keeps the observed compartments deterministic and
    lets reset_bulkhead_registry() fully isolate each test.
    """
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
    )


@pytest.fixture(autouse=True)
def reset_singletons(_empty_provider_slot):
    """Reset singletons before and after each test."""
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()


class TestPartitionStateBulkheadStates:
    """PartitionState.bulkhead_states field tests."""

    def test_partition_state_has_bulkhead_states_field(self):
        """PartitionState carries a bulkhead_states field."""
        state = PartitionState()

        assert hasattr(state, "bulkhead_states")
        assert state.bulkhead_states == {}

    def test_partition_state_bulkhead_pressure_false_when_empty(self):
        """Empty bulkhead_states → has_bulkhead_pressure=False."""
        state = PartitionState()

        assert state.has_bulkhead_pressure is False

    def test_partition_state_bulkhead_pressure_false_when_low_utilization(self):
        """Utilization at or below 80% → has_bulkhead_pressure=False."""
        state = PartitionState(
            bulkhead_states={
                "database": {"utilization_percent": 50.0},
                "cache": {"utilization_percent": 30.0},
            }
        )

        assert state.has_bulkhead_pressure is False

    def test_partition_state_bulkhead_pressure_true_when_high_utilization(self):
        """Utilization above 80% → has_bulkhead_pressure=True."""
        state = PartitionState(
            bulkhead_states={
                "database": {"utilization_percent": 50.0},
                "cache": {"utilization_percent": 85.0},  # above 80%
            }
        )

        assert state.has_bulkhead_pressure is True


class TestConnectionHealthMonitorBulkheadCollection:
    """DefaultConnectionHealthMonitor bulkhead state collection tests."""

    def test_get_partition_state_includes_bulkhead_states(self):
        """get_partition_state() includes bulkhead_states."""
        monitor = DefaultConnectionHealthMonitor()

        state = monitor.get_partition_state()

        assert hasattr(state, "bulkhead_states")
        assert isinstance(state.bulkhead_states, dict)

        # The built-in compartments must be present
        assert "database" in state.bulkhead_states
        assert "cache" in state.bulkhead_states
        assert "external_api" in state.bulkhead_states
        assert "message_queue" in state.bulkhead_states

    def test_bulkhead_states_have_required_fields(self):
        """Each bulkhead_states entry carries the required fields."""
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
        """bulkhead_states reflects the current occupancy."""
        monitor = DefaultConnectionHealthMonitor()
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        # Occupy a few slots
        db_bulkhead.try_acquire()
        db_bulkhead.try_acquire()

        state = monitor.get_partition_state()

        assert state.bulkhead_states["database"]["active_count"] == 2

        # Cleanup
        db_bulkhead.release()
        db_bulkhead.release()

    def test_collect_bulkhead_states_method(self):
        """Directly exercise _collect_bulkhead_states()."""
        monitor = DefaultConnectionHealthMonitor()

        states = monitor._collect_bulkhead_states()

        assert isinstance(states, dict)
        assert len(states) >= 4  # at least the four built-in compartments
