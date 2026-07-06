"""
DLQ Overflow Metrics Unit Tests (329_DLQ_SIZE_LIMIT).

Test targets:
    - baldur.metrics.prometheus.BaldurMetrics (overflow metrics)
    - baldur.metrics.event_handlers.DLQMetricEventHandler (overflow handlers)

Test Categories:
    A. Contract: Metric attributes exist with correct labels
    B. Behavior — Recording methods: record_dlq_overflow, record_dlq_evicted, etc.
    C. Behavior — Event handlers: on_overflow_rejected, on_overflow_evicted
"""

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# A. Contract Tests — Metric attributes exist
# =============================================================================


class TestOverflowMetricsExistContract:
    """Overflow metric attributes exist on BaldurMetrics."""

    @pytest.fixture(autouse=True)
    def _check_prometheus(self):
        """Skip if prometheus_client is not installed."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_dlq_overflow_total_exists(self):
        """dlq._overflow_total counter exists on DLQ recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_a")
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "_overflow_total")

    def test_dlq_overflow_total_labels(self):
        """dlq._overflow_total has domain and strategy labels."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_b")
        assert "domain" in metrics.dlq._overflow_total._labelnames
        assert "strategy" in metrics.dlq._overflow_total._labelnames

    def test_dlq_evicted_total_exists(self):
        """dlq._evicted_total counter exists on DLQ recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_c")
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "_evicted_total")

    def test_dlq_evicted_total_labels(self):
        """dlq._evicted_total has domain and strategy labels."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_d")
        assert "domain" in metrics.dlq._evicted_total._labelnames
        assert "strategy" in metrics.dlq._evicted_total._labelnames

    def test_dlq_rejected_total_exists(self):
        """dlq._rejected_total counter exists on DLQ recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_e")
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "_rejected_total")

    def test_dlq_rejected_total_labels(self):
        """dlq._rejected_total has domain label."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_f")
        assert "domain" in metrics.dlq._rejected_total._labelnames

    def test_dlq_emergency_purge_total_exists(self):
        """dlq._emergency_purge_total counter exists on DLQ recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_g")
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "_emergency_purge_total")

    def test_dlq_size_ratio_exists(self):
        """dlq._size_ratio gauge exists on DLQ recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_h")
        assert hasattr(metrics, "dlq")
        assert hasattr(metrics.dlq, "_size_ratio")

    def test_dlq_size_ratio_labels(self):
        """dlq._size_ratio has domain label."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_overflow_i")
        assert "domain" in metrics.dlq._size_ratio._labelnames


# =============================================================================
# B. Behavior Tests — Recording methods
# =============================================================================


class TestOverflowRecordingMethodsBehavior:
    """BaldurMetrics overflow recording methods behavior."""

    @pytest.fixture(autouse=True)
    def _check_prometheus(self):
        """Skip if prometheus_client is not installed."""
        from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

        if not PROMETHEUS_AVAILABLE:
            pytest.skip("prometheus_client not installed")

    def test_record_dlq_overflow_increments_counter(self):
        """record_dlq_overflow increments dlq._overflow_total."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_rec_overflow")
        metrics.record_dlq_overflow("payment", "drop_oldest")

        val = metrics.dlq._overflow_total.labels(
            domain="payment", strategy="drop_oldest"
        )._value.get()
        assert val == 1.0

    def test_record_dlq_evicted_increments_by_count(self):
        """record_dlq_evicted increments counter by evicted count."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_rec_evicted")
        metrics.record_dlq_evicted(count=50, strategy="drop_oldest", domain="payment")

        val = metrics.dlq._evicted_total.labels(
            domain="payment", strategy="drop_oldest"
        )._value.get()
        assert val == 50.0

    def test_record_dlq_rejected_increments_counter(self):
        """record_dlq_rejected increments dlq._rejected_total."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_rec_rejected")
        metrics.record_dlq_rejected("payment")

        val = metrics.dlq._rejected_total.labels(domain="payment")._value.get()
        assert val == 1.0

    def test_record_dlq_emergency_purge_increments_counter(self):
        """record_dlq_emergency_purge increments counter."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_rec_emergency")
        before = metrics.dlq._emergency_purge_total._value.get()
        metrics.record_dlq_emergency_purge()

        val = metrics.dlq._emergency_purge_total._value.get()
        assert val == before + 1.0

    def test_set_dlq_size_ratio_sets_gauge(self):
        """set_dlq_size_ratio sets gauge value."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test_rec_ratio")
        metrics.set_dlq_size_ratio("payment", 0.75)

        val = metrics.dlq._size_ratio.labels(domain="payment")._value.get()
        assert 0.74 <= val <= 0.76  # Allow floating-point tolerance

    def test_recording_noop_when_not_initialized(self):
        """Recording methods are no-ops when _initialized is False."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics.__new__(BaldurMetrics)
        metrics._initialized = False

        # Should not raise
        metrics.record_dlq_overflow("payment", "drop_oldest")
        metrics.record_dlq_evicted(10, "drop_oldest")
        metrics.record_dlq_rejected("payment")
        metrics.record_dlq_emergency_purge()
        metrics.set_dlq_size_ratio("payment", 0.5)


