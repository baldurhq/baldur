"""
AutoTuningService Runtime Governance EventBus Integration Tests (S1-2b).

Test targets:
1. _subscribe_governance_events() — EventBus subscription, Fail-Open
2. _unsubscribe_governance_events() — lifecycle cleanup, idempotent stop
3. _governance_pause() / _governance_resume() — multi-reason set logic
4. _on_kill_switch_activated/deactivated — Kill Switch event handlers
5. _on_emergency_changed — Emergency event handler with threshold logic
6. resume() — clears governance pause reasons (Break Glass semantics)
7. Multi-reason state resolution — independent sources don't overwrite each other
"""

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur_pro.services.auto_tuning.service import AutoTuningService


@pytest.fixture
def mock_adapters():
    """Test adapter mocks with realistic metrics return values."""
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
def service(mock_adapters):
    """Create AutoTuningService with governance check bypassed and mock strategy."""
    with patch(
        "baldur_pro.services.governance.checks.check_all_governance",
        autospec=True,
    ):
        svc = AutoTuningService(**mock_adapters)

    # Replace strategy with a Mock so we can verify pause/resume calls
    svc._strategy = MagicMock()
    return svc


class TestSubscribeGovernanceEventsBehavior:
    """_subscribe_governance_events() EventBus subscription behavior."""

    def test_subscribe_sets_governance_subscribed_flag(self, service):
        """Successful subscription sets _governance_subscribed = True."""

        mock_bus = MagicMock()
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._subscribe_governance_events()

        assert service._governance_subscribed is True

    def test_subscribe_registers_all_three_event_types(self, service):
        """Subscription covers KILL_SWITCH_ACTIVATED, DEACTIVATED, EMERGENCY_LEVEL_CHANGED."""
        from baldur.services.event_bus import EventType

        mock_bus = MagicMock()
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._subscribe_governance_events()

        subscribed_types = {call.args[0] for call in mock_bus.subscribe.call_args_list}
        assert EventType.KILL_SWITCH_ACTIVATED in subscribed_types
        assert EventType.KILL_SWITCH_DEACTIVATED in subscribed_types
        assert EventType.EMERGENCY_LEVEL_CHANGED in subscribed_types

    def test_subscribe_fail_open_on_import_error(self, service):
        """EventBus unavailable (ImportError) → no crash, flag stays False."""
        with patch(
            "baldur.services.event_bus.get_event_bus",
            autospec=True,
            side_effect=ImportError("no event_bus"),
        ):
            service._subscribe_governance_events()

        assert service._governance_subscribed is False

    def test_subscribe_fail_open_on_runtime_exception(self, service):
        """EventBus runtime error → no crash, flag stays False."""
        mock_bus = MagicMock()
        mock_bus.subscribe.side_effect = RuntimeError("bus broken")
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._subscribe_governance_events()

        assert service._governance_subscribed is False


class TestUnsubscribeGovernanceEventsBehavior:
    """_unsubscribe_governance_events() lifecycle cleanup behavior."""

    def test_unsubscribe_clears_governance_subscribed_flag(self, service):
        """Successful unsubscription sets _governance_subscribed = False."""

        mock_bus = MagicMock()
        service._governance_subscribed = True
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._unsubscribe_governance_events()

        assert service._governance_subscribed is False

    def test_unsubscribe_calls_bus_unsubscribe_for_all_event_types(self, service):
        """Unsubscribe covers all 3 event types."""
        from baldur.services.event_bus import EventType

        mock_bus = MagicMock()
        service._governance_subscribed = True
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._unsubscribe_governance_events()

        unsubscribed_types = {
            call.args[0] for call in mock_bus.unsubscribe.call_args_list
        }
        assert EventType.KILL_SWITCH_ACTIVATED in unsubscribed_types
        assert EventType.KILL_SWITCH_DEACTIVATED in unsubscribed_types
        assert EventType.EMERGENCY_LEVEL_CHANGED in unsubscribed_types

    def test_unsubscribe_skips_when_not_subscribed(self, service):
        """Idempotent: stop() before start() doesn't call unsubscribe."""
        assert service._governance_subscribed is False

        mock_bus = MagicMock()
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service._unsubscribe_governance_events()

        mock_bus.unsubscribe.assert_not_called()

    def test_stop_calls_unsubscribe_before_strategy_stop(self, service):
        """stop() calls _unsubscribe_governance_events()."""
        service._governance_subscribed = True
        mock_bus = MagicMock()
        with patch(
            "baldur.services.event_bus.get_event_bus",
            return_value=mock_bus,
            autospec=True,
        ):
            service.stop()

        assert service._governance_subscribed is False


