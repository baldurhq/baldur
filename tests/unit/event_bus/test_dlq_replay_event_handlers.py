"""
DLQ Replay Event Handlers Unit Tests (394 — R2).

Test targets:
    - baldur.services.event_bus.bus._replay_handlers (4 logging-only handlers)

Test Categories:
    A. Behavior: Correct log level and field extraction from event data

Reference:
    394 §E (R2)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def make_event():
    """Factory for BaldurEvent with arbitrary data."""

    def _make(data: dict):
        from baldur.services.event_bus.bus.models import BaldurEvent

        return BaldurEvent(
            event_type="test",
            data=data,
            source="test",
        )

    return _make


class TestReplayHandlersBehavior:
    """R2: DLQ replay logging-only handlers emit correct log entries."""

    def test_on_dlq_replay_completed_logs_info(self, make_event):
        """_on_dlq_replay_completed logs at INFO with expected fields."""
        from baldur.services.event_bus.bus._replay_handlers import (
            _on_dlq_replay_completed,
        )

        event = make_event(
            {"dlq_id": 42, "domain": "payment", "success": True, "replay_attempt": 1}
        )
        # Note: structlog logger uses dynamic proxy — autospec incompatible
        with patch(
            "baldur.services.event_bus.bus._replay_handlers.logger"
        ) as mock_logger:
            _on_dlq_replay_completed(event)

        mock_logger.info.assert_called_once()
        call_kwargs = mock_logger.info.call_args
        assert call_kwargs[0][0] == "event_handler.dlq_replay_completed"
        assert call_kwargs[1]["dlq_id"] == 42
        assert call_kwargs[1]["domain"] == "payment"

    def test_on_dlq_replay_failed_logs_warning(self, make_event):
        """_on_dlq_replay_failed logs at WARNING with error fields."""
        from baldur.services.event_bus.bus._replay_handlers import (
            _on_dlq_replay_failed,
        )

        event = make_event(
            {
                "dlq_id": 7,
                "domain": "order",
                "error_type": "ValueError",
                "error_message": "bad data",
                "replay_attempt": 3,
            }
        )
        # Note: structlog logger uses dynamic proxy — autospec incompatible
        with patch(
            "baldur.services.event_bus.bus._replay_handlers.logger"
        ) as mock_logger:
            _on_dlq_replay_failed(event)

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "event_handler.dlq_replay_failed"

    def test_on_dlq_replay_batch_completed_logs_info(self, make_event):
        """_on_dlq_replay_batch_completed logs at INFO with batch summary."""
        from baldur.services.event_bus.bus._replay_handlers import (
            _on_dlq_replay_batch_completed,
        )

        event = make_event(
            {
                "domain": "all",
                "total": 10,
                "success_count": 8,
                "failed_count": 2,
            }
        )
        # Note: structlog logger uses dynamic proxy — autospec incompatible
        with patch(
            "baldur.services.event_bus.bus._replay_handlers.logger"
        ) as mock_logger:
            _on_dlq_replay_batch_completed(event)

        mock_logger.info.assert_called_once()
        assert (
            mock_logger.info.call_args[0][0]
            == "event_handler.dlq_replay_batch_completed"
        )

    def test_on_dlq_replay_blocked_logs_warning(self, make_event):
        """_on_dlq_replay_blocked logs at WARNING with block reason."""
        from baldur.services.event_bus.bus._replay_handlers import (
            _on_dlq_replay_blocked,
        )

        event = make_event(
            {
                "dlq_id": 99,
                "domain": "dlq",
                "block_reason": "kill_switch",
                "block_message": "System disabled",
            }
        )
        # Note: structlog logger uses dynamic proxy — autospec incompatible
        with patch(
            "baldur.services.event_bus.bus._replay_handlers.logger"
        ) as mock_logger:
            _on_dlq_replay_blocked(event)

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "event_handler.dlq_replay_blocked"
        assert mock_logger.warning.call_args[1]["block_reason"] == "kill_switch"

    def test_handler_handles_missing_data_keys(self, make_event):
        """Handlers do not crash when event data keys are missing."""
        from baldur.services.event_bus.bus._replay_handlers import (
            _on_dlq_replay_completed,
        )

        event = make_event({})
        _on_dlq_replay_completed(event)
