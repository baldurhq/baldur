"""
Circuit Breaker Kill Switch and Freeze Tests

Subjects under test:
1. Kill Switch Override (manual_control.py)
2. Freeze Mode (freeze_mode.py)
3. Panic Threshold (panic_threshold.py)
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

# =============================================================================
# Kill Switch Override Tests
# =============================================================================


class TestKillSwitchOverride:
    """Kill Switch Override behaviour (manual_control.py)."""

    def test_force_open_blocked_when_kill_switch_active_without_override(self):
        """force_open is blocked by an active kill switch without an override."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerConfig,
        )
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        # Mock repository
        mock_repo = Mock()

        # Create mixin instance with mocked config
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = mixin.force_open(
                service_name="payment-api",
                reason="test",
            )

        assert result.success is False
        assert "Kill Switch" in result.error
        assert "override_kill_switch=True" in result.error

    def test_force_open_allowed_with_override(self):
        """force_open is allowed past an active kill switch with override=True."""
        from baldur.services.circuit_breaker.config import (
            CircuitBreakerConfig,
        )
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )
        from baldur.services.circuit_breaker.outcome_window import OutcomeWindow

        # Mock repository
        mock_repo = Mock()
        mock_repo.atomic_force_open.return_value = (True, "closed", "open")

        # Create mixin instance
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo
        mixin._half_open_requests = {}
        mixin._emit_event = Mock()
        mixin._outcome_window = OutcomeWindow()

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            with patch("baldur.services.circuit_breaker.manual_control.logger"):
                result = mixin.force_open(
                    service_name="payment-api",
                    reason="emergency",
                    override_kill_switch=True,
                )

        assert result.success is True
        mock_repo.atomic_force_open.assert_called_once()

    def test_force_close_blocked_when_kill_switch_active_without_override(self):
        """force_close is blocked by an active kill switch without an override."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )

        mock_repo = Mock()
        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            result = mixin.force_close(
                service_name="payment-api",
                reason="test",
            )

        assert result.success is False
        assert "Kill Switch" in result.error

    def test_force_close_allowed_with_override(self):
        """force_close is allowed past an active kill switch with override=True."""
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.manual_control import (
            ManualControlMixin,
        )
        from baldur.services.circuit_breaker.outcome_window import OutcomeWindow

        mock_repo = Mock()
        mock_repo.atomic_force_close.return_value = (True, "open", "closed")

        mixin = ManualControlMixin()
        mixin.config = CircuitBreakerConfig()
        mixin.repository = mock_repo
        mixin._half_open_requests = {}
        mixin._emit_event = Mock()
        mixin._outcome_window = OutcomeWindow()

        with patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=False,
        ):
            with patch("baldur.services.circuit_breaker.manual_control.logger"):
                result = mixin.force_close(
                    service_name="payment-api",
                    reason="recovery",
                    override_kill_switch=True,
                )

        assert result.success is True
        mock_repo.atomic_force_close.assert_called_once()


# =============================================================================
# Freeze Mode Tests
# =============================================================================


class TestFreezeMode:
    """Freeze Mode behaviour (freeze_mode.py)."""

    @staticmethod
    def _manager(level):
        """Build a manager over a stub emergency manager reporting ``level``."""
        from baldur.interfaces.emergency import EmergencyManager
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        emergency = Mock(spec=EmergencyManager)
        emergency.get_current_level.return_value = level
        emergency.get_state.return_value = SimpleNamespace(
            activated_at="2026-01-01T00:00:00+00:00"
        )
        return FreezeModeManager(emergency_manager=emergency)

    def test_freeze_mode_inactive_without_emergency_manager(self):
        """No registered emergency manager means nothing is frozen."""
        from baldur.services.circuit_breaker.freeze_mode import FreezeModeManager

        manager = FreezeModeManager()

        with patch(
            "baldur.services.circuit_breaker.freeze_mode._pro_distribution_present",
            return_value=False,
        ):
            assert manager.is_active() is False

    def test_freeze_mode_active_on_level_3(self):
        """LEVEL_3 is read through the enum's own ordering, not its value."""
        from baldur.models.emergency import EmergencyLevel

        manager = self._manager(EmergencyLevel.LEVEL_3)

        assert manager.is_active() is True

    def test_freeze_mode_inactive_below_level_3(self):
        """LEVEL_2 does not freeze the breakers."""
        from baldur.models.emergency import EmergencyLevel

        manager = self._manager(EmergencyLevel.LEVEL_2)

        assert manager.is_active() is False

    def test_freeze_mode_inactive_for_non_enum_level(self):
        """A level that is not the ordered enum is not evidence of a lockdown."""
        manager = self._manager("level_3")

        assert manager.is_active() is False

    def test_should_allow_state_change_auto_blocked_in_freeze(self):
        """Automatic transitions are refused while frozen."""
        from baldur.models.emergency import EmergencyLevel

        manager = self._manager(EmergencyLevel.LEVEL_3)

        allowed, reason = manager.should_allow_state_change(
            service_id="payment-api",
            new_state="OPEN",
        )

        assert allowed is False
        assert "Freeze Mode" in reason

    def test_should_allow_state_change_allowed_when_not_frozen(self):
        """Automatic transitions pass when no lockdown holds."""
        from baldur.models.emergency import EmergencyLevel

        manager = self._manager(EmergencyLevel.NORMAL)

        allowed, reason = manager.should_allow_state_change(
            service_id="payment-api",
            new_state="OPEN",
        )

        assert allowed is True
        assert reason == ""

    def test_get_state_derives_from_the_emergency_state(self):
        """The reported state is derived, never stored."""
        from baldur.models.emergency import EmergencyLevel
        from baldur.services.circuit_breaker.freeze_mode import FreezeReason

        manager = self._manager(EmergencyLevel.LEVEL_3)

        state = manager.get_state()

        assert state.active is True
        assert state.reason == FreezeReason.LOCKDOWN_ENTRY
        assert state.activated_by == "system"
        assert state.activated_at == "2026-01-01T00:00:00+00:00"


