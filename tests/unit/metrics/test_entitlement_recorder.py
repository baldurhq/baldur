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


class _RecordingGauge:
    """Gauge stand-in that keeps every value written to it.

    A real object rather than a mock: the assertions below are about the
    sequence of values the setter writes, and an auto-generated attribute
    would make "the setter did nothing" indistinguishable from a pass.
    """

    def __init__(self) -> None:
        self.values: list[int] = []

    def set(self, value: int) -> None:
        self.values.append(value)


class _RaisingGauge:
    """Gauge stand-in whose every write fails, for the fail-open arm."""

    def set(self, value: int) -> None:
        raise RuntimeError("gauge error")


class TestRegistrationFailuresGaugeContract:
    """The third gauge — what an entitled boot actually registered.

    The two subscription gauges describe the licence. PRO registration is a
    sequence of individually fail-soft steps, so an entitled process can run
    with a capability silently on its OSS default; without a steady-state
    series that condition is visible only in one boot-time log line.
    """

    def test_registration_failures_gauge_name(self):
        """Gauge name follows the baldur_entitlement_* convention."""
        recorder = EntitlementMetricRecorder()
        assert (
            recorder._registration_failures._name
            == "baldur_entitlement_registration_failures"
        )


class TestRegistrationFailuresGaugeBehavior:
    """Set behavior, including the state (not counter) semantics."""

    def test_set_registration_failures_calls_gauge(self):
        """set_registration_failures delegates to Prometheus gauge.set()."""
        recorder = EntitlementMetricRecorder()
        gauge = _RecordingGauge()
        recorder._registration_failures = gauge

        recorder.set_registration_failures(2)

        assert gauge.values == [2]

    def test_a_clean_registration_clears_a_previous_count(self):
        """It is a state, not a counter: a clean re-register writes 0.

        Without the zero write, a process that recovered would keep alerting
        on the count some earlier registration left behind.
        """
        recorder = EntitlementMetricRecorder()
        gauge = _RecordingGauge()
        recorder._registration_failures = gauge

        recorder.set_registration_failures(3)
        recorder.set_registration_failures(0)

        assert gauge.values == [3, 0]

    def test_set_registration_failures_swallows_exception(self):
        """A gauge error never propagates into the boot path (fail-open)."""
        recorder = EntitlementMetricRecorder()
        recorder._registration_failures = _RaisingGauge()

        # Should not raise
        recorder.set_registration_failures(1)

    def test_module_setter_is_silent_when_no_recorder_is_wired(self, monkeypatch):
        """No metrics facade → the setter is a no-op, never a boot failure."""
        from baldur.metrics.recorders import entitlement as module

        monkeypatch.setattr(module, "_lazy_recorder", lambda: None)

        module.set_entitlement_registration_failures(1)