class TestGovernancePauseResumeBehavior:
    """Multi-reason _governance_pause/_governance_resume behavior (D4)."""

    def test_pause_adds_reason_and_calls_strategy_pause(self, service):
        """_governance_pause adds reason to set and pauses strategy."""
        service._governance_pause("kill_switch")

        assert "kill_switch" in service._pause_reasons
        service._strategy.pause.assert_called_once_with("kill_switch")

    def test_resume_removes_reason_and_resumes_when_empty(self, service):
        """_governance_resume removes reason; resumes only when set is empty."""
        # Given — single reason
        service._pause_reasons = {"kill_switch"}

        # When
        service._governance_resume("kill_switch")

        # Then
        assert "kill_switch" not in service._pause_reasons
        service._strategy.resume.assert_called_once()

    def test_resume_does_not_resume_when_other_reasons_remain(self, service):
        """_governance_resume doesn't resume if other reasons still present."""
        # Given — two reasons
        service._pause_reasons = {"kill_switch", "emergency"}

        # When — only remove one
        service._governance_resume("emergency")

        # Then — strategy NOT resumed (kill_switch still active)
        assert "kill_switch" in service._pause_reasons
        service._strategy.resume.assert_not_called()

    def test_resume_discard_nonexistent_reason_is_safe(self, service):
        """Discarding a reason that isn't in the set is a no-op."""
        service._pause_reasons = {"kill_switch"}

        service._governance_resume("nonexistent")

        assert service._pause_reasons == {"kill_switch"}
        service._strategy.resume.assert_not_called()

    def test_public_resume_clears_all_governance_pause_reasons(self, service):
        """Public resume() clears all _pause_reasons (Break Glass override)."""
        # Given — multiple governance reasons
        service._pause_reasons = {"kill_switch", "emergency"}

        # When
        service.resume()

        # Then
        assert len(service._pause_reasons) == 0
        service._strategy.resume.assert_called_once()


class TestKillSwitchEventHandlersBehavior:
    """Kill Switch event handler behavior."""

    def test_on_kill_switch_activated_pauses_with_reason(self, service):
        """KILL_SWITCH_ACTIVATED → _governance_pause('kill_switch')."""
        event = MagicMock()
        service._on_kill_switch_activated(event)

        assert "kill_switch" in service._pause_reasons
        service._strategy.pause.assert_called_once_with("kill_switch")

    def test_on_kill_switch_deactivated_resumes_when_only_reason(self, service):
        """KILL_SWITCH_DEACTIVATED → resume if no other reasons."""
        # Given — pause from kill switch
        service._pause_reasons = {"kill_switch"}

        # When
        event = MagicMock()
        service._on_kill_switch_deactivated(event)

        # Then
        assert "kill_switch" not in service._pause_reasons
        service._strategy.resume.assert_called_once()

    def test_on_kill_switch_deactivated_does_not_resume_when_emergency_active(
        self, service
    ):
        """KILL_SWITCH_DEACTIVATED → no resume if 'emergency' reason remains."""
        # Given — both reasons active
        service._pause_reasons = {"kill_switch", "emergency"}

        # When
        event = MagicMock()
        service._on_kill_switch_deactivated(event)

        # Then — kill_switch removed, but emergency holds
        assert "kill_switch" not in service._pause_reasons
        assert "emergency" in service._pause_reasons
        service._strategy.resume.assert_not_called()


