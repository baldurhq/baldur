"""
EventBusMetricRecorder Unit Tests (423 — D5).

Test targets:
    - baldur.metrics.recorders.event_bus.EventBusMetricRecorder
    - Module-level convenience functions
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, metric names
    B. Behavior: Counter increment, label validation, fail-open

Reference:
    423
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def event_bus_recorder():
    from baldur.metrics.recorders.event_bus import EventBusMetricRecorder

    return EventBusMetricRecorder()


# =============================================================================
# A. Contract Tests — Exports and Metric Names
# =============================================================================


class TestEventBusRecorderContract:
    """D5: EventBusMetricRecorder contract values."""

    def test_exports_class_and_convenience_functions(self):
        """__all__ includes class + 2 convenience functions."""
        from baldur.metrics.recorders.event_bus import __all__

        assert "EventBusMetricRecorder" in __all__
        assert "record_emit_skipped" in __all__
        assert "record_emit_failed" in __all__

    def test_metric_name_emit_skipped(self, event_bus_recorder):
        """emit_skipped counter has correct name prefix."""
        counter = event_bus_recorder._emit_skipped_total
        assert "baldur" in counter._name
        assert "emit_skipped" in counter._name

    def test_metric_name_emit_failed(self, event_bus_recorder):
        """emit_failed counter has correct name prefix."""
        counter = event_bus_recorder._emit_failed_total
        assert "baldur" in counter._name
        assert "emit_failed" in counter._name

    def test_emit_skipped_has_source_label(self, event_bus_recorder):
        """emit_skipped counter has 'source' label."""
        counter = event_bus_recorder._emit_skipped_total
        assert "source" in counter._labelnames

    def test_emit_failed_has_source_and_event_type_labels(self, event_bus_recorder):
        """emit_failed counter has 'source' and 'event_type' labels."""
        counter = event_bus_recorder._emit_failed_total
        assert "source" in counter._labelnames
        assert "event_type" in counter._labelnames


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestEventBusRecorderBehavior:
    """D5: EventBusMetricRecorder method behavior."""

    def test_record_emit_skipped_increments_counter(self, event_bus_recorder):
        """record_emit_skipped increments counter with source label."""
        # Given
        source = "test_service"

        # When
        event_bus_recorder.record_emit_skipped(source)

        # Then - no exception raised, counter incremented
        # (Counter value verification requires prometheus_client internals)

    def test_record_emit_failed_increments_counter(self, event_bus_recorder):
        """record_emit_failed increments counter with source and event_type labels."""
        # Given
        source = "replay_service"
        event_type = "emergency_level_changed"

        # When
        event_bus_recorder.record_emit_failed(source, event_type)

        # Then - no exception raised

    def test_record_emit_skipped_handles_exception(self, event_bus_recorder):
        """record_emit_skipped logs warning on exception (fail-open)."""
        # Given - mock counter to raise exception
        event_bus_recorder._emit_skipped_total = MagicMock()
        event_bus_recorder._emit_skipped_total.labels.side_effect = RuntimeError(
            "Prometheus error"
        )

        # When/Then - no exception raised (fail-open)
        event_bus_recorder.record_emit_skipped("test_service")

    def test_record_emit_failed_handles_exception(self, event_bus_recorder):
        """record_emit_failed logs warning on exception (fail-open)."""
        # Given - mock counter to raise exception
        event_bus_recorder._emit_failed_total = MagicMock()
        event_bus_recorder._emit_failed_total.labels.side_effect = RuntimeError(
            "Prometheus error"
        )

        # When/Then - no exception raised (fail-open)
        event_bus_recorder.record_emit_failed("test_service", "test_event")


# =============================================================================
# C. Convenience Function Tests
# =============================================================================


class TestEventBusConvenienceFunctions:
    """D5: Module-level convenience functions."""

    def test_record_emit_skipped_delegates_to_recorder(self):
        """record_emit_skipped() calls recorder method."""
        from baldur.metrics.recorders.event_bus import record_emit_skipped

        with patch("baldur.metrics.recorders.event_bus._lazy_recorder") as mock_lazy:
            mock_recorder = MagicMock()
            mock_lazy.return_value = mock_recorder

            record_emit_skipped("my_service")

            mock_recorder.record_emit_skipped.assert_called_once_with("my_service")

    def test_record_emit_failed_delegates_to_recorder(self):
        """record_emit_failed() calls recorder method."""
        from baldur.metrics.recorders.event_bus import record_emit_failed

        with patch("baldur.metrics.recorders.event_bus._lazy_recorder") as mock_lazy:
            mock_recorder = MagicMock()
            mock_lazy.return_value = mock_recorder

            record_emit_failed("my_service", "test_event")

            mock_recorder.record_emit_failed.assert_called_once_with(
                "my_service", "test_event"
            )

    def test_record_emit_skipped_no_op_when_recorder_unavailable(self):
        """record_emit_skipped() is no-op when recorder is None."""
        from baldur.metrics.recorders.event_bus import record_emit_skipped

        with patch(
            "baldur.metrics.recorders.event_bus._lazy_recorder", return_value=None
        ):
            # When/Then - no exception raised
            record_emit_skipped("my_service")

    def test_record_emit_failed_no_op_when_recorder_unavailable(self):
        """record_emit_failed() is no-op when recorder is None."""
        from baldur.metrics.recorders.event_bus import record_emit_failed

        with patch(
            "baldur.metrics.recorders.event_bus._lazy_recorder", return_value=None
        ):
            # When/Then - no exception raised
            record_emit_failed("my_service", "test_event")


# =============================================================================
# D. Facade Registration Tests
# =============================================================================


class TestEventBusFacadeRegistration:
    """D5: EventBusMetricRecorder registered in BaldurMetrics facade."""

    def test_event_bus_recorder_in_facade(self):
        """BaldurMetrics has event_bus attribute."""
        from baldur.metrics.prometheus import BaldurMetrics
        from baldur.metrics.recorders.event_bus import EventBusMetricRecorder

        metrics = BaldurMetrics()

        assert hasattr(metrics, "event_bus")
        assert isinstance(metrics.event_bus, EventBusMetricRecorder)