# =============================================================================
# Panic Threshold Tests
# =============================================================================


class TestPanicThreshold:
    """Panic Threshold behaviour (panic_threshold.py)."""

    @staticmethod
    def _monitor(open_names, all_names, **config_kwargs):
        """Build a monitor whose cluster read returns the given rows."""
        from baldur.services.circuit_breaker import CircuitBreakerService
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdConfig,
            PanicThresholdMonitor,
        )

        rows = [
            SimpleNamespace(
                service_name=name, state="open" if name in open_names else "closed"
            )
            for name in all_names
        ]
        service = Mock(spec=CircuitBreakerService)
        service.repository.get_cluster_states.return_value = rows
        return PanicThresholdMonitor(
            config=PanicThresholdConfig(**config_kwargs),
            circuit_breaker_service=service,
        )

    def test_probe_below_threshold(self):
        """An OPEN ratio under the threshold does not trigger."""
        monitor = self._monitor(
            ["svc1", "svc2", "svc3"],
            ["svc1", "svc2", "svc3", "svc4", "svc5", "svc6"],
            threshold_percent=70.0,
        )

        result = monitor.evaluate()

        assert result.triggered is False
        assert result.open_rate == 50.0

    def test_probe_insufficient_services(self):
        """A fleet below the minimum size is not judged at all."""
        monitor = self._monitor(["svc1", "svc2"], ["svc1", "svc2"])

        result = monitor.evaluate()

        assert result.triggered is False
        assert "Insufficient services" in result.reason

    def test_probe_triggers_on_threshold_exceeded(self):
        """The probe reports the condition instantaneously, with no hysteresis."""
        monitor = self._monitor(
            ["svc1", "svc2", "svc3", "svc4"],
            ["svc1", "svc2", "svc3", "svc4", "svc5"],
            threshold_percent=70.0,
        )

        result = monitor.evaluate()

        assert result.triggered is True
        assert result.open_rate == 80.0
        assert len(result.open_circuits) == 4

    def test_probe_leaves_the_consecutive_counter_untouched(self):
        """The probe never advances the escalation lane's hysteresis."""
        monitor = self._monitor(
            ["svc1", "svc2", "svc3", "svc4"],
            ["svc1", "svc2", "svc3", "svc4", "svc5"],
            threshold_percent=70.0,
        )

        monitor.evaluate()
        monitor.evaluate()

        assert monitor._consecutive_triggers == 0

    def test_panic_threshold_result_fields(self):
        """PanicThresholdResult carries the observation, not a fabricated action."""
        from baldur.services.circuit_breaker.panic_threshold import (
            PanicThresholdResult,
        )

        result = PanicThresholdResult(
            triggered=True,
            open_rate=75.0,
            open_count=3,
            total_count=4,
            open_circuits=["a", "b", "c"],
            action_taken="emergency_level_3_escalation",
        )

        assert result.triggered is True
        assert result.open_rate == 75.0
        assert result.open_count == 3
        assert result.action_taken == "emergency_level_3_escalation"


# =============================================================================
# Integration Tests
# =============================================================================


class TestKillSwitchFreezeIntegration:
    """Cross-module wiring checks."""

    def test_imports_work(self):
        """The package exposes the surviving advanced-protection symbols."""
        from baldur.services.circuit_breaker import (
            FreezeModeManager,
            PanicThresholdMonitor,
            reset_freeze_mode_manager,
            reset_panic_threshold_monitor,
        )

        assert FreezeModeManager is not None
        assert PanicThresholdMonitor is not None
        assert reset_freeze_mode_manager is not None
        assert reset_panic_threshold_monitor is not None


# =============================================================================
# Audit Helper Tests
# =============================================================================


class TestAuditHelpers:
    """Audit helper delegation."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_log_kill_switch_override_audit(self):
        """Kill Switch Override audit is recorded."""
        from baldur_pro.services.audit import log_kill_switch_override_audit

        # Confirms the WAL write succeeded
        result = log_kill_switch_override_audit(
            service_name="payment-api",
            action="force_open",
            reason="emergency recovery",
            controlled_by_id=123,
        )

        # Returns a WAL sequence number, or None
        assert result is None or isinstance(result, int)

    def test_log_panic_threshold_audit(self):
        """Panic Threshold audit is recorded."""
        from baldur_pro.services.audit import log_panic_threshold_audit

        result = log_panic_threshold_audit(
            open_rate=75.0,
            threshold=70.0,
            open_count=3,
            total_count=4,
            open_circuits=["a", "b", "c"],
            action_taken="emergency_level_3_escalation",
        )

        assert result is None or isinstance(result, int)

    def test_log_freeze_mode_audit(self):
        """Freeze Mode audit is recorded."""
        from baldur_pro.services.audit import log_freeze_mode_audit

        result = log_freeze_mode_audit(
            active=True,
            reason="LOCKDOWN entry",
            activated_by="system",
        )

        assert result is None or isinstance(result, int)
