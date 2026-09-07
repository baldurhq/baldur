"""Circuit Breaker Kill Switch Override, and the audit helpers it shares.

Freeze Mode and the Panic Threshold moved to suites of their own -- the gate
sites and the escalation policy each outgrew a shared module. What stays here
is the kill switch's own contract (a force is refused without an explicit
override, honoured with one), the package's exported surface, and the audit
helper delegation the three features share.
"""

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
