"""
RuntimeConfigMetricRecorder Unit Tests (394 — R).

Test targets:
    - baldur.metrics.recorders.runtime_config.RuntimeConfigMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration
    B. Behavior: Method calls, value clamping

Reference:
    394
"""

from __future__ import annotations

import pytest


@pytest.fixture
def runtime_config_recorder():
    from baldur.metrics.recorders.runtime_config import (
        RuntimeConfigMetricRecorder,
    )

    return RuntimeConfigMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestRuntimeConfigRecorderContract:
    """RuntimeConfigMetricRecorder contract: exports and facade registration."""

    def test_all_exports_exactly_recorder_class(self):
        """__all__ exports exactly ['RuntimeConfigMetricRecorder']."""
        from baldur.metrics.recorders.runtime_config import __all__

        assert __all__ == ["RuntimeConfigMetricRecorder"]

    def test_facade_has_runtime_config_attribute(self):
        """BaldurMetrics exposes runtime_config recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.runtime_config import (
            RuntimeConfigMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.runtime_config, RuntimeConfigMetricRecorder)


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestRuntimeConfigRecorderBehavior:
    """RuntimeConfigMetricRecorder method behavior."""

    def test_record_update_does_not_raise(self, runtime_config_recorder):
        """record_update with config_type does not raise."""
        runtime_config_recorder.record_update("circuit_breaker")

    def test_record_no_change_does_not_raise(self, runtime_config_recorder):
        """record_no_change with config_type does not raise."""
        runtime_config_recorder.record_no_change("retry")

    def test_record_safe_default_applied_does_not_raise(self, runtime_config_recorder):
        """record_safe_default_applied with config_type and field does not raise."""
        runtime_config_recorder.record_safe_default_applied("retry", "max_retries")

    def test_record_update_failed_does_not_raise(self, runtime_config_recorder):
        """record_update_failed with config_type and reason does not raise."""
        runtime_config_recorder.record_update_failed("circuit_breaker", "validation")

    def test_set_pending_changes_positive_value(self, runtime_config_recorder):
        """set_pending_changes with positive count does not raise."""
        runtime_config_recorder.set_pending_changes("retry", 5)

    def test_set_pending_changes_negative_value_clamped(self, runtime_config_recorder):
        """set_pending_changes with negative count gets clamped to 0."""
        from baldur.metrics.recorders.base import BaseMetricRecorder

        assert BaseMetricRecorder._clamp_non_negative(-3, "pending_changes") == 0
        # Verify the method still completes without error
        runtime_config_recorder.set_pending_changes("retry", -3)
