"""
Event Bus & ErrorBudgetGate Integration Tests.

Tests for:
1. BaldurEventBus - event publish/subscribe system
2. EmergencyManager event emission
3. ErrorBudgetGate event emission
4. RetryHandler ErrorBudgetGate check
5. Conditional Replay ErrorBudgetGate check
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

# =============================================================================
# Event Bus Tests
# =============================================================================


class TestBaldurEventBus:
    """BaldurEventBus tests."""

    def setup_method(self):
        """Reset the event bus before each test."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """Reset the event bus after each test."""
        self.bus.reset()

    def test_event_bus_singleton(self):
        """Verify the event bus behaves as a singleton."""
        from baldur.services.event_bus import get_event_bus

        bus1 = get_event_bus()
        bus2 = get_event_bus()

        assert bus1 is bus2

    def test_subscribe_and_publish(self):
        """Event subscribe and publish test."""
        from baldur.services.event_bus import (
            BaldurEvent,
            EventType,
        )

        received_events: list[BaldurEvent] = []

        def handler(event: BaldurEvent):
            received_events.append(event)

        # Subscribe
        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        # Publish
        event = BaldurEvent(
            event_type=EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": 3, "previous_level": 0},
            source="test",
        )
        handlers_called = self.bus.publish(event)

        assert handlers_called == 1
        assert len(received_events) == 1
        assert received_events[0].data["level"] == 3

    def test_emit_convenience_method(self):
        """emit convenience method test."""
        from baldur.services.event_bus import EventType

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler)

        # Use the emit method
        handlers_called = self.bus.emit(
            event_type=EventType.ERROR_BUDGET_CRITICAL,
            data={"budget_percent": 15.0, "threshold": 20.0},
            source="test",
        )

        assert handlers_called == 1
        assert received_events[0].data["budget_percent"] == 15.0

    def test_unsubscribe(self):
        """Unsubscribe test."""
        from baldur.services.event_bus import EventType

        call_count = 0

        def handler(event):
            nonlocal call_count
            call_count += 1

        # subscribe → publish → unsubscribe → publish
        self.bus.subscribe(EventType.CIRCUIT_BREAKER_CLOSED, handler)
        self.bus.emit(EventType.CIRCUIT_BREAKER_CLOSED, {}, "test")
        assert call_count == 1

        self.bus.unsubscribe(EventType.CIRCUIT_BREAKER_CLOSED, handler)
        self.bus.emit(EventType.CIRCUIT_BREAKER_CLOSED, {}, "test")
        assert call_count == 1  # no longer called

    def test_multiple_handlers_priority(self):
        """Multiple handler priority test."""
        from baldur.services.event_bus import EventPriority, EventType

        call_order = []

        def low_handler(event):
            call_order.append("low")

        def high_handler(event):
            call_order.append("high")

        def critical_handler(event):
            call_order.append("critical")

        # Registered in a different order but executed in priority order
        self.bus.subscribe(
            EventType.EMERGENCY_ACTIVATED, low_handler, EventPriority.LOW
        )
        self.bus.subscribe(
            EventType.EMERGENCY_ACTIVATED, critical_handler, EventPriority.CRITICAL
        )
        self.bus.subscribe(
            EventType.EMERGENCY_ACTIVATED, high_handler, EventPriority.HIGH
        )

        self.bus.emit(EventType.EMERGENCY_ACTIVATED, {}, "test")

        # Executed in CRITICAL → HIGH → LOW order
        assert call_order == ["critical", "high", "low"]

    def test_handler_exception_isolation(self):
        """Test that a handler exception does not affect other handlers."""
        from baldur.services.event_bus import EventType

        call_count = 0

        def failing_handler(event):
            raise Exception("Handler failed!")

        def success_handler(event):
            nonlocal call_count
            call_count += 1

        self.bus.subscribe(EventType.CONFIG_UPDATED, failing_handler)
        self.bus.subscribe(EventType.CONFIG_UPDATED, success_handler)

        # Other handlers still run even when an exception is raised
        handlers_called = self.bus.emit(EventType.CONFIG_UPDATED, {}, "test")

        assert handlers_called == 1  # failing_handler fails
        assert call_count == 1  # success_handler succeeds

    def test_event_history(self):
        """Event history recording test."""
        from baldur.services.event_bus import EventType

        self.bus.emit(EventType.EMERGENCY_LEVEL_CHANGED, {"level": 1}, "test1")
        self.bus.emit(EventType.ERROR_BUDGET_CRITICAL, {"budget": 10}, "test2")
        self.bus.emit(EventType.CIRCUIT_BREAKER_OPENED, {"service": "api"}, "test3")

        history = self.bus.get_history()
        assert len(history) == 3

        # Filter by a specific event type only
        filtered = self.bus.get_history(EventType.EMERGENCY_LEVEL_CHANGED)
        assert len(filtered) == 1
        assert filtered[0]["data"]["level"] == 1

    def test_disable_and_enable(self):
        """Event bus disable/enable test."""
        from baldur.services.event_bus import EventType

        call_count = 0

        def handler(event):
            nonlocal call_count
            call_count += 1

        self.bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, handler)

        # Events are ignored while disabled
        self.bus.disable()
        assert not self.bus.is_enabled()
        self.bus.emit(EventType.KILL_SWITCH_ACTIVATED, {}, "test")
        assert call_count == 0

        # Works normally after re-enabling
        self.bus.enable()
        assert self.bus.is_enabled()
        self.bus.emit(EventType.KILL_SWITCH_ACTIVATED, {}, "test")
        assert call_count == 1

    def test_stats(self):
        """Event bus statistics test."""
        from baldur.services.event_bus import EventType

        def handler(event):
            pass

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)
        self.bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler)
        self.bus.emit(EventType.EMERGENCY_LEVEL_CHANGED, {}, "test")

        stats = self.bus.get_stats()

        assert stats["enabled"] is True
        assert stats["subscriptions_count"] == 2
        assert stats["event_types_with_subscribers"] == 2
        assert stats["history_count"] == 1

    def test_thread_safety(self):
        """Thread safety test."""
        from baldur.services.event_bus import EventType

        call_count = 0
        lock = threading.Lock()

        def handler(event):
            nonlocal call_count
            with lock:
                call_count += 1

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        # Publish concurrently from multiple threads
        threads = []
        for _ in range(10):
            t = threading.Thread(
                target=lambda: self.bus.emit(
                    EventType.EMERGENCY_LEVEL_CHANGED, {}, "test"
                )
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        assert call_count == 10


# =============================================================================
# EmergencyManager Event Emission Tests
# =============================================================================


class TestEmergencyManagerEventEmission:
    """EmergencyManager event emission tests."""

    def setup_method(self):
        """Set up before each test."""
        # EmergencyManager lives in baldur_pro (PRO-tier emergency mode).
        pytest.importorskip("baldur_pro")
        from baldur.services.event_bus import get_event_bus
        from baldur_pro.services.emergency_mode import get_emergency_manager

        self.bus = get_event_bus()
        self.bus.reset()

        self.manager = get_emergency_manager()
        # conftest auto_reset_audit_singletons may replace the bus singleton;
        # clear cached reference so _get_event_bus() re-fetches the current one.
        self.manager._event_bus = None
        self.manager.reset()

    def teardown_method(self):
        """Clean up after each test."""
        self.bus.reset()
        self.manager.reset()

    def test_activate_manual_emits_event(self):
        """Verify an event is emitted on manual activation."""
        from baldur.services.event_bus import EventType
        from baldur_pro.services.emergency_mode import EmergencyLevel

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        # Activate emergency mode
        self.manager.activate_manual(
            level=EmergencyLevel.LEVEL_2,
            reason="Test activation",
            activated_by="test_user",
        )

        assert len(received_events) == 1
        event = received_events[0]
        assert event.data["level"] == "level_2"
        assert event.data["previous_level"] == "normal"
        assert event.data["is_escalation"] is True
        assert event.data["reason"] == "Test activation"

    def test_deactivate_emits_event(self):
        """Verify an event is emitted on deactivation."""
        from baldur.services.event_bus import EventType
        from baldur_pro.services.emergency_mode import EmergencyLevel

        received_events = []

        def handler(event):
            received_events.append(event)

        # Activate first
        self.manager.activate_manual(
            level=EmergencyLevel.LEVEL_2,
            reason="Setup",
            activated_by="test",
        )

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        # Deactivate
        self.manager.deactivate(deactivated_by="test", force=True)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.data["level"] == "normal"
        assert event.data["previous_level"] == "level_2"
        assert event.data["is_escalation"] is False

    def test_activate_auto_emits_event(self):
        """Verify an event is emitted on automatic activation."""
        from baldur.services.event_bus import EventType
        from baldur_pro.services.emergency_mode import EmergencyLevel

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        # Automatic activation
        self.manager.activate_auto(
            level=EmergencyLevel.LEVEL_1,
            reason="High error rate detected",
            duration_minutes=30,
        )

        assert len(received_events) == 1
        assert received_events[0].data["level"] == "level_1"


# =============================================================================
# ErrorBudgetGate Event Emission Tests
# =============================================================================


class TestErrorBudgetGateEventEmission:
    """ErrorBudgetGate event emission tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # ErrorBudgetGate lives in baldur_pro (PRO-tier governance).
        pytest.importorskip("baldur_pro")

    def setup_method(self):
        """Set up before each test."""
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        """Clean up after each test."""
        self.bus.reset()

    def test_critical_budget_emits_event(self):
        """Verify an event is emitted when the error budget reaches the critical threshold."""
        from baldur.services.event_bus import EventType
        from baldur_pro.services.error_budget_gate import (
            ErrorBudgetGate,
            ErrorBudgetGateConfig,
        )

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler)

        # Gate setup - low threshold
        config = ErrorBudgetGateConfig(
            enabled=True,
            critical_threshold_percent=20.0,
            warning_threshold_percent=40.0,
        )
        gate = ErrorBudgetGate(config=config)

        # Call _evaluate directly (budget < critical)
        result = gate._evaluate(budget_percent=15.0)

        assert result.allowed is False
        assert len(received_events) == 1
        assert received_events[0].data["budget_percent"] == 15.0
        assert received_events[0].data["status"] == "critical"

    def test_warning_budget_emits_event(self):
        """Verify an event is emitted on an error budget warning."""
        from baldur.services.event_bus import EventType
        from baldur_pro.services.error_budget_gate import (
            ErrorBudgetGate,
            ErrorBudgetGateConfig,
        )

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.ERROR_BUDGET_WARNING, handler)

        config = ErrorBudgetGateConfig(
            enabled=True,
            critical_threshold_percent=20.0,
            warning_threshold_percent=40.0,
        )
        gate = ErrorBudgetGate(config=config)

        # Call _evaluate (critical < budget < warning)
        result = gate._evaluate(budget_percent=30.0)

        assert result.allowed is True
        assert len(received_events) == 1
        assert received_events[0].data["status"] == "warning"


