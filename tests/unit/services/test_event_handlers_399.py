"""
Tests for event handlers introduced in 399.

Sources:
- src/baldur/services/event_bus/bus/_learning_handlers.py
- src/baldur/services/event_bus/bus/_daily_report_handlers.py

Tests:
- Behavior: handlers accept event dict and log via structlog
- Behavior: handlers handle missing event data keys gracefully
- Side effect: handlers call metric recorder methods when available
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch


@dataclass
class FakeBaldurEvent:
    """Lightweight stand-in for BaldurEvent to avoid heavy imports."""

    data: dict[str, Any] = field(default_factory=dict)
    source: str = "test"


# =============================================================================
# Learning Handlers
# =============================================================================


class TestLearningParameterBlacklistedHandlerBehavior:
    """Behavior tests for _on_learning_parameter_blacklisted handler."""

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_logs_event(self, mock_get_recorder, mock_logger):
        """_on_learning_parameter_blacklisted logs via structlog.info."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_parameter_blacklisted,
        )

        event = FakeBaldurEvent(
            data={
                "pattern_key": "cb:threshold",
                "blocked_values": ["0.1"],
                "reason": "repeated failures",
            }
        )
        _on_learning_parameter_blacklisted(event)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "learning.parameter_blacklisted"

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_handles_missing_keys_gracefully(
        self, mock_get_recorder, mock_logger
    ):
        """_on_learning_parameter_blacklisted handles empty data dict without error."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_parameter_blacklisted,
        )

        event = FakeBaldurEvent(data={})
        _on_learning_parameter_blacklisted(event)  # Should not raise

        mock_logger.info.assert_called_once()

    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    def test_handler_calls_recorder_when_available(
        self, mock_logger, mock_get_recorder
    ):
        """_on_learning_parameter_blacklisted calls recorder.record_blacklisted."""
        mock_recorder = MagicMock()
        mock_recorder.record_blacklisted = MagicMock()
        mock_get_recorder.return_value = mock_recorder

        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_parameter_blacklisted,
        )

        event = FakeBaldurEvent(
            data={"module": "circuit_breaker", "reason": "flapping"}
        )
        _on_learning_parameter_blacklisted(event)

        mock_recorder.record_blacklisted.assert_called_once_with(
            module="circuit_breaker", reason="flapping"
        )


class TestLearningPatternDetectedHandlerBehavior:
    """Behavior tests for _on_learning_pattern_detected handler."""

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_logs_event(self, mock_get_recorder, mock_logger):
        """_on_learning_pattern_detected logs via structlog.info."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_pattern_detected,
        )

        event = FakeBaldurEvent(
            data={"rule_name": "timeout_rule", "pattern_type": "failure"}
        )
        _on_learning_pattern_detected(event)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "learning.pattern_detected"

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_handles_missing_keys_gracefully(
        self, mock_get_recorder, mock_logger
    ):
        """_on_learning_pattern_detected handles empty data dict without error."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_pattern_detected,
        )

        event = FakeBaldurEvent(data={})
        _on_learning_pattern_detected(event)  # Should not raise

    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    def test_handler_calls_recorder_when_available(
        self, mock_logger, mock_get_recorder
    ):
        """_on_learning_pattern_detected calls recorder.record_pattern."""
        mock_recorder = MagicMock()
        mock_recorder.record_pattern = MagicMock()
        mock_get_recorder.return_value = mock_recorder

        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_pattern_detected,
        )

        event = FakeBaldurEvent(data={"pattern_type": "failure", "confidence": 0.85})
        _on_learning_pattern_detected(event)

        mock_recorder.record_pattern.assert_called_once_with(
            pattern_type="failure", confidence=0.85
        )


class TestLearningManualOnlyActivatedHandlerBehavior:
    """Behavior tests for _on_learning_manual_only_activated handler."""

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_logs_event(self, mock_get_recorder, mock_logger):
        """_on_learning_manual_only_activated logs via structlog.info."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_manual_only_activated,
        )

        event = FakeBaldurEvent(data={"module": "circuit_breaker"})
        _on_learning_manual_only_activated(event)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "learning.manual_only_activated"

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_handles_missing_keys_gracefully(
        self, mock_get_recorder, mock_logger
    ):
        """_on_learning_manual_only_activated handles empty data dict without error."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_manual_only_activated,
        )

        event = FakeBaldurEvent(data={})
        _on_learning_manual_only_activated(event)  # Should not raise

    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    def test_handler_calls_recorder_with_activated_true(
        self, mock_logger, mock_get_recorder
    ):
        """_on_learning_manual_only_activated calls recorder.set_manual_only(enabled=True)."""
        mock_recorder = MagicMock()
        mock_recorder.set_manual_only = MagicMock()
        mock_get_recorder.return_value = mock_recorder

        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_manual_only_activated,
        )

        event = FakeBaldurEvent(data={"module": "retry"})
        _on_learning_manual_only_activated(event)

        mock_recorder.set_manual_only.assert_called_once_with(
            module="retry", enabled=True
        )


class TestLearningManualOnlyDeactivatedHandlerBehavior:
    """Behavior tests for _on_learning_manual_only_deactivated handler."""

    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
        return_value=None,
    )
    def test_handler_logs_event(self, mock_get_recorder, mock_logger):
        """_on_learning_manual_only_deactivated logs via structlog.info."""
        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_manual_only_deactivated,
        )

        event = FakeBaldurEvent(data={"module": "circuit_breaker"})
        _on_learning_manual_only_deactivated(event)

        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "learning.manual_only_deactivated"

    @patch(
        "baldur.services.event_bus.bus._learning_handlers._get_learning_recorder",
    )
    @patch(
        "baldur.services.event_bus.bus._learning_handlers.logger",
    )
    def test_handler_calls_recorder_with_activated_false(
        self, mock_logger, mock_get_recorder
    ):
        """_on_learning_manual_only_deactivated calls recorder.set_manual_only(enabled=False)."""
        mock_recorder = MagicMock()
        mock_recorder.set_manual_only = MagicMock()
        mock_get_recorder.return_value = mock_recorder

        from baldur.services.event_bus.bus._learning_handlers import (
            _on_learning_manual_only_deactivated,
        )

        event = FakeBaldurEvent(data={"module": "retry"})
        _on_learning_manual_only_deactivated(event)

        mock_recorder.set_manual_only.assert_called_once_with(
            module="retry", enabled=False
        )


# =============================================================================
# Daily Report Handlers
# =============================================================================


class TestDailyReportSendFailedHandlerBehavior:
    """Behavior tests for _on_daily_report_send_failed handler."""

    @patch(
        "baldur.services.event_bus.bus._daily_report_handlers.logger",
    )
    def test_handler_logs_event(self, mock_logger):
        """_on_daily_report_send_failed logs via structlog.warning."""
        from baldur.services.event_bus.bus._daily_report_handlers import (
            _on_daily_report_send_failed,
        )

        event = FakeBaldurEvent(
            data={
                "channel": "slack",
                "error": "timeout",
                "date": "2026-03-28",
            }
        )
        _on_daily_report_send_failed(event)

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "daily_report.send_failed"

    @patch(
        "baldur.services.event_bus.bus._daily_report_handlers.logger",
    )
    def test_handler_handles_missing_keys_gracefully(self, mock_logger):
        """_on_daily_report_send_failed handles empty data dict without error."""
        from baldur.services.event_bus.bus._daily_report_handlers import (
            _on_daily_report_send_failed,
        )

        event = FakeBaldurEvent(data={})
        _on_daily_report_send_failed(event)  # Should not raise

        mock_logger.warning.assert_called_once()

    @patch(
        "baldur.services.event_bus.bus._daily_report_handlers.logger",
    )
    def test_handler_does_not_call_recorder(self, mock_logger):
        """_on_daily_report_send_failed only logs — no metric recording (service already records)."""
        from baldur.services.event_bus.bus._daily_report_handlers import (
            _on_daily_report_send_failed,
        )

        event = FakeBaldurEvent(data={"channel": "email", "error": "SMTP error"})
        _on_daily_report_send_failed(event)

        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "daily_report.send_failed"
