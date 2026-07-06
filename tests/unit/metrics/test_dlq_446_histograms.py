"""
DLQ Histogram & Event Handler Duration Tests (446).

Test targets:
    - baldur.metrics.recorders.dlq (3 histograms + 3 recording methods)
    - baldur.metrics.event_handlers (on_item_created duration_seconds)

Test Categories:
    Contract: Histogram metric names, labels, bucket values
    Behavior: observe calls, domain resolve, None handling
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def dlq_recorder():
    """Create a DLQMetricRecorder instance."""
    from baldur.metrics.recorders.dlq import DLQMetricRecorder

    return DLQMetricRecorder()


# =============================================================================
# A. Contract Tests — Histogram Definitions
# =============================================================================


class TestDLQHistogramContract:
    """Histogram metric name, label, and bucket contract verification."""

    def test_store_duration_metric_name(self, dlq_recorder):
        """Store duration histogram has correct name."""
        assert (
            dlq_recorder._store_duration_seconds._name
            == "baldur_dlq_store_duration_seconds"
        )

    def test_store_duration_has_domain_label(self, dlq_recorder):
        """Store duration histogram has domain label."""
        assert "domain" in dlq_recorder._store_duration_seconds._labelnames

    def test_store_duration_buckets(self, dlq_recorder):
        """Store duration histogram has per-doc custom buckets."""
        buckets = dlq_recorder._store_duration_seconds._kwargs.get("upper_bounds")
        if buckets is None:
            buckets = dlq_recorder._store_duration_seconds._upper_bounds
        assert 0.001 in buckets
        assert 10 in buckets or 10.0 in buckets

    def test_replay_duration_metric_name(self, dlq_recorder):
        """Replay duration histogram has correct name."""
        assert (
            dlq_recorder._replay_duration_seconds._name
            == "baldur_dlq_replay_duration_seconds"
        )

    def test_replay_duration_has_domain_label(self, dlq_recorder):
        """Replay duration histogram has domain label."""
        assert "domain" in dlq_recorder._replay_duration_seconds._labelnames

    def test_replay_duration_buckets_include_long_operations(self, dlq_recorder):
        """Replay duration buckets cover long operations (up to 120s)."""
        buckets = dlq_recorder._replay_duration_seconds._upper_bounds
        assert 120 in buckets or 120.0 in buckets

    def test_acquire_duration_metric_name(self, dlq_recorder):
        """Acquire duration histogram has correct name."""
        assert (
            dlq_recorder._acquire_duration_seconds._name
            == "baldur_dlq_acquire_duration_seconds"
        )

    def test_acquire_duration_has_domain_label(self, dlq_recorder):
        """Acquire duration histogram has domain label."""
        assert "domain" in dlq_recorder._acquire_duration_seconds._labelnames

    def test_acquire_duration_buckets_are_submillisecond(self, dlq_recorder):
        """Acquire duration buckets start at sub-millisecond range."""
        buckets = dlq_recorder._acquire_duration_seconds._upper_bounds
        assert 0.0005 in buckets


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestDLQRecorderDurationBehavior:
    """Duration recording method behavior."""

    def test_record_store_duration_observes_histogram(self, dlq_recorder):
        """record_store_duration calls observe on store histogram."""
        mock_hist = MagicMock()
        dlq_recorder._store_duration_seconds = mock_hist

        with patch.object(dlq_recorder, "_resolve_domain", return_value="payment"):
            dlq_recorder.record_store_duration("payment", 0.125)

        mock_hist.labels.assert_called_once_with(domain="payment")
        mock_hist.labels().observe.assert_called_once_with(0.125)

    def test_record_replay_duration_observes_histogram(self, dlq_recorder):
        """record_replay_duration calls observe on replay histogram."""
        mock_hist = MagicMock()
        dlq_recorder._replay_duration_seconds = mock_hist

        with patch.object(dlq_recorder, "_resolve_domain", return_value="inventory"):
            dlq_recorder.record_replay_duration("inventory", 2.5)

        mock_hist.labels.assert_called_once_with(domain="inventory")
        mock_hist.labels().observe.assert_called_once_with(2.5)

    def test_record_acquire_duration_observes_histogram(self, dlq_recorder):
        """record_acquire_duration calls observe on acquire histogram."""
        mock_hist = MagicMock()
        dlq_recorder._acquire_duration_seconds = mock_hist

        with patch.object(dlq_recorder, "_resolve_domain", return_value="webhook"):
            dlq_recorder.record_acquire_duration("webhook", 0.003)

        mock_hist.labels.assert_called_once_with(domain="webhook")
        mock_hist.labels().observe.assert_called_once_with(0.003)

    def test_record_store_duration_resolves_domain(self, dlq_recorder):
        """record_store_duration applies domain cardinality guard."""
        mock_hist = MagicMock()
        dlq_recorder._store_duration_seconds = mock_hist

        with patch.object(
            dlq_recorder, "_resolve_domain", return_value="OTHER_DOMAIN"
        ) as mock_resolve:
            dlq_recorder.record_store_duration("unregistered_domain", 0.1)

        mock_resolve.assert_called_once_with("unregistered_domain")
        mock_hist.labels.assert_called_once_with(domain="OTHER_DOMAIN")

    def test_record_store_duration_swallows_exception(self, dlq_recorder):
        """record_store_duration does not raise on internal errors."""
        mock_hist = MagicMock()
        mock_hist.labels.side_effect = RuntimeError("metric error")
        dlq_recorder._store_duration_seconds = mock_hist

        with patch.object(dlq_recorder, "_resolve_domain", return_value="payment"):
            dlq_recorder.record_store_duration("payment", 0.1)


# =============================================================================
# B. Behavior Tests — Event Handler on_item_created Duration (D8)
# =============================================================================


class TestOnItemCreatedDurationBehavior:
    """on_item_created duration_seconds parameter behavior."""

    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge", return_value=None)
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_duration_none_skips_histogram(self, mock_get_metrics, _):
        """When duration_seconds is None, store histogram is not observed."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler
        from baldur.metrics.registry import register_domain

        register_domain("payment")
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created("payment", "PG_TIMEOUT")

        mock_metrics.dlq.record_store_duration.assert_not_called()

    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge", return_value=None)
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_duration_provided_observes_histogram(self, mock_get_metrics, _):
        """When duration_seconds is provided, store histogram is observed."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler
        from baldur.metrics.registry import register_domain

        register_domain("payment")
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created(
            "payment", "PG_TIMEOUT", duration_seconds=0.05
        )

        mock_metrics.dlq.record_store_duration.assert_called_once_with("payment", 0.05)

    @patch("baldur.metrics.event_handlers._get_safe_pending_gauge", return_value=None)
    @patch("baldur.metrics.event_handlers._get_metrics")
    def test_counter_still_incremented_with_duration(self, mock_get_metrics, _):
        """Counter increment still works when duration_seconds is provided."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler
        from baldur.metrics.registry import register_domain

        register_domain("payment")
        mock_metrics = MagicMock()
        mock_get_metrics.return_value = mock_metrics

        DLQMetricEventHandler.on_item_created(
            "payment", "PG_TIMEOUT", duration_seconds=0.1
        )

        mock_metrics.record_dlq_item_created.assert_called_once_with(
            "payment", "PG_TIMEOUT"
        )