# =============================================================================
# Conditional Replay ErrorBudgetGate Integration Tests
# =============================================================================
# TestRetryHandlerErrorBudgetGate removed: RetryHandler deprecated and deleted.
# ErrorBudgetGate integration is now tested via RetryPolicy + PolicyComposer.


@pytest.mark.governance
class TestConditionalReplayErrorBudgetGate:
    """Conditional Replay ErrorBudgetGate check tests."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        # Patches baldur_pro.services.governance.checks (PRO-tier governance).
        pytest.importorskip("baldur_pro")

    def test_conditional_replay_blocked_when_budget_low(self):
        """Conditional Replay is blocked when the error budget is insufficient."""
        from unittest.mock import MagicMock

        from baldur.models.governance import BlockReason, GovernanceCheckResult
        from baldur.services.replay_service.service import ReplayService

        mock_gov_result = GovernanceCheckResult(
            allowed=False,
            block_reason=BlockReason.ERROR_BUDGET,
            block_message="Error budget exhausted",
            error_budget_percent=10.0,
        )

        service = ReplayService.__new__(ReplayService)
        service.config = {"max_replay_attempts": 3}
        service._event_emitter_bus = None
        service._governance = None
        service._governance_resolved = False

        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                return_value=mock_gov_result,
            ),
            patch.object(
                ReplayService,
                "_load_failure_type_map",
                return_value={"test_service": ["TIMEOUT"]},
            ),
            patch.object(
                ReplayService,
                "repository",
                new_callable=lambda: property(lambda self: MagicMock()),
            ),
        ):
            result = service.replay_on_circuit_close(
                service_name="test_service",
                max_items=10,
            )

            assert result.governance_blocked is True
            assert "Error budget exhausted" in result.governance_block_reason


# =============================================================================
# Convenience Functions Tests
# =============================================================================


class TestEventBusConvenienceFunctions:
    """Event bus convenience function tests."""

    def setup_method(self):
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        self.bus.reset()

    def test_emit_emergency_level_changed(self):
        """emit_emergency_level_changed convenience function test."""
        from baldur.services.event_bus import (
            EventType,
            emit_emergency_level_changed,
        )

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.EMERGENCY_LEVEL_CHANGED, handler)

        emit_emergency_level_changed(
            level=2,
            previous_level=0,
            reason="Test",
        )

        assert len(received_events) == 1
        assert received_events[0].data["level"] == 2
        assert received_events[0].data["is_escalation"] is True

    def test_emit_error_budget_critical(self):
        """emit_error_budget_critical convenience function test."""
        from baldur.services.event_bus import (
            EventType,
            emit_error_budget_critical,
        )

        received_events = []

        def handler(event):
            received_events.append(event)

        self.bus.subscribe(EventType.ERROR_BUDGET_CRITICAL, handler)

        emit_error_budget_critical(
            budget_percent=10.0,
            threshold=20.0,
        )

        assert len(received_events) == 1
        assert received_events[0].data["budget_percent"] == 10.0

    def test_emit_circuit_breaker_state_changed(self):
        """emit_circuit_breaker_state_changed convenience function test."""
        from baldur.services.event_bus import (
            EventType,
            emit_circuit_breaker_state_changed,
        )

        received_closed = []
        received_opened = []

        def closed_handler(event):
            received_closed.append(event)

        def opened_handler(event):
            received_opened.append(event)

        self.bus.subscribe(EventType.CIRCUIT_BREAKER_CLOSED, closed_handler)
        self.bus.subscribe(EventType.CIRCUIT_BREAKER_OPENED, opened_handler)

        # CLOSED event
        emit_circuit_breaker_state_changed(
            service_name="payment",
            new_state="CLOSED",
            previous_state="OPEN",
        )

        assert len(received_closed) == 1
        assert received_closed[0].data["service_name"] == "payment"

        # OPEN event
        emit_circuit_breaker_state_changed(
            service_name="inventory",
            new_state="OPEN",
            previous_state="CLOSED",
        )

        assert len(received_opened) == 1
        assert received_opened[0].data["service_name"] == "inventory"


# =============================================================================
# Default Handlers Registration Tests
# =============================================================================


class TestDefaultHandlersRegistration:
    """Default handler registration test."""

    def setup_method(self):
        from baldur.services.event_bus import get_event_bus

        self.bus = get_event_bus()
        self.bus.reset()

    def teardown_method(self):
        self.bus.reset()

    def test_register_default_handlers(self):
        """Default handler registration test."""
        from baldur.services.event_bus import (
            register_default_handlers,
        )

        register_default_handlers()

        subscriptions = self.bus.get_subscriptions()

        # Verify the default handlers are registered
        handler_names = [s["handler_name"] for s in subscriptions]

        assert "_on_emergency_level_changed" in handler_names
        assert "_on_error_budget_critical" in handler_names
        assert "_on_circuit_breaker_closed" in handler_names

    def test_default_handlers_not_duplicated(self):
        """Default handler duplicate registration prevention test."""
        from baldur.services.event_bus import register_default_handlers

        register_default_handlers()
        count1 = len(self.bus.get_subscriptions())

        register_default_handlers()
        count2 = len(self.bus.get_subscriptions())

        assert count1 == count2  # not registered twice


# =============================================================================
# Refresh interval of the emergency copy
# =============================================================================


class TestEmergencyManagerRefreshIntervalContract:
    """Every process re-reads the level on the emergency interval (default 30 s)."""

    def setup_method(self):
        # GracefulDegradationManager lives in baldur_pro (PRO-tier emergency mode).
        pytest.importorskip("baldur_pro")
        import baldur_pro.services.emergency_mode as em_mod
        from baldur_pro.services.emergency_mode import (
            GracefulDegradationManager,
        )

        # Singleton reset (dual singleton: module-level + class-level)
        self._prev_emergency_manager = em_mod._emergency_manager
        GracefulDegradationManager._instance = None
        em_mod._emergency_manager = None
        self.manager = GracefulDegradationManager()

    def teardown_method(self):
        import baldur_pro.services.emergency_mode as em_mod
        from baldur_pro.services.emergency_mode import GracefulDegradationManager

        self.manager.close()
        GracefulDegradationManager._instance = None
        em_mod._emergency_manager = self._prev_emergency_manager

    def test_refresh_interval_default_value(self):
        """The refresh interval defaults to 30 seconds."""
        assert self.manager._cache_ttl_seconds == 30
        assert self.manager._refresh_interval() == 30.0
