"""
Tests for D7: CB->Daily Report push from event handler.

Source: src/baldur/metrics/event_handlers.py (CircuitBreakerEventHandler.on_state_changed)
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock, patch


class TestCBDailyReportPushBehavior:
    """Behavior tests for daily report push in CircuitBreakerEventHandler.on_state_changed."""

    @patch(
        "baldur.metrics.event_handlers._get_metrics",
    )
    @patch(
        "baldur.metrics.event_handlers.get_daily_report_collector",
    )
    def test_open_state_pushes_transitions_and_opened(
        self, mock_get_collector, mock_get_metrics
    ):
        """to_state='open' pushes circuit_transitions=1, circuits_opened=1."""
        from baldur.metrics.event_handlers import CircuitBreakerEventHandler

        # Given
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics
        mock_collector = Mock()
        mock_get_collector.return_value = mock_collector

        # When
        CircuitBreakerEventHandler.on_state_changed(
            service="payment_api",
            from_state="closed",
            to_state="open",
        )

        # Then — 428 Phase 1.3 (D3): result includes base_service context
        mock_collector.add_result.assert_called_once_with(
            task_name="circuit_breaker_state_changed",
            result={
                "circuit_transitions": 1,
                "circuits_opened": 1,
                "service": "payment_api",
            },
        )

    @patch(
        "baldur.metrics.event_handlers._get_metrics",
    )
    @patch(
        "baldur.metrics.event_handlers.get_daily_report_collector",
    )
    def test_closed_state_pushes_transitions_and_closed(
        self, mock_get_collector, mock_get_metrics
    ):
        """to_state='closed' pushes circuit_transitions=1, circuits_closed=1."""
        from baldur.metrics.event_handlers import CircuitBreakerEventHandler

        # Given
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics
        mock_collector = Mock()
        mock_get_collector.return_value = mock_collector

        # When
        CircuitBreakerEventHandler.on_state_changed(
            service="order_api",
            from_state="open",
            to_state="closed",
        )

        # Then — 428 Phase 1.3 (D3): result includes base_service context
        mock_collector.add_result.assert_called_once_with(
            task_name="circuit_breaker_state_changed",
            result={
                "circuit_transitions": 1,
                "circuits_closed": 1,
                "service": "order_api",
            },
        )

    @patch(
        "baldur.metrics.event_handlers._get_metrics",
    )
    @patch(
        "baldur.metrics.event_handlers.get_daily_report_collector",
    )
    def test_half_open_state_pushes_transitions_only(
        self, mock_get_collector, mock_get_metrics
    ):
        """to_state='half_open' pushes circuit_transitions=1 only (no opened/closed)."""
        from baldur.metrics.event_handlers import CircuitBreakerEventHandler

        # Given
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics
        mock_collector = Mock()
        mock_get_collector.return_value = mock_collector

        # When
        CircuitBreakerEventHandler.on_state_changed(
            service="inventory_api",
            from_state="open",
            to_state="half_open",
        )

        # Then — 428 Phase 1.3 (D3): result includes base_service context
        mock_collector.add_result.assert_called_once_with(
            task_name="circuit_breaker_state_changed",
            result={"circuit_transitions": 1, "service": "inventory_api"},
        )
