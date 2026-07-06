"""
DegradedModeManager degradation broadcast integration tests (307).

Tests that DegradedModeManager.enter/exit_degraded_mode correctly
invokes DegradationBroadcaster.notify with the expected arguments.
"""

from unittest.mock import patch

from baldur.audit.resilience.circuit_breaker import CircuitBreakerRegistry
from baldur.audit.resilience.degraded_mode import DegradedModeManager
from baldur.audit.resilience.metrics import AuditMetrics
from baldur.audit.resilience.syslog_fallback import SyslogFallback


class TestDegradedModeBroadcastBehavior:
    """DegradedModeManager broadcasts state changes via DegradationBroadcaster."""

    def setup_method(self):
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

    def teardown_method(self):
        DegradedModeManager._instance = None
        CircuitBreakerRegistry._instance = None
        AuditMetrics._instance = None
        SyslogFallback._instance = None

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_enter_degraded_mode_broadcasts_degraded(self, mock_broadcaster):
        """enter_degraded_mode calls DegradationBroadcaster.notify with is_degraded=True."""
        manager = DegradedModeManager.get_instance()
        manager.enter_degraded_mode("backend failure")

        mock_broadcaster.notify.assert_called_once_with(
            "external_backends",
            True,
            None,
            "backend failure",
        )

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_exit_degraded_mode_broadcasts_recovered(self, mock_broadcaster):
        """exit_degraded_mode calls DegradationBroadcaster.notify with is_degraded=False."""
        manager = DegradedModeManager.get_instance()
        manager.enter_degraded_mode("test")
        mock_broadcaster.reset_mock()

        manager.exit_degraded_mode()

        mock_broadcaster.notify.assert_called_once_with(
            "external_backends",
            False,
            None,
            "recovered",
        )

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_enter_when_already_degraded_does_not_broadcast_again(
        self, mock_broadcaster
    ):
        """Second enter_degraded_mode while already degraded does not re-broadcast."""
        manager = DegradedModeManager.get_instance()
        manager.enter_degraded_mode("first reason")
        mock_broadcaster.reset_mock()

        manager.enter_degraded_mode("second reason")
        mock_broadcaster.notify.assert_not_called()

    @patch(
        "baldur.audit.resilience.degradation_protocol.DegradationBroadcaster",
        autospec=True,
    )
    def test_exit_when_not_degraded_does_not_broadcast(self, mock_broadcaster):
        """exit_degraded_mode when not degraded does not broadcast."""
        manager = DegradedModeManager.get_instance()
        manager.exit_degraded_mode()
        mock_broadcaster.notify.assert_not_called()