# =============================================================================
# C. Behavior Tests — Event Handlers
# =============================================================================


class TestDLQMetricEventHandlerOverflowBehavior:
    """DLQMetricEventHandler overflow event handler behavior."""

    def test_on_overflow_rejected_calls_record_dlq_rejected(self):
        """on_overflow_rejected calls metrics.record_dlq_rejected."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_metrics.record_dlq_rejected = MagicMock()
        mock_metrics.record_dlq_overflow = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_overflow_rejected("external_service")

        mock_metrics.record_dlq_rejected.assert_called_once_with("external_service")

    def test_on_overflow_rejected_calls_record_dlq_overflow_with_reject_strategy(self):
        """on_overflow_rejected also records overflow event with 'reject' strategy."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_overflow_rejected("external_service")

        mock_metrics.record_dlq_overflow.assert_called_once_with(
            "external_service", "reject"
        )

    def test_on_overflow_rejected_noop_when_metrics_none(self):
        """on_overflow_rejected is no-op when metrics not available."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=None,
        ):
            # Should not raise
            DLQMetricEventHandler.on_overflow_rejected("external_service")

    def test_on_overflow_evicted_calls_record_dlq_evicted(self):
        """on_overflow_evicted calls metrics.record_dlq_evicted."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_overflow_evicted(evicted_count=100, level="normal")

        mock_metrics.record_dlq_evicted.assert_called_once_with(
            count=100, strategy="drop_oldest"
        )

    def test_on_overflow_evicted_emergency_calls_emergency_purge(self):
        """on_overflow_evicted with level='emergency' records emergency purge."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_overflow_evicted(
                evicted_count=5000, level="emergency"
            )

        mock_metrics.record_dlq_emergency_purge.assert_called_once()

    def test_on_overflow_evicted_normal_does_not_call_emergency_purge(self):
        """on_overflow_evicted with level='normal' does NOT record emergency purge."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            DLQMetricEventHandler.on_overflow_evicted(evicted_count=100, level="normal")

        mock_metrics.record_dlq_emergency_purge.assert_not_called()

    def test_on_overflow_evicted_noop_when_metrics_none(self):
        """on_overflow_evicted is no-op when metrics not available."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=None,
        ):
            # Should not raise
            DLQMetricEventHandler.on_overflow_evicted(evicted_count=100, level="normal")

    def test_on_overflow_rejected_graceful_on_exception(self):
        """on_overflow_rejected does not raise on internal exception."""
        from baldur.metrics.event_handlers import DLQMetricEventHandler

        mock_metrics = MagicMock()
        mock_metrics.record_dlq_rejected.side_effect = RuntimeError("boom")

        with patch(
            "baldur.metrics.event_handlers._get_metrics",
            return_value=mock_metrics,
        ):
            # Should not raise (fail-open)
            DLQMetricEventHandler.on_overflow_rejected("external_service")
