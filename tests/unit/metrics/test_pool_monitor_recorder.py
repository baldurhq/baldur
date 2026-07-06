"""
PoolMetricRecorder Unit Tests (394 — R20).

Test targets:
    - baldur.metrics.recorders.pool_monitor.PoolMetricRecorder
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, facade registration
    B. Behavior: Method invocations, clamping guards

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def pool_recorder():
    from baldur.metrics.recorders.pool_monitor import PoolMetricRecorder

    return PoolMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestPoolMetricRecorderContract:
    """PoolMetricRecorder contract: exports, facade."""

    def test_all_exports_exactly_class(self):
        """__all__ exports exactly ['PoolMetricRecorder']."""
        from baldur.metrics.recorders.pool_monitor import __all__

        assert __all__ == ["PoolMetricRecorder"]

    def test_facade_has_pool_monitor_attribute(self):
        """BaldurMetrics exposes pool_monitor recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.pool_monitor import PoolMetricRecorder

        m = get_metrics()
        assert isinstance(m.pool_monitor, PoolMetricRecorder)


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestPoolMetricRecorderBehavior:
    """PoolMetricRecorder method behavior."""

    def test_set_health_status_valid_value(self, pool_recorder):
        """set_health_status with valid status_value does not raise."""
        pool_recorder.set_health_status("redis_main", 2)

    def test_set_health_status_negative_clamped(self, pool_recorder):
        """set_health_status with negative value gets clamped to 0."""
        with patch.object(pool_recorder, "_health_status", autospec=True) as mock_gauge:
            pool_recorder.set_health_status("redis_main", -1)
            mock_gauge.labels.return_value.set.assert_called_once()
            clamped_value = mock_gauge.labels.return_value.set.call_args[0][0]
            assert clamped_value == 0.0

    def test_set_utilization_valid_percent(self, pool_recorder):
        """set_utilization with 50.0 does not raise."""
        pool_recorder.set_utilization("pg_pool", 50.0)

    def test_set_utilization_over_100_clamped(self, pool_recorder):
        """set_utilization with value >100 gets clamped to 100."""
        with patch.object(pool_recorder, "_utilization", autospec=True) as mock_gauge:
            pool_recorder.set_utilization("pg_pool", 150.0)
            mock_gauge.labels.return_value.set.assert_called_once()
            clamped_value = mock_gauge.labels.return_value.set.call_args[0][0]
            assert clamped_value == 100.0

    def test_record_leak_detected_does_not_raise(self, pool_recorder):
        """record_leak_detected does not raise."""
        pool_recorder.record_leak_detected("redis_main")

    def test_record_close_leaked_does_not_raise(self, pool_recorder):
        """record_close_leaked does not raise."""
        pool_recorder.record_close_leaked("redis_main")

    def test_record_expand_does_not_raise(self, pool_recorder):
        """record_expand does not raise."""
        pool_recorder.record_expand("pg_pool")

    def test_record_circuit_break_does_not_raise(self, pool_recorder):
        """record_circuit_break does not raise."""
        pool_recorder.record_circuit_break("pg_pool")
