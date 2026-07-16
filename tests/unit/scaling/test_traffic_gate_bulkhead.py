"""
TrafficGate Bulkhead integration tests.

Verify the behavior after adding the bulkhead_name parameter to TrafficGate:
- allowed=True, bulkhead_acquired=True on successful bulkhead acquisition
- allowed=False, gate="Bulkhead" when the bulkhead is full
- automatic release when a later stage rejects after the bulkhead was acquired

Worker-pool-backed gate routing cases live in the private tree (the pool
implementation ships in the licensed tier).
"""

from __future__ import annotations

import pytest

from baldur.core.connection_health import ConnectionType
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.traffic_gate import (
    TrafficDecision,
    TrafficGate,
    reset_traffic_gate,
)
from baldur.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.settings.bulkhead import reset_bulkhead_settings


@pytest.fixture(autouse=True)
def _empty_provider_slot(monkeypatch):
    """Pin the resolution chain to its fallback leg for this module.

    The gate resolves the registry via the chain; forcing the provider slot
    empty keeps the compartments deterministic and lets
    reset_bulkhead_registry() fully isolate each test.
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
    reset_traffic_gate()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    reset_traffic_gate()


class TestTrafficGateBulkheadIntegration:
    """TrafficGate Bulkhead integration tests."""

    def test_should_allow_without_bulkhead(self):
        """Calling without bulkhead_name keeps the existing behavior."""
        gate = TrafficGate()

        decision = gate.should_allow(priority=0)

        assert decision.allowed is True
        assert decision.bulkhead_acquired is False
        assert decision.bulkhead_name is None

    def test_should_allow_with_bulkhead_success(self):
        """Calling with bulkhead_name acquires the bulkhead and allows."""
        gate = TrafficGate()

        decision = gate.should_allow(
            priority=0,
            bulkhead_name=ConnectionType.DATABASE.value,
        )

        assert decision.allowed is True
        assert decision.bulkhead_acquired is True
        assert decision.bulkhead_name == "database"

        # Release after acquisition
        gate.release_bulkhead("database")

    def test_should_reject_when_bulkhead_full(self):
        """Reject when the bulkhead is full."""
        gate = TrafficGate()
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        # Occupy every slot of the bulkhead
        max_concurrent = db_bulkhead.get_state().max_concurrent
        for _ in range(max_concurrent):
            db_bulkhead.try_acquire()

        decision = gate.should_allow(
            priority=0,
            bulkhead_name="database",
        )

        assert decision.allowed is False
        assert decision.gate == "Bulkhead"
        assert "database" in decision.reason
        assert decision.bulkhead_acquired is False

        # Cleanup
        for _ in range(max_concurrent):
            db_bulkhead.release()

    def test_release_bulkhead_method(self):
        """Verify release_bulkhead method behavior."""
        gate = TrafficGate()
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        initial_active = db_bulkhead.get_state().active_count

        decision = gate.should_allow(
            priority=0,
            bulkhead_name="database",
        )

        assert decision.bulkhead_acquired is True
        assert db_bulkhead.get_state().active_count == initial_active + 1

        gate.release_bulkhead("database")

        assert db_bulkhead.get_state().active_count == initial_active

    def test_unknown_bulkhead_skipped(self):
        """An unregistered bulkhead is skipped and processing continues."""
        gate = TrafficGate()

        decision = gate.should_allow(
            priority=0,
            bulkhead_name="unknown_bulkhead",
        )

        assert decision.allowed is True
        assert decision.bulkhead_acquired is False

    def test_traffic_decision_has_bulkhead_fields(self):
        """TrafficDecision carries the bulkhead-related fields."""
        decision = TrafficDecision(
            allowed=True,
            reason="test",
            level=BackpressureLevel.NONE,
            gate="test",
        )

        assert hasattr(decision, "bulkhead_acquired")
        assert hasattr(decision, "bulkhead_name")
        assert decision.bulkhead_acquired is False
        assert decision.bulkhead_name is None


class TestTrafficGateBulkheadWithLoadShedding:
    """TrafficGate Bulkhead + LoadShedding combination tests."""

    def test_bulkhead_acquired_then_load_shedding_rejects(self):
        """
        When LoadShedding rejects after the bulkhead was acquired, the bulkhead is
        released automatically.
        """

        # MockLoadShedding that always rejects
        class MockLoadShedding:
            def should_accept(self, **kwargs):
                return {"accepted": False}

        gate = TrafficGate(load_shedding=MockLoadShedding())
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        initial_active = db_bulkhead.get_state().active_count

        decision = gate.should_allow(
            priority=5,
            bulkhead_name="database",
        )

        # Rejected by LoadShedding
        assert decision.allowed is False
        assert decision.gate == "CascadeLoadShedding"

        # The bulkhead must be released automatically
        assert db_bulkhead.get_state().active_count == initial_active
