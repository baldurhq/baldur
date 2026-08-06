"""
RuntimeConfigMetricRecorder Unit Tests (394 — R).

Test targets:
    - baldur.metrics.recorders.runtime_config.RuntimeConfigMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration
    B. Behavior: Method calls, value clamping, the convergence gauge

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


class TestInstalledFingerprintRecorderBehavior:
    """The fleet-convergence gauge.

    Two processes serving byte-identical values publish the same number whatever
    order or how many installs got them there, so any spread across the fleet is
    real divergence — which is why the value is a content hash and not the
    per-process install counter.
    """

    def test_set_installed_fingerprint_does_not_raise(self, runtime_config_recorder):
        runtime_config_recorder.set_installed_fingerprint("circuit_breaker", 1234567)

    def test_a_failing_collector_never_reaches_the_caller(
        self, runtime_config_recorder
    ):
        """A metric must never break the tick that publishes it."""
        from unittest.mock import patch

        with patch.object(
            runtime_config_recorder,
            "_installed_fingerprint",
            **{"labels.side_effect": RuntimeError("registry unavailable")},
        ):
            runtime_config_recorder.set_installed_fingerprint("circuit_breaker", 42)

    def test_the_gauge_carries_only_the_config_type_label(
        self, runtime_config_recorder
    ):
        """Label cardinality is bounded by the domain count, not by values."""
        from unittest.mock import MagicMock, patch

        gauge = MagicMock(spec=runtime_config_recorder._installed_fingerprint)
        with patch.object(runtime_config_recorder, "_installed_fingerprint", gauge):
            runtime_config_recorder.set_installed_fingerprint("retry", 99)

        gauge.labels.assert_called_once_with(config_type="retry")
        gauge.labels.return_value.set.assert_called_once_with(99)
