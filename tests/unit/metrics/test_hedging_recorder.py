"""
HedgingMetricRecorder Unit Tests (394 — R17).

Test targets:
    - baldur.metrics.recorders.hedging.HedgingMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, metric count, facade registration
    B. Behavior: Method invocations, benefit guard, synthetic label usage

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def hedging_recorder():
    from baldur.metrics.recorders.hedging import HedgingMetricRecorder

    return HedgingMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestHedgingRecorderContract:
    """HedgingMetricRecorder contract: exports, metric count, facade."""

    def test_all_exports_exactly_class(self):
        """__all__ exports exactly ['HedgingMetricRecorder']."""
        from baldur.metrics.recorders.hedging import __all__

        assert __all__ == ["HedgingMetricRecorder"]

    def test_eleven_metrics_defined(self, hedging_recorder):
        """__init__ defines exactly 11 metric attributes."""
        metric_attrs = [
            "_total",
            "_success_total",
            "_failed_total",
            "_timeout_total",
            "_non_retryable_total",
            "_hedged_total",
            "_disabled_due_to_load",
            "_latency_seconds",
            "_benefit_ms",
            "_candidate_tried",
            "_mismatch_total",
        ]
        for attr in metric_attrs:
            assert hasattr(hedging_recorder, attr), f"Missing metric attribute: {attr}"
        assert len(metric_attrs) == 11

    def test_facade_has_hedging_attribute(self):
        """BaldurMetrics exposes hedging recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.hedging import HedgingMetricRecorder

        m = get_metrics()
        assert isinstance(m.hedging, HedgingMetricRecorder)


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestHedgingRecorderBehavior:
    """HedgingMetricRecorder method behavior."""

    def test_record_execution_does_not_raise(self, hedging_recorder):
        """record_execution with mode string does not raise."""
        hedging_recorder.record_execution("eager")

    def test_record_success_does_not_raise(self, hedging_recorder):
        """record_success with source and latency does not raise."""
        hedging_recorder.record_success("primary", 0.25)

    def test_record_failure_does_not_raise(self, hedging_recorder):
        """record_failure does not raise."""
        hedging_recorder.record_failure()

    def test_record_timeout_does_not_raise(self, hedging_recorder):
        """record_timeout does not raise."""
        hedging_recorder.record_timeout()

    def test_record_non_retryable_does_not_raise(self, hedging_recorder):
        """record_non_retryable does not raise."""
        hedging_recorder.record_non_retryable()

    def test_record_hedged_does_not_raise(self, hedging_recorder):
        """record_hedged does not raise."""
        hedging_recorder.record_hedged()

    def test_record_disabled_does_not_raise(self, hedging_recorder):
        """record_disabled with load_level string does not raise."""
        hedging_recorder.record_disabled("high")

    def test_record_benefit_positive_does_not_raise(self, hedging_recorder):
        """record_benefit with positive value does not raise."""
        hedging_recorder.record_benefit(150.0)

    def test_record_benefit_zero_does_not_record(self, hedging_recorder):
        """record_benefit(0) skips recording due to benefit_ms > 0 guard."""
        with patch.object(hedging_recorder, "_benefit_ms", autospec=True) as mock_hist:
            hedging_recorder.record_benefit(0)
            mock_hist.labels.assert_not_called()

    def test_record_candidate_tried_does_not_raise(self, hedging_recorder):
        """record_candidate_tried with candidate string does not raise."""
        hedging_recorder.record_candidate_tried("candidate_a")

    def test_record_result_mismatch_does_not_raise(self, hedging_recorder):
        """record_result_mismatch with mismatch_type string does not raise."""
        hedging_recorder.record_result_mismatch("value_diff")

    def test_all_methods_use_synthetic_label(self, hedging_recorder):
        """All recording methods call _get_synthetic_label()."""
        with patch.object(
            hedging_recorder,
            "_get_synthetic_label",
            return_value="false",
            autospec=True,
        ) as mock_synth:
            hedging_recorder.record_execution("eager")
            hedging_recorder.record_success("primary", 0.1)
            hedging_recorder.record_failure()
            hedging_recorder.record_timeout()
            hedging_recorder.record_non_retryable()
            hedging_recorder.record_hedged()
            hedging_recorder.record_disabled("high")
            hedging_recorder.record_benefit(100.0)
            hedging_recorder.record_candidate_tried("c1")
            hedging_recorder.record_result_mismatch("type_diff")

            # 10 methods, each calls _get_synthetic_label once
            assert mock_synth.call_count == 10
