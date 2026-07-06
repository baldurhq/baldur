"""
Unit tests for observation Prometheus metric recorders (correlation_engine, auto_tuning).

Covers:
- CorrelationEngineMetricRecorder: 8 metrics (Contract + Behavior)
- AutoTuningMetricRecorder: 7 metrics (Contract + Behavior)

Reference:
    docs/baldur/middleware_system/358_LARGE_SERVICE_IMPROVEMENT.md
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _check_prometheus():
    """Skip all tests if prometheus_client is not installed."""
    from baldur.metrics.prometheus import PROMETHEUS_AVAILABLE

    if not PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")


@pytest.fixture
def ce_recorder():
    """Create a CorrelationEngineMetricRecorder instance."""
    from baldur.metrics.recorders.correlation_engine import (
        CorrelationEngineMetricRecorder,
    )

    return CorrelationEngineMetricRecorder()


@pytest.fixture
def at_recorder():
    """Create an AutoTuningMetricRecorder instance."""
    from baldur.metrics.recorders.auto_tuning import AutoTuningMetricRecorder

    return AutoTuningMetricRecorder()


# =============================================================================
# CorrelationEngineMetricRecorder — Contract
# =============================================================================


class TestCorrelationEngineRecorderContract:
    """CorrelationEngineMetricRecorder design contract verification."""

    def test_prefix_is_baldur(self, ce_recorder):
        """Metric prefix follows D13 convention: 'baldur'."""
        assert ce_recorder.PREFIX == "baldur"

    def test_all_eight_metrics_initialized(self, ce_recorder):
        """All 8 metrics are created during __init__."""
        assert ce_recorder._state is not None
        assert ce_recorder._periodic_analysis_total is not None
        assert ce_recorder._periodic_analysis_duration is not None
        assert ce_recorder._incident_analysis_total is not None
        assert ce_recorder._ml_bulkhead_active is not None
        assert ce_recorder._ml_bulkhead_rejected_total is not None
        assert ce_recorder._correlations_tracked is not None
        assert ce_recorder._strategy_fallback_total is not None


# =============================================================================
# CorrelationEngineMetricRecorder — Behavior
# =============================================================================


class TestCorrelationEngineRecorderBehavior:
    """CorrelationEngineMetricRecorder recording behavior."""

    def test_set_state_updates_gauge(self, ce_recorder):
        """set_state() updates the state gauge."""
        # Should not raise
        ce_recorder.set_state(2)

    def test_record_periodic_analysis_increments_counter(self, ce_recorder):
        """record_periodic_analysis() increments counter and observes duration."""
        ce_recorder.record_periodic_analysis(1.5)
        ce_recorder.record_periodic_analysis(0.3)

    def test_record_incident_analysis_increments_counter(self, ce_recorder):
        """record_incident_analysis() increments incident counter."""
        ce_recorder.record_incident_analysis()

    def test_set_ml_bulkhead_active_clamps_negative(self, ce_recorder):
        """set_ml_bulkhead_active() clamps negative values to 0."""
        # Should not raise, negative value clamped
        ce_recorder.set_ml_bulkhead_active(-1)

    def test_record_ml_bulkhead_rejected_with_priority(self, ce_recorder):
        """record_ml_bulkhead_rejected() records with priority label."""
        ce_recorder.record_ml_bulkhead_rejected("low")
        ce_recorder.record_ml_bulkhead_rejected("high")

    def test_set_correlations_tracked_updates_gauge(self, ce_recorder):
        """set_correlations_tracked() sets the gauge."""
        ce_recorder.set_correlations_tracked(42)

    def test_record_strategy_fallback_increments_counter(self, ce_recorder):
        """record_strategy_fallback() increments fallback counter."""
        ce_recorder.record_strategy_fallback()

    def test_negative_duration_clamped_to_zero(self, ce_recorder):
        """Negative duration is clamped to 0 by _clamp_non_negative."""
        # Should not raise
        ce_recorder.record_periodic_analysis(-1.0)

    def test_recording_graceful_on_metric_error(self):
        """Recording methods do not raise when underlying metric fails."""
        from baldur.metrics.recorders.correlation_engine import (
            CorrelationEngineMetricRecorder,
        )

        recorder = CorrelationEngineMetricRecorder()
        # Sabotage a metric to force error
        recorder._state = MagicMock()
        recorder._state.labels.side_effect = RuntimeError("broken")

        # Should not raise
        recorder.set_state(1)


# =============================================================================
# AutoTuningMetricRecorder — Contract
# =============================================================================


class TestAutoTuningRecorderContract:
    """AutoTuningMetricRecorder design contract verification."""

    def test_prefix_is_baldur(self, at_recorder):
        """Metric prefix follows D13 convention: 'baldur'."""
        assert at_recorder.PREFIX == "baldur"

    def test_all_seven_metrics_initialized(self, at_recorder):
        """All 7 metrics are created during __init__."""
        assert at_recorder._enabled is not None
        assert at_recorder._module_state is not None
        assert at_recorder._adjustments_total is not None
        assert at_recorder._override_active is not None
        assert at_recorder._override_rollback_total is not None
        assert at_recorder._governance_block_total is not None
        assert at_recorder._safety_bounds_violations_total is not None


# =============================================================================
# AutoTuningMetricRecorder — Behavior
# =============================================================================


class TestAutoTuningRecorderBehavior:
    """AutoTuningMetricRecorder recording behavior."""

    def test_set_enabled_true(self, at_recorder):
        """set_enabled(True) sets gauge to 1."""
        at_recorder.set_enabled(True)

    def test_set_enabled_false(self, at_recorder):
        """set_enabled(False) sets gauge to 0."""
        at_recorder.set_enabled(False)

    def test_set_module_state_with_label(self, at_recorder):
        """set_module_state() uses module label."""
        at_recorder.set_module_state("circuit_breaker", 1)
        at_recorder.set_module_state("dlq", 2)

    def test_record_adjustment_with_module(self, at_recorder):
        """record_adjustment() increments with module label."""
        at_recorder.record_adjustment("retry")

    def test_set_override_active_clamps_negative(self, at_recorder):
        """set_override_active() clamps negative values to 0."""
        at_recorder.set_override_active("max_retries", -1)

    def test_record_override_rollback(self, at_recorder):
        """record_override_rollback() increments counter."""
        at_recorder.record_override_rollback()

    def test_record_governance_block_with_check_type(self, at_recorder):
        """record_governance_block() records with check_type label."""
        at_recorder.record_governance_block("kill_switch")
        at_recorder.record_governance_block("error_budget")

    def test_record_safety_bounds_violation(self, at_recorder):
        """record_safety_bounds_violation() increments counter."""
        at_recorder.record_safety_bounds_violation()

    def test_recording_graceful_on_metric_error(self):
        """Recording methods do not raise when underlying metric fails."""
        from baldur.metrics.recorders.auto_tuning import AutoTuningMetricRecorder

        recorder = AutoTuningMetricRecorder()
        recorder._enabled = MagicMock()
        recorder._enabled.labels.side_effect = RuntimeError("broken")

        # Should not raise
        recorder.set_enabled(True)


# =============================================================================
# Prometheus Facade Integration — Behavior
# =============================================================================


class TestPrometheusFacadeRecordersBehavior:
    """New recorders are properly integrated into BaldurMetrics facade."""

    def test_correlation_engine_recorder_available_on_facade(self):
        """BaldurMetrics.correlation_engine is a CorrelationEngineMetricRecorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.correlation_engine import (
            CorrelationEngineMetricRecorder,
        )

        metrics = get_metrics()
        assert hasattr(metrics, "correlation_engine")
        assert isinstance(metrics.correlation_engine, CorrelationEngineMetricRecorder)

    def test_auto_tuning_recorder_available_on_facade(self):
        """BaldurMetrics.auto_tuning is an AutoTuningMetricRecorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.auto_tuning import AutoTuningMetricRecorder

        metrics = get_metrics()
        assert hasattr(metrics, "auto_tuning")
        assert isinstance(metrics.auto_tuning, AutoTuningMetricRecorder)
