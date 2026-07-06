"""
TrafficGate Bulkhead integration tests.

Verify the behavior after adding the bulkhead_name parameter to TrafficGate:
- allowed=True, bulkhead_acquired=True on successful bulkhead acquisition
- allowed=False, gate="Bulkhead" when the bulkhead is full
- automatic release when a later stage rejects after the bulkhead was acquired
- real ThreadPool-backed domains are routed and isolated at the gate (no
  silently-swallowed TypeError), and gate rejections are visible in stats
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import pytest
from structlog.testing import capture_logs

from baldur.core.connection_health import ConnectionType
from baldur.scaling.config import BackpressureLevel
from baldur.scaling.traffic_gate import (
    TrafficDecision,
    TrafficGate,
    reset_traffic_gate,
)
from baldur.settings.bulkhead import reset_bulkhead_settings
from baldur_pro.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur_pro.services.bulkhead.threadpool import ThreadPoolBulkhead


@pytest.fixture(autouse=True)
def reset_singletons():
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


class TestTrafficGateThreadPoolBulkhead:
    """TrafficGate routing of real ThreadPool-backed domains (G1 fix, D5-1, D6).

    Before 616, the gate passed ``timeout=`` to ``try_acquire`` on every
    bulkhead, but only Semaphore bulkheads accepted it — a ThreadPool domain
    raised a TypeError that the fail-open ``except`` swallowed, silently skipping
    isolation. These tests route a genuine ThreadPoolBulkhead through the gate.
    """

    def test_external_api_threadpool_routed_through_gate_acquires(self):
        """The built-in external_api (ThreadPool) domain acquires and releases at
        the gate without emitting traffic_gate.bulkhead_failed — isolation is
        enforced, not silently skipped."""
        # Given — the built-in external_api domain is a real ThreadPoolBulkhead
        gate = TrafficGate()
        registry = get_bulkhead_registry()
        bulkhead = registry.get(ConnectionType.EXTERNAL_API)
        assert isinstance(bulkhead, ThreadPoolBulkhead)
        initial_active = bulkhead.get_state().active_count

        # When — routed through the gate with a timeout
        with capture_logs() as logs:
            decision = gate.should_allow(
                priority=0,
                bulkhead_name="external_api",
                bulkhead_timeout=0.05,
            )

        # Then — acquired, and no fail-open WARNING was emitted
        assert decision.allowed is True
        assert decision.bulkhead_acquired is True
        assert decision.bulkhead_name == "external_api"
        assert not [e for e in logs if e.get("event") == "traffic_gate.bulkhead_failed"]
        assert bulkhead.get_state().active_count == initial_active + 1

        # Release works on the ThreadPool path
        gate.release_bulkhead("external_api")
        assert bulkhead.get_state().active_count == initial_active

    def test_saturated_threadpool_rejects_at_gate_and_records_stats(self):
        """A saturated ThreadPool compartment rejects at the gate with
        gate='Bulkhead' (isolation enforced) and the rejection is visible in
        stats (D6)."""
        # Given — a capacity-1 ThreadPool compartment (custom name avoids the
        # built-in-overwrite WARNING), already occupied
        gate = TrafficGate()
        registry = get_bulkhead_registry()
        bulkhead = ThreadPoolBulkhead("tp_gate_sat", max_workers=1, queue_size=0)
        registry.register(bulkhead)
        try:
            assert bulkhead.try_acquire() is True

            # When — a request routes through the saturated compartment
            decision = gate.should_allow(
                priority=0,
                bulkhead_name="tp_gate_sat",
                bulkhead_timeout=0.05,
            )

            # Then — rejected at the bulkhead gate, not silently skipped
            assert decision.allowed is False
            assert decision.gate == "Bulkhead"
            assert "tp_gate_sat" in decision.reason
            assert decision.bulkhead_acquired is False

            # And the gate rejection is recorded in stats (D6)
            state = bulkhead.get_state()
            assert state.rejected_count == 1
            assert state.last_rejection_time is not None
        finally:
            bulkhead.shutdown(wait=False)


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
