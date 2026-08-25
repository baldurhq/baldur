"""Trip degraded-mode recorder — the marker for a relaxed trip contract.

``baldur_circuit_breaker_trip_degraded_mode_total`` marks the CLOSED->OPEN
automatic trip that fell back to L1 because the store was unhealthy, timed
out, raised, or answered with a state the routing does not recognize. While
the counter is moving for a service, two guarantees the atomic path carries —
one emission per transition, and an immediate read showing OPEN — are relaxed
to per-worker best-effort for it.

That makes the counter load-bearing rather than decorative: it is the only
signal distinguishing a cluster-wide trip from a local one, so its name, its
label set, and the fact that it fires at all are contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import Counter

from baldur.metrics.recorders import circuit_breaker as recorder_module
from baldur.metrics.recorders.circuit_breaker import (
    CBMetricRecorder,
    record_trip_degraded_mode,
    reset_blocked_recorder,
)


@pytest.fixture(autouse=True)
def _reset_cb_recorder_sticky_state():
    reset_blocked_recorder()
    yield
    reset_blocked_recorder()


# =============================================================================
# Contract — Prometheus surface (metric name + label tuple)
# =============================================================================


class TestTripDegradedModeMetricContract:
    """Hardcoded name + label tuple."""

    def test_metric_name_and_labels(self):
        recorder = CBMetricRecorder()

        # prometheus_client strips a Counter's trailing ``_total`` from its
        # internal ``_name`` (the suffix reappears on scrape).
        assert (
            recorder._trip_degraded_mode_total._name
            == "baldur_circuit_breaker_trip_degraded_mode"
        )
        assert tuple(recorder._trip_degraded_mode_total._labelnames) == ("service",)

    def test_module_exports_shortcut(self):
        from baldur.metrics.recorders import circuit_breaker

        assert "record_trip_degraded_mode" in circuit_breaker.__all__

    def test_counter_is_distinct_from_the_open_check_degraded_counter(self):
        # The two degraded lanes describe different transitions and must not
        # collapse into one series — an operator reading it needs to know
        # which primitive fell back.
        recorder = CBMetricRecorder()

        assert (
            recorder._trip_degraded_mode_total._name
            != recorder._open_check_degraded_mode_total._name
        )


# =============================================================================
# Behavior — recorder dispatch
# =============================================================================


class TestTripDegradedModeRecorderBehavior:
    """``record_trip_degraded_mode`` forwards the service label to inc()."""

    def test_dispatches_with_service_label(self):
        recorder = CBMetricRecorder()
        recorder._trip_degraded_mode_total = MagicMock(spec=Counter)

        recorder.record_trip_degraded_mode("payment_api")

        recorder._trip_degraded_mode_total.labels.assert_called_once_with(
            service="payment_api"
        )
        recorder._trip_degraded_mode_total.labels.return_value.inc.assert_called_once()

    def test_swallows_exceptions(self):
        """A broken metric must never break the trip path."""
        recorder = CBMetricRecorder()
        recorder._trip_degraded_mode_total = MagicMock(spec=Counter)
        recorder._trip_degraded_mode_total.labels.side_effect = RuntimeError(
            "metric broken"
        )

        # Must not raise.
        recorder.record_trip_degraded_mode("svc")


# =============================================================================
# Behavior — module-level shortcut sticky-cache parity
# =============================================================================


class TestTripDegradedModeShortcutBehavior:
    """The shortcut honors the shared ``_cb_recorder`` sticky-flag cache."""

    def test_none_recorder_is_noop(self):
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        # Must not raise even though the cached recorder is unavailable.
        record_trip_degraded_mode("svc")

    def test_valid_recorder_delegates(self):
        fake_recorder = MagicMock(spec=CBMetricRecorder)
        recorder_module._cb_recorder = fake_recorder

        record_trip_degraded_mode("payment_api")

        fake_recorder.record_trip_degraded_mode.assert_called_once_with("payment_api")

    def test_uses_sticky_fast_path(self):
        """Sticky flag short-circuits ``get_metrics`` re-import after a prior
        failure — the trip path must not pay an import per fallback.
        """
        recorder_module._cb_recorder_init_failed = True

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            record_trip_degraded_mode("svc")

        mock_get.assert_not_called()
