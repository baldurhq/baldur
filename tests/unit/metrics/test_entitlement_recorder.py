"""
Unit tests for EntitlementMetricRecorder (427 D8).

Verification techniques:
- Contract: metric names, facade registration
- Side effects: gauge set calls
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.metrics.recorders.entitlement import EntitlementMetricRecorder


class TestEntitlementRecorderContract:
    """Metric name and facade registration contract (427 D8)."""

    def test_facade_has_entitlement_attribute(self):
        """BaldurMetrics facade exposes entitlement recorder."""
        from baldur.metrics.prometheus import BaldurMetrics

        metrics = BaldurMetrics(prefix="test")
        assert hasattr(metrics, "entitlement")
        assert isinstance(metrics.entitlement, EntitlementMetricRecorder)

    def test_status_gauge_name(self):
        """Status gauge name follows baldur_entitlement_status convention."""
        recorder = EntitlementMetricRecorder()
        assert recorder._status._name == "baldur_entitlement_status"

    def test_expiry_days_gauge_name(self):
        """Expiry days gauge name follows baldur_entitlement_expiry_days convention."""
        recorder = EntitlementMetricRecorder()
        assert recorder._expiry_days._name == "baldur_entitlement_expiry_days"


class TestEntitlementRecorderBehavior:
    """Gauge set behavior."""

    def test_set_status_calls_gauge(self):
        """set_status delegates to Prometheus gauge.set()."""
        recorder = EntitlementMetricRecorder()
        recorder._status = MagicMock()

        recorder.set_status(2)

        recorder._status.set.assert_called_once_with(2)

    def test_set_expiry_days_calls_gauge(self):
        """set_expiry_days delegates to Prometheus gauge.set()."""
        recorder = EntitlementMetricRecorder()
        recorder._expiry_days = MagicMock()

        recorder.set_expiry_days(15)

        recorder._expiry_days.set.assert_called_once_with(15)

    def test_set_status_swallows_exception(self):
        """set_status does not propagate gauge errors (fail-open)."""
        recorder = EntitlementMetricRecorder()
        recorder._status = MagicMock()
        recorder._status.set.side_effect = RuntimeError("gauge error")

        # Should not raise
        recorder.set_status(1)

    def test_set_expiry_days_swallows_exception(self):
        """set_expiry_days does not propagate gauge errors (fail-open)."""
        recorder = EntitlementMetricRecorder()
        recorder._expiry_days = MagicMock()
        recorder._expiry_days.set.side_effect = RuntimeError("gauge error")

        # Should not raise
        recorder.set_expiry_days(-5)
