"""
LearningMetricRecorder Unit Tests (394 — R).

Test targets:
    - baldur.metrics.recorders.learning.LearningMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration, metric count
    B. Behavior: Method calls, gauge state, histogram observation

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def learning_recorder():
    from baldur.metrics.recorders.learning import LearningMetricRecorder

    return LearningMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestLearningRecorderContract:
    """LearningMetricRecorder contract: exports, facade, metric count."""

    def test_all_exports_exactly_recorder_class(self):
        """__all__ exports exactly ['LearningMetricRecorder']."""
        from baldur.metrics.recorders.learning import __all__

        assert __all__ == ["LearningMetricRecorder"]

    def test_facade_has_learning_attribute(self):
        """BaldurMetrics exposes learning recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.learning import LearningMetricRecorder

        m = get_metrics()
        assert isinstance(m.learning, LearningMetricRecorder)

    def test_seven_metrics_defined(self, learning_recorder):
        """Recorder defines exactly 7 metric attributes."""
        metric_attrs = [
            "_patterns_total",
            "_pattern_confidence",
            "_suggestions_generated",
            "_suggestions_applied",
            "_blacklisted_total",
            "_manual_only_mode",
            "_anomalies_detected",
        ]
        for attr in metric_attrs:
            assert hasattr(learning_recorder, attr), f"Missing metric: {attr}"
        assert len(metric_attrs) == 7


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestLearningRecorderBehavior:
    """LearningMetricRecorder method behavior."""

    def test_record_pattern_does_not_raise(self, learning_recorder):
        """record_pattern with pattern_type and confidence does not raise."""
        learning_recorder.record_pattern("error_correlation", 0.85)

    def test_record_pattern_observes_confidence_histogram(self, learning_recorder):
        """record_pattern observes confidence value on histogram."""
        with patch.object(
            learning_recorder._pattern_confidence,
            "labels",
            wraps=learning_recorder._pattern_confidence.labels,
        ) as mock_labels:
            learning_recorder.record_pattern("error_correlation", 0.75)
            mock_labels.assert_called_with(pattern_type="error_correlation")

    def test_record_suggestion_generated_does_not_raise(self, learning_recorder):
        """record_suggestion_generated with pattern_type and priority does not raise."""
        learning_recorder.record_suggestion_generated("spike_detection", "high")

    def test_record_suggestion_applied_does_not_raise(self, learning_recorder):
        """record_suggestion_applied with pattern_type does not raise."""
        learning_recorder.record_suggestion_applied("error_correlation")

    def test_record_blacklisted_does_not_raise(self, learning_recorder):
        """record_blacklisted with module and reason does not raise."""
        learning_recorder.record_blacklisted("payment_service", "excessive_failures")

    def test_set_manual_only_true_sets_gauge_to_1(self, learning_recorder):
        """set_manual_only(module, True) sets gauge to 1."""

        mock_gauge = MagicMock()
        learning_recorder._manual_only_mode = mock_gauge
        learning_recorder.set_manual_only("payment_service", True)
        mock_gauge.labels.assert_called_once_with(module="payment_service")
        mock_gauge.labels.return_value.set.assert_called_once_with(1)

    def test_set_manual_only_false_sets_gauge_to_0(self, learning_recorder):
        """set_manual_only(module, False) sets gauge to 0."""

        mock_gauge = MagicMock()
        learning_recorder._manual_only_mode = mock_gauge
        learning_recorder.set_manual_only("payment_service", False)
        mock_gauge.labels.assert_called_once_with(module="payment_service")
        mock_gauge.labels.return_value.set.assert_called_once_with(0)

    def test_record_anomaly_does_not_raise(self, learning_recorder):
        """record_anomaly with metric_name does not raise."""
        learning_recorder.record_anomaly("response_time_p99")
