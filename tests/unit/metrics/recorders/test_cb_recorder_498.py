"""498 D7 — close-check degraded-mode recorder tests.

Locks in the new ``baldur_circuit_breaker_close_check_degraded_mode_total``
counter + ``record_close_check_degraded_mode`` recorder method + module-
level shortcut. Mirrors the 476 D8/D10 observability pattern (see
``test_cb_recorder_476.py`` and ``test_cb_recorder_blocked_sticky.py``).

The counter goes non-zero whenever the L2-authoritative close-check in
``LayeredCircuitBreakerStateRepository.record_success_with_close_check``
falls back to L1 — covers L2 unhealthy, timeout, exception, and the
stale-L2 routing detection guard (D6 step 2). While non-zero, the cross-
process exactly-one CLOSED-emit contract is relaxed to <=1 per worker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.recorders import circuit_breaker as recorder_module
from baldur.metrics.recorders.circuit_breaker import (
    CBMetricRecorder,
    record_close_check_degraded_mode,
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


class TestCloseCheckDegradedModeContract:
    """Hardcoded name + label tuple per 498 D7."""

    def test_metric_name_and_labels(self):
        recorder = CBMetricRecorder()

        # prometheus_client strips the trailing ``_total`` from a Counter's
        # internal ``_name`` (suffix reappended on scrape).
        assert (
            recorder._close_check_degraded_mode_total._name
            == "baldur_circuit_breaker_close_check_degraded_mode"
        )
        assert tuple(recorder._close_check_degraded_mode_total._labelnames) == (
            "service",
        )

    def test_module_exports_shortcut(self):
        from baldur.metrics.recorders import circuit_breaker

        assert "record_close_check_degraded_mode" in circuit_breaker.__all__


# =============================================================================
# Behavior — recorder method dispatches to labels(...).inc()
# =============================================================================


class TestCloseCheckDegradedModeBehavior:
    """``record_close_check_degraded_mode`` forwards service label to inc()."""

    def test_dispatches_with_service_label(self):
        recorder = CBMetricRecorder()
        recorder._close_check_degraded_mode_total = MagicMock()

        recorder.record_close_check_degraded_mode("payment_api")

        recorder._close_check_degraded_mode_total.labels.assert_called_once_with(
            service="payment_api"
        )
        recorder._close_check_degraded_mode_total.labels.return_value.inc.assert_called_once()

    def test_swallows_exceptions(self):
        """Metric failures must never break the close-check hot path."""
        recorder = CBMetricRecorder()
        recorder._close_check_degraded_mode_total = MagicMock()
        recorder._close_check_degraded_mode_total.labels.side_effect = RuntimeError(
            "metric broken"
        )

        # Must not raise.
        recorder.record_close_check_degraded_mode("svc")


# =============================================================================
# Behavior — module-level shortcut sticky-cache parity
# =============================================================================


class TestCloseCheckDegradedModeShortcutBehavior:
    """The module-level ``record_close_check_degraded_mode`` honors the
    shared ``_cb_recorder`` sticky-flag cache (no per-call import re-run).
    """

    def test_none_recorder_is_noop(self):
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        # Must not raise even though the cached recorder is unavailable.
        record_close_check_degraded_mode("svc")

    def test_valid_recorder_delegates(self):
        fake_recorder = MagicMock()
        recorder_module._cb_recorder = fake_recorder

        record_close_check_degraded_mode("payment_api")

        fake_recorder.record_close_check_degraded_mode.assert_called_once_with(
            "payment_api"
        )

    def test_uses_sticky_fast_path(self):
        """Sticky flag short-circuits ``get_metrics`` re-import after a prior failure."""
        recorder_module._cb_recorder_init_failed = True

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            record_close_check_degraded_mode("svc")

        mock_get.assert_not_called()
