"""
ForecasterMetricRecorder Unit Tests (394 — R).

Test targets:
    - baldur.metrics.recorders.forecaster.ForecasterMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration
    B. Behavior: Method calls, dry_run label, clamping, label sanitization

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def forecaster_recorder():
    from baldur.metrics.recorders.forecaster import ForecasterMetricRecorder

    return ForecasterMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestForecasterRecorderContract:
    """ForecasterMetricRecorder contract: exports and facade registration."""

    def test_all_exports_exactly_recorder_class(self):
        """__all__ exports exactly ['ForecasterMetricRecorder']."""
        from baldur.metrics.recorders.forecaster import __all__

        assert __all__ == ["ForecasterMetricRecorder"]

    def test_facade_has_forecaster_attribute(self):
        """BaldurMetrics exposes forecaster recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.forecaster import ForecasterMetricRecorder

        m = get_metrics()
        assert isinstance(m.forecaster, ForecasterMetricRecorder)


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestForecasterRecorderBehavior:
    """ForecasterMetricRecorder method behavior."""

    def test_record_forecast_does_not_raise(self, forecaster_recorder):
        """record_forecast with metric_name does not raise."""
        forecaster_recorder.record_forecast("cpu_usage")

    def test_record_anomaly_does_not_raise(self, forecaster_recorder):
        """record_anomaly with metric_name and detector_type does not raise."""
        forecaster_recorder.record_anomaly("memory_usage", "zscore")

    def test_record_spike_does_not_raise(self, forecaster_recorder):
        """record_spike with metric_name and spike_type does not raise."""
        forecaster_recorder.record_spike("request_rate", "sudden")

    def test_record_action_dry_run_true_passes_true_label(self, forecaster_recorder):
        """record_action with dry_run=True passes 'true' for dry_run label."""
        with patch.object(
            forecaster_recorder._actions_total,
            "labels",
            wraps=forecaster_recorder._actions_total.labels,
        ) as mock_labels:
            forecaster_recorder.record_action("sudden", dry_run=True)
            call_kwargs = mock_labels.call_args[1]
            assert call_kwargs["dry_run"] == "true"

    def test_record_action_dry_run_false_passes_false_label(self, forecaster_recorder):
        """record_action with dry_run=False passes 'false' for dry_run label."""
        with patch.object(
            forecaster_recorder._actions_total,
            "labels",
            wraps=forecaster_recorder._actions_total.labels,
        ) as mock_labels:
            forecaster_recorder.record_action("gradual", dry_run=False)
            call_kwargs = mock_labels.call_args[1]
            assert call_kwargs["dry_run"] == "false"

    def test_record_action_rejection_does_not_raise(self, forecaster_recorder):
        """record_action_rejection with reason does not raise."""
        forecaster_recorder.record_action_rejection("budget_exceeded")

    def test_observe_accuracy_does_not_raise(self, forecaster_recorder):
        """observe_accuracy with metric_name and accuracy does not raise."""
        forecaster_recorder.observe_accuracy("cpu_usage", 0.92)

    def test_set_misprediction_count_positive_value(self, forecaster_recorder):
        """set_misprediction_count with positive count does not raise."""
        forecaster_recorder.set_misprediction_count("cpu_usage", 2)

    def test_set_misprediction_count_negative_value_clamped(self, forecaster_recorder):
        """set_misprediction_count with negative count gets clamped to 0."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert BaseMetricRecorder._clamp_non_negative(-1, "mispredictions") == 0
        # Verify the method still completes without error
        forecaster_recorder.set_misprediction_count("cpu_usage", -1)

    def test_safe_metric_name_calls_sanitize_label_value(self, forecaster_recorder):
        """_safe_metric_name delegates to sanitize_label_value."""
        with patch(
            "baldur.metrics.recorders.forecaster.sanitize_label_value",
            autospec=True,
            return_value="sanitized",
        ) as mock_sanitize:
            result = forecaster_recorder._safe_metric_name("raw.metric/name")
            mock_sanitize.assert_called_once_with("raw.metric/name")
            assert result == "sanitized"
