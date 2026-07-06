"""
LoggingNotificationAdapter / ConnectionHealthMonitor / PoolMonitor structlog
contract and behavior tests.

Verifies the OSS default notification adapter logs (never pushes) and maps a
payload priority onto the correct structlog level, plus the structlog event
shape of the connection-health and pool monitors.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.notification import LoggingNotificationAdapter
from baldur.models.notification import NotificationPayload, NotificationPriority

# ===========================================================================
# LoggingNotificationAdapter Contract Tests
# ===========================================================================


class TestLoggingNotificationAdapterContract:
    """LoggingNotificationAdapter structlog contract."""

    def test_adapter_uses_structlog_bound_logger(self):
        """adapter._logger must be a structlog BoundLogger."""
        adapter = LoggingNotificationAdapter()
        logger_type_name = type(adapter._logger).__name__
        assert "BoundLogger" in logger_type_name or "Proxy" in logger_type_name

    def test_priority_to_log_method_mapping_has_six_entries(self):
        """The priority-to-log-method mapping defines all six levels."""
        mapping = LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD
        assert len(mapping) == 6
        assert set(mapping.keys()) == {
            "CRITICAL",
            "HIGH",
            "WARNING",
            "MEDIUM",
            "LOW",
            "INFO",
        }

    def test_critical_maps_to_critical_method(self):
        assert (
            LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD["CRITICAL"] == "critical"
        )

    def test_high_maps_to_error_method(self):
        assert LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD["HIGH"] == "error"

    def test_medium_maps_to_warning_method(self):
        assert LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD["MEDIUM"] == "warning"

    def test_low_maps_to_info_method(self):
        assert LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD["LOW"] == "info"

    def test_info_priority_maps_to_debug_method(self):
        assert LoggingNotificationAdapter._PRIORITY_TO_LOG_METHOD["INFO"] == "debug"


# ===========================================================================
# LoggingNotificationAdapter Behavior Tests
# ===========================================================================


class TestLoggingNotificationAdapterBehavior:
    """LoggingNotificationAdapter logging behavior."""

    def _make_payload(self, priority: NotificationPriority) -> NotificationPayload:
        return NotificationPayload(
            title="Test Alert",
            message="Something happened",
            priority=priority,
            source="test_component",
        )

    def test_send_critical_notification_calls_critical_method(self):
        """A CRITICAL payload is recorded via logger.critical()."""
        mock_logger = MagicMock()
        adapter = LoggingNotificationAdapter()
        adapter._logger = mock_logger

        payload = self._make_payload(NotificationPriority.CRITICAL)
        result = adapter.send(payload)

        assert result is True
        mock_logger.critical.assert_called_once()
        call = mock_logger.critical.call_args
        assert call.args[0] == "notification.sent"

    def test_send_high_notification_calls_error_method(self):
        """A HIGH payload is recorded via logger.error()."""
        mock_logger = MagicMock()
        adapter = LoggingNotificationAdapter()
        adapter._logger = mock_logger

        payload = self._make_payload(NotificationPriority.HIGH)
        adapter.send(payload)

        mock_logger.error.assert_called_once()

    def test_send_medium_notification_calls_warning_method(self):
        """A MEDIUM payload is recorded via logger.warning()."""
        mock_logger = MagicMock()
        adapter = LoggingNotificationAdapter()
        adapter._logger = mock_logger

        payload = self._make_payload(NotificationPriority.MEDIUM)
        adapter.send(payload)

        mock_logger.warning.assert_called_once()

    def test_notification_event_name_is_notification_sent(self):
        """The structlog event name is 'notification.sent'."""
        mock_logger = MagicMock()
        adapter = LoggingNotificationAdapter()
        adapter._logger = mock_logger

        payload = self._make_payload(NotificationPriority.LOW)
        adapter.send(payload)

        mock_logger.info.assert_called_once()
        event_name = mock_logger.info.call_args.args[0]
        assert event_name == "notification.sent"

    def test_notification_kwargs_include_source_and_title(self):
        """The structlog call kwargs include source, title, and message."""
        mock_logger = MagicMock()
        adapter = LoggingNotificationAdapter()
        adapter._logger = mock_logger

        payload = self._make_payload(NotificationPriority.LOW)
        adapter.send(payload)

        call = mock_logger.info.call_args
        assert call.kwargs.get("source") == "test_component"
        assert call.kwargs.get("title") == "Test Alert"
        assert call.kwargs.get("message") == "Something happened"

    def test_send_returns_true(self):
        """send() returns True."""
        adapter = LoggingNotificationAdapter()
        # Mock to avoid actual stdout writes.
        with patch.object(adapter._logger, "debug"):
            payload = self._make_payload(NotificationPriority.INFO)
            result = adapter.send(payload)
        assert result is True


# ===========================================================================
# ConnectionHealthMonitor structlog Behavior Tests
# ===========================================================================


class TestConnectionHealthMonitorStructlogBehavior:
    """connection_health.py structlog behavior."""

    def test_simulation_override_uses_structlog_logger(self):
        """set_simulation_override uses the structlog logger."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()

        with patch("baldur.core.connection_health.logger") as mock_logger:
            monitor.set_simulation_override(
                ConnectionType.DATABASE,
                "primary",
                ConnectionStatus.UNHEALTHY,
            )
            mock_logger.info.assert_called_once()
            call = mock_logger.info.call_args
            assert call.args[0] == "connection_health.simulation_override_set"

    def test_clear_overrides_logs_cleared_event(self):
        """clear_all_simulation_overrides logs a cleared event."""
        from baldur.core.connection_health import DefaultConnectionHealthMonitor

        monitor = DefaultConnectionHealthMonitor()

        with patch("baldur.core.connection_health.logger") as mock_logger:
            monitor.clear_all_simulation_overrides()
            mock_logger.info.assert_called_once()
            call = mock_logger.info.call_args
            assert call.args[0] == "connection_health.simulation_overrides_cleared"


# ===========================================================================
# PoolMonitor structlog Behavior Tests
# ===========================================================================


class TestPoolMonitorStructlogBehavior:
    """pool_monitor.py structlog behavior."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_set_simulation_override_logs_set_event(self):
        """set_simulation_override logs a set event."""
        from baldur_pro.services.pool_monitor import (
            ConnectionPoolMonitor,
            PoolHealthStatus,
        )

        monitor = ConnectionPoolMonitor()

        with patch("baldur_pro.services.pool_monitor.logger") as mock_logger:
            monitor.set_simulation_override(PoolHealthStatus.CRITICAL, None, "exp-1")
            mock_logger.info.assert_called_once()
            call = mock_logger.info.call_args
            assert call.args[0] == "pool_monitor.simulation_override_set"
            assert (
                call.kwargs.get("pool_health_status") == PoolHealthStatus.CRITICAL.value
            )

    def test_clear_simulation_override_logs_cleared_event(self):
        """clear_simulation_override logs a cleared event."""
        from baldur_pro.services.pool_monitor import (
            ConnectionPoolMonitor,
            PoolHealthStatus,
        )

        monitor = ConnectionPoolMonitor()
        monitor.set_simulation_override(PoolHealthStatus.HEALTHY, None, None)

        with patch("baldur_pro.services.pool_monitor.logger") as mock_logger:
            monitor.set_simulation_override(None, None, None)
            # health_status is None -> cleared message
            mock_logger.info.assert_called_once()
            call = mock_logger.info.call_args
            assert call.args[0] == "pool_monitor.simulation_override_cleared"
