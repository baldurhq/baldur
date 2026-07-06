"""
AutoTuningService + EventBus Governance Integration Test (S1-2b).

Verifies the end-to-end event flow:
EventBus.emit() → AutoTuningService handler → Strategy pause/resume

Test Categories:
    A. EventBus Governance Pipeline:
        - Kill Switch event pauses/resumes strategy via EventBus
        - Emergency escalation event pauses strategy via EventBus
        - Full lifecycle: KS ON → EM ON → EM OFF → KS OFF
        - stop() unsubscribes from EventBus

Note: All tests use in-memory EventBus - no infra dependency.
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.models.governance import GovernanceCheckResult
from baldur.services.event_bus import EventType
from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur_pro.services.auto_tuning.service import AutoTuningService


@pytest.fixture(autouse=True)
def _enable_auto_tuning(monkeypatch):
    # #527 R5 follow-up: AutoTuningSettings.enabled defaults to False
    # (v1.1-deferred). AutoTuningService.start() short-circuits when disabled,
    # leaving _pause_reasons empty and EventBus subscriptions inactive.
    from baldur.settings.auto_tuning import reset_auto_tuning_settings

    monkeypatch.setenv("BALDUR_AUTO_TUNING_ENABLED", "true")
    reset_auto_tuning_settings()
    yield
    reset_auto_tuning_settings()


@pytest.fixture
def mock_adapters():
    """Test adapter mocks."""
    metrics_adapter = MagicMock()
    metrics_adapter.fetch_current_metrics.return_value = {
        "error_rate": 0.02,
        "p99_latency_ms": 200.0,
        "throughput_rps": 1000.0,
    }
    return {
        "metrics_adapter": metrics_adapter,
        "config_provider": MagicMock(),
        "config_applier": MagicMock(),
        "audit_adapter": MagicMock(),
    }


@pytest.fixture
def event_bus():
    """Fresh in-memory EventBus instance."""
    return BaldurEventBus()


@pytest.fixture
def service_with_bus(mock_adapters, event_bus):
    """AutoTuningService started with real EventBus, mock strategy for assertions."""
    with (
        patch(
            "baldur_pro.services.governance.checks.check_all_governance",
            autospec=True,
            return_value=GovernanceCheckResult.allowed_result(),
        ),
        patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=event_bus,
            autospec=True,
        ),
    ):
        service = AutoTuningService(**mock_adapters)
        service.start()

    # Replace strategy with Mock for assertion tracking
    service._strategy = MagicMock()

    yield service, event_bus

    with patch(
        "baldur.services.event_bus.get_event_bus",
        return_value=event_bus,
        autospec=True,
    ):
        service.stop()


class TestEventBusGovernancePipelineBehavior:
    """End-to-end: EventBus event → AutoTuningService → Strategy state change."""

    def test_kill_switch_event_pauses_strategy_via_eventbus(self, service_with_bus):
        """EventBus KILL_SWITCH_ACTIVATED → strategy.pause() called."""
        service, bus = service_with_bus

        # When
        bus.emit(EventType.KILL_SWITCH_ACTIVATED, data={}, source="governance")

        # Then
        assert "kill_switch" in service._pause_reasons
        service._strategy.pause.assert_called_with("kill_switch")

    def test_kill_switch_deactivate_event_resumes_strategy(self, service_with_bus):
        """EventBus KILL_SWITCH_DEACTIVATED → strategy.resume() called."""
        service, bus = service_with_bus

        # Given — pause first
        service._pause_reasons.add("kill_switch")

        # When
        bus.emit(EventType.KILL_SWITCH_DEACTIVATED, data={}, source="governance")

        # Then
        assert "kill_switch" not in service._pause_reasons
        service._strategy.resume.assert_called()

    def test_emergency_escalation_event_pauses_via_eventbus(self, service_with_bus):
        """EventBus EMERGENCY_LEVEL_CHANGED (escalation) → strategy.pause()."""
        service, bus = service_with_bus

        # When
        bus.emit(
            EventType.EMERGENCY_LEVEL_CHANGED,
            data={
                "level": "level_2",
                "is_active": True,
                "is_escalation": True,
            },
            source="emergency_manager",
        )

        # Then
        assert "emergency" in service._pause_reasons
        service._strategy.pause.assert_called_with("emergency")

    def test_full_lifecycle_kill_switch_then_emergency_then_resume(
        self, service_with_bus
    ):
        """Full lifecycle: KS ON → EM ON → EM OFF → still paused → KS OFF → resumed."""
        service, bus = service_with_bus

        # Step 1: Kill Switch ON
        bus.emit(EventType.KILL_SWITCH_ACTIVATED, data={}, source="governance")
        assert service._pause_reasons == {"kill_switch"}

        # Step 2: Emergency ON (escalation)
        bus.emit(
            EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": "level_2", "is_active": True, "is_escalation": True},
            source="emergency_manager",
        )
        assert service._pause_reasons == {"kill_switch", "emergency"}

        # Step 3: Emergency OFF — kill_switch still holds
        bus.emit(
            EventType.EMERGENCY_LEVEL_CHANGED,
            data={"level": "normal", "is_active": False, "is_escalation": False},
            source="emergency_manager",
        )
        assert service._pause_reasons == {"kill_switch"}
        service._strategy.resume.assert_not_called()

        # Step 4: Kill Switch OFF — all clear, resume
        bus.emit(EventType.KILL_SWITCH_DEACTIVATED, data={}, source="governance")
        assert len(service._pause_reasons) == 0
        service._strategy.resume.assert_called_once()

    def test_stop_unsubscribes_from_eventbus(self, service_with_bus):
        """After stop(), EventBus events no longer affect the service."""
        service, bus = service_with_bus

        # Stop the service (triggers unsubscribe)
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=bus,
            autospec=True,
        ):
            service.stop()

        # Clear pause_reasons to have a clean baseline
        service._pause_reasons.clear()

        # Emit event after stop
        bus.emit(EventType.KILL_SWITCH_ACTIVATED, data={}, source="governance")

        # Handler should NOT be called — pause_reasons still empty
        assert len(service._pause_reasons) == 0