class TestEmergencyEventHandlerBehavior:
    """Emergency Level changed event handler behavior."""

    def test_emergency_escalation_above_threshold_pauses(self, service):
        """Emergency escalation (level >= min_level, is_escalation) → pause."""
        from baldur.settings.governance import get_governance_settings

        get_governance_settings().emergency_min_level

        event = MagicMock()
        event.data = {
            "level": "level_2",
            "is_active": True,
            "is_escalation": True,
        }

        service._on_emergency_changed(event)

        assert "emergency" in service._pause_reasons
        service._strategy.pause.assert_called_once_with("emergency")

    def test_emergency_deactivation_resumes(self, service):
        """Emergency is_active=False → _governance_resume('emergency')."""
        # Given
        service._pause_reasons = {"emergency"}

        event = MagicMock()
        event.data = {
            "level": "normal",
            "is_active": False,
            "is_escalation": False,
        }

        # When
        service._on_emergency_changed(event)

        # Then
        assert "emergency" not in service._pause_reasons
        service._strategy.resume.assert_called_once()

    def test_emergency_escalation_below_threshold_does_not_pause(self, service):
        """Emergency level < min_level → no pause."""
        event = MagicMock()
        event.data = {
            "level": "level_1",
            "is_active": True,
            "is_escalation": True,
        }

        with patch(
            "baldur.settings.governance.get_governance_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value.emergency_min_level = 3

            service._on_emergency_changed(event)

        assert "emergency" not in service._pause_reasons
        service._strategy.pause.assert_not_called()

    def test_emergency_event_with_non_dict_data_is_ignored(self, service):
        """Event with non-dict data attribute → silently ignored."""
        event = MagicMock()
        event.data = "not-a-dict"

        service._on_emergency_changed(event)

        assert len(service._pause_reasons) == 0
        service._strategy.pause.assert_not_called()

    def test_emergency_event_with_invalid_level_is_ignored(self, service):
        """Event with unknown level string → silently ignored."""
        event = MagicMock()
        event.data = {
            "level": "invalid_level_99",
            "is_active": True,
            "is_escalation": True,
        }

        service._on_emergency_changed(event)

        assert len(service._pause_reasons) == 0
        service._strategy.pause.assert_not_called()

    def test_emergency_event_fail_open_on_import_error(self, service):
        """EmergencyLevel import fails → silently ignored."""
        event = MagicMock()
        event.data = {
            "level": "level_2",
            "is_active": True,
            "is_escalation": True,
        }

        with patch.dict(
            "sys.modules",
            {"baldur.models.emergency": None},
        ):
            service._on_emergency_changed(event)

        assert len(service._pause_reasons) == 0


class TestMultiReasonStateResolutionBehavior:
    """Multi-reason state resolution: independent sources don't overwrite each other."""

    def test_kill_switch_on_then_emergency_off_stays_paused(self, service):
        """Kill Switch ON → Emergency NORMAL → service still PAUSED."""
        # Given — Kill Switch pauses
        event_ks = MagicMock()
        service._on_kill_switch_activated(event_ks)
        assert "kill_switch" in service._pause_reasons
        service._strategy.pause.assert_called_with("kill_switch")

        # When �� Emergency deactivates
        event_em = MagicMock()
        event_em.data = {
            "level": "normal",
            "is_active": False,
            "is_escalation": False,
        }
        service._on_emergency_changed(event_em)

        # Then — still paused (kill_switch reason remains)
        assert "kill_switch" in service._pause_reasons
        assert service._pause_reasons == {"kill_switch"}
        service._strategy.resume.assert_not_called()

    def test_both_reasons_active_then_both_cleared_resumes(self, service):
        """Both Kill Switch + Emergency active → both cleared → resume."""
        # Given — both reasons
        service._pause_reasons = {"kill_switch", "emergency"}

        # When — remove kill_switch
        service._governance_resume("kill_switch")
        service._strategy.resume.assert_not_called()  # emergency still active

        # When — remove emergency
        service._governance_resume("emergency")

        # Then — now empty → resume called
        service._strategy.resume.assert_called_once()

    def test_public_resume_overrides_all_governance_reasons(self, service):
        """Public resume() (Break Glass) clears both reasons and resumes."""
        # Given
        service._pause_reasons = {"kill_switch", "emergency"}

        # When
        service.resume()

        # Then
        assert len(service._pause_reasons) == 0
        service._strategy.resume.assert_called_once()


class TestStartStopGovernanceLifecycleBehavior:
    """Governance subscription lifecycle in start()/stop()."""

    @pytest.fixture(autouse=True)
    def _enable_auto_tuning(self, monkeypatch):
        """Enable auto-tuning (impl 527 flipped default to False; v1.1 deferred)."""
        from baldur.settings.auto_tuning import reset_auto_tuning_settings

        monkeypatch.setenv("BALDUR_AUTO_TUNING_ENABLED", "true")
        reset_auto_tuning_settings()
        yield
        reset_auto_tuning_settings()

    def test_start_subscribes_to_governance_events(self, service):
        """start() calls _subscribe_governance_events()."""
        from baldur.models.governance import GovernanceCheckResult

        mock_bus = MagicMock()
        with (
            patch(
                "baldur_pro.services.governance.checks.check_all_governance",
                autospec=True,
                return_value=GovernanceCheckResult.allowed_result(),
            ),
            patch(
                "baldur.services.event_bus.get_event_bus",
                return_value=mock_bus,
                autospec=True,
            ),
        ):
            try:
                service.start()

                assert service._governance_subscribed is True
                assert mock_bus.subscribe.call_count == 3
            finally:
                service.stop()

    def test_stop_before_start_does_not_error(self, service):
        """Idempotent: stop() before start() runs without error."""
        assert service._governance_subscribed is False
        service.stop()  # Should not raise
        assert service._governance_subscribed is False
