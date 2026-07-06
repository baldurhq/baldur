"""476 — CBMetricRecorder contract tests for HALF_OPEN observability.

Three new metrics + one extended label, all per the doc D8/D10 spec:

- ``baldur_circuit_breaker_blocked_total{service, reason}`` — extended
  with the ``reason`` label so operators can split "rejected because
  state==OPEN" from "rejected because HALF_OPEN window is full"
  without a separate metric.
- ``baldur_circuit_breaker_half_open_degraded_mode_total{service}`` —
  L1-fallback acquires during a Redis outage (C1).
- ``baldur_circuit_breaker_half_open_stuck_recovery_total{service}`` —
  Lua auto-resets a stalled HALF_OPEN window (D8).
"""

from __future__ import annotations

from unittest.mock import MagicMock


class TestCBMetricRecorder476Contract:
    """Hardcoded names + label tuples — public Prometheus surface."""

    def test_blocked_total_metric_name_and_labels(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()

        # prometheus_client strips the trailing ``_total`` from a Counter's
        # internal ``_name`` attribute (the suffix is reappended on scrape).
        assert recorder._blocked_total._name == "baldur_circuit_breaker_blocked"
        assert tuple(recorder._blocked_total._labelnames) == ("service", "reason")

    def test_half_open_degraded_mode_metric_name_and_labels(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()

        assert (
            recorder._half_open_degraded_mode_total._name
            == "baldur_circuit_breaker_half_open_degraded_mode"
        )
        assert tuple(recorder._half_open_degraded_mode_total._labelnames) == (
            "service",
        )

    def test_half_open_stuck_recovery_metric_name_and_labels(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()

        assert (
            recorder._half_open_stuck_recovery_total._name
            == "baldur_circuit_breaker_half_open_stuck_recovery"
        )
        assert tuple(recorder._half_open_stuck_recovery_total._labelnames) == (
            "service",
        )


class TestCBMetricRecorder476Behavior:
    """Recorder methods forward to prometheus_client `labels(...).inc()`."""

    def test_record_blocked_dispatches_with_reason_label(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()
        recorder._blocked_total = MagicMock()

        recorder.record_blocked("svc", "half_open_full")

        recorder._blocked_total.labels.assert_called_once_with(
            service="svc", reason="half_open_full"
        )
        recorder._blocked_total.labels.return_value.inc.assert_called_once()

    def test_record_blocked_open_reason(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()
        recorder._blocked_total = MagicMock()

        recorder.record_blocked("svc", "open")

        recorder._blocked_total.labels.assert_called_once_with(
            service="svc", reason="open"
        )

    def test_record_half_open_degraded_mode_dispatches_with_service(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()
        recorder._half_open_degraded_mode_total = MagicMock()

        recorder.record_half_open_degraded_mode("svc")

        recorder._half_open_degraded_mode_total.labels.assert_called_once_with(
            service="svc"
        )
        recorder._half_open_degraded_mode_total.labels.return_value.inc.assert_called_once()

    def test_record_half_open_stuck_recovery_dispatches_with_service(self):
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()
        recorder._half_open_stuck_recovery_total = MagicMock()

        recorder.record_half_open_stuck_recovery("svc")

        recorder._half_open_stuck_recovery_total.labels.assert_called_once_with(
            service="svc"
        )
        recorder._half_open_stuck_recovery_total.labels.return_value.inc.assert_called_once()

    def test_record_blocked_swallows_exceptions(self):
        """Metric failures must never break the request hot path."""
        from baldur.metrics.recorders.circuit_breaker import CBMetricRecorder

        recorder = CBMetricRecorder()
        recorder._blocked_total = MagicMock()
        recorder._blocked_total.labels.side_effect = RuntimeError("metric broken")

        # Must not raise.
        recorder.record_blocked("svc", "open")
