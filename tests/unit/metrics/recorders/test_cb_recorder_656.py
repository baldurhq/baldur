"""656 — open-check degraded-mode + peer-propagation recorder tests.

Locks in two new CB metric surfaces:

- ``baldur_circuit_breaker_open_check_degraded_mode_total`` +
  ``record_open_check_degraded_mode`` (D7): non-zero whenever the
  L2-authoritative open-check in ``record_failure_with_open_check`` falls
  back to L1 (L2 unhealthy / timeout / exception / stale-L2 routing). The
  failure-side mirror of the #498 close-check degraded counter.
- ``baldur_circuit_breaker_peer_propagation_total`` +
  ``record_peer_propagation`` (D5): a peer worker's CB OPEN/CLOSED
  transition applied to this worker's L1. ``outcome=applied`` ALSO refreshes
  the ``circuit_breaker_state`` gauge (R6) — the repo-level peer apply
  bypasses the service ``on_state_changed`` metric path, so without this the
  gauge would lie (report closed while the peer rejects). ``outcome=noop``
  records the counter only.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.recorders import circuit_breaker as recorder_module
from baldur.metrics.recorders.circuit_breaker import (
    CBMetricRecorder,
    record_open_check_degraded_mode,
    record_peer_propagation,
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


class TestOpenCheckDegradedModeContract:
    """Hardcoded name + label tuple per 656 D7."""

    def test_metric_name_and_labels(self):
        recorder = CBMetricRecorder()

        # prometheus_client strips the trailing ``_total`` from a Counter's
        # internal ``_name`` (suffix reappended on scrape).
        assert (
            recorder._open_check_degraded_mode_total._name
            == "baldur_circuit_breaker_open_check_degraded_mode"
        )
        assert tuple(recorder._open_check_degraded_mode_total._labelnames) == (
            "service",
        )

    def test_module_exports_shortcut(self):
        from baldur.metrics.recorders import circuit_breaker

        assert "record_open_check_degraded_mode" in circuit_breaker.__all__


class TestPeerPropagationContract:
    """Hardcoded name + label tuple per 656 D5."""

    def test_metric_name_and_labels(self):
        recorder = CBMetricRecorder()

        assert (
            recorder._peer_propagation_total._name
            == "baldur_circuit_breaker_peer_propagation"
        )
        assert tuple(recorder._peer_propagation_total._labelnames) == (
            "service",
            "to_state",
            "outcome",
        )

    def test_module_exports_shortcut(self):
        from baldur.metrics.recorders import circuit_breaker

        assert "record_peer_propagation" in circuit_breaker.__all__


# =============================================================================
# Behavior — open-check degraded-mode recorder
# =============================================================================


class TestOpenCheckDegradedModeBehavior:
    """``record_open_check_degraded_mode`` forwards service label to inc()."""

    def test_dispatches_with_service_label(self):
        recorder = CBMetricRecorder()
        recorder._open_check_degraded_mode_total = MagicMock()

        recorder.record_open_check_degraded_mode("payment_api")

        recorder._open_check_degraded_mode_total.labels.assert_called_once_with(
            service="payment_api"
        )
        recorder._open_check_degraded_mode_total.labels.return_value.inc.assert_called_once()

    def test_swallows_exceptions(self):
        """Metric failures must never break the open-check hot path."""
        recorder = CBMetricRecorder()
        recorder._open_check_degraded_mode_total = MagicMock()
        recorder._open_check_degraded_mode_total.labels.side_effect = RuntimeError(
            "metric broken"
        )

        # Must not raise.
        recorder.record_open_check_degraded_mode("svc")


# =============================================================================
# Behavior — peer-propagation recorder (counter + conditional gauge refresh)
# =============================================================================


class TestPeerPropagationMetric:
    """``record_peer_propagation`` — counter always, gauge only on applied."""

    def test_applied_increments_counter_and_refreshes_gauge(self):
        recorder = CBMetricRecorder()
        recorder._peer_propagation_total = MagicMock()

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_peer_propagation("svc", "open", "applied")

        recorder._peer_propagation_total.labels.assert_called_once_with(
            service="svc", to_state="open", outcome="applied"
        )
        recorder._peer_propagation_total.labels.return_value.inc.assert_called_once()
        # R6: applied refreshes the cb_state gauge to the new state. The
        # non-composite name resolves to (service, cell_id="") unchanged.
        mock_set_state.assert_called_once_with("svc", "open", cell_id="")

    def test_applied_composite_name_refreshes_canonical_gauge_series(self):
        # Regression: a cell-based composite CB name (service::cell_id) must
        # refresh the canonical (base_service, cell_id) gauge series, not a
        # phantom (composite, cell_id="") series — otherwise the canonical
        # series stays stale and R6's gauge-must-not-lie fix is defeated for
        # cell-based deployments under propagation.
        recorder = CBMetricRecorder()
        recorder._peer_propagation_total = MagicMock()

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_peer_propagation("payment::cell-1", "open", "applied")

        mock_set_state.assert_called_once_with("payment", "open", cell_id="cell-1")

    def test_noop_increments_counter_only_no_gauge_change(self):
        recorder = CBMetricRecorder()
        recorder._peer_propagation_total = MagicMock()

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_peer_propagation("svc", "open", "noop")

        recorder._peer_propagation_total.labels.assert_called_once_with(
            service="svc", to_state="open", outcome="noop"
        )
        recorder._peer_propagation_total.labels.return_value.inc.assert_called_once()
        # A no-op re-apply must NOT touch the gauge.
        mock_set_state.assert_not_called()

    def test_swallows_exceptions(self):
        recorder = CBMetricRecorder()
        recorder._peer_propagation_total = MagicMock()
        recorder._peer_propagation_total.labels.side_effect = RuntimeError(
            "metric broken"
        )

        # Must not raise.
        recorder.record_peer_propagation("svc", "open", "applied")


# =============================================================================
# Behavior — module-level shortcut sticky-cache parity
# =============================================================================


class TestOpenCheckDegradedModeShortcutBehavior:
    """The module-level ``record_open_check_degraded_mode`` honors the
    shared ``_cb_recorder`` sticky-flag cache (no per-call import re-run).
    """

    def test_none_recorder_is_noop(self):
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        # Must not raise even though the cached recorder is unavailable.
        record_open_check_degraded_mode("svc")

    def test_valid_recorder_delegates(self):
        fake_recorder = MagicMock()
        recorder_module._cb_recorder = fake_recorder

        record_open_check_degraded_mode("payment_api")

        fake_recorder.record_open_check_degraded_mode.assert_called_once_with(
            "payment_api"
        )

    def test_uses_sticky_fast_path(self):
        """Sticky flag short-circuits ``get_metrics`` re-import after a prior failure."""
        recorder_module._cb_recorder_init_failed = True

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            record_open_check_degraded_mode("svc")

        mock_get.assert_not_called()


class TestPeerPropagationShortcutBehavior:
    """The module-level ``record_peer_propagation`` honors the sticky cache."""

    def test_none_recorder_is_noop(self):
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        record_peer_propagation("svc", "open", "applied")

    def test_valid_recorder_delegates(self):
        fake_recorder = MagicMock()
        recorder_module._cb_recorder = fake_recorder

        record_peer_propagation("payment_api", "closed", "noop")

        fake_recorder.record_peer_propagation.assert_called_once_with(
            "payment_api", "closed", "noop"
        )
