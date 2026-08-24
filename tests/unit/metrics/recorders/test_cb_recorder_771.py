"""771 — reject-path convergence recorder tests.

Locks in the CB metric surface the layered repository's convergence lane
reports through:

- ``baldur_circuit_breaker_reject_path_convergence_total{service,outcome}`` +
  ``record_reject_path_convergence``: one increment per contradiction the lane
  acted on, labelled with what it did about it.
- ``outcome=converged`` ALSO refreshes the ``circuit_breaker_state`` gauge, for
  the same reason the peer apply does: a repo-level L1 transition bypasses the
  service's ``on_state_changed`` metric path, so without the refresh the gauge
  keeps reporting the pre-convergence state on a worker that is admitting
  again — which reads to an operator as a stuck breaker. Every other outcome
  records the counter only; ``repaired`` in particular changed L2, not L1.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.metrics.recorders import circuit_breaker as recorder_module
from baldur.metrics.recorders.circuit_breaker import (
    CBMetricRecorder,
    record_reject_path_convergence,
    reset_blocked_recorder,
)

NON_CONVERGED_OUTCOMES = [
    "repaired",
    "repair_failed",
    "skipped_pinned",
    "skipped",
    "noop",
]


@pytest.fixture(autouse=True)
def _reset_cb_recorder_sticky_state():
    reset_blocked_recorder()
    yield
    reset_blocked_recorder()


# =============================================================================
# Contract — Prometheus surface (metric name + label tuple)
# =============================================================================


class TestRejectPathConvergenceContract:
    """Hardcoded name + label tuple."""

    def test_metric_name_and_labels(self):
        recorder = CBMetricRecorder()

        # prometheus_client strips the trailing ``_total`` from a Counter's
        # internal ``_name`` (suffix reappended on scrape).
        assert (
            recorder._reject_path_convergence_total._name
            == "baldur_circuit_breaker_reject_path_convergence"
        )
        assert tuple(recorder._reject_path_convergence_total._labelnames) == (
            "service",
            "outcome",
        )

    def test_module_exports_shortcut(self):
        from baldur.metrics.recorders import circuit_breaker

        assert "record_reject_path_convergence" in circuit_breaker.__all__


# =============================================================================
# Behavior — counter always, gauge only on converged
# =============================================================================


class TestRejectPathConvergenceMetric:
    """``record_reject_path_convergence`` — labels and the conditional gauge."""

    def test_converged_increments_counter_and_refreshes_gauge(self):
        recorder = CBMetricRecorder()
        recorder._reject_path_convergence_total = MagicMock(
            spec=recorder._reject_path_convergence_total
        )

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_reject_path_convergence("svc", "converged")

        recorder._reject_path_convergence_total.labels.assert_called_once_with(
            service="svc", outcome="converged"
        )
        recorder._reject_path_convergence_total.labels.return_value.inc.assert_called_once()
        mock_set_state.assert_called_once_with("svc", "closed", cell_id="")

    def test_converged_composite_name_refreshes_canonical_gauge_series(self):
        """A cell-based composite name must refresh the canonical
        ``(base_service, cell_id)`` series, not a phantom
        ``(composite, cell_id="")`` one — otherwise the canonical series stays
        stale and the gauge still lies for cell-based deployments.
        """
        recorder = CBMetricRecorder()
        recorder._reject_path_convergence_total = MagicMock(
            spec=recorder._reject_path_convergence_total
        )

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_reject_path_convergence("payment::cell-1", "converged")

        mock_set_state.assert_called_once_with("payment", "closed", cell_id="cell-1")

    @pytest.mark.parametrize("outcome", NON_CONVERGED_OUTCOMES)
    def test_other_outcomes_increment_counter_only(self, outcome):
        """L1's state did not change, so the gauge must not be rewritten."""
        recorder = CBMetricRecorder()
        recorder._reject_path_convergence_total = MagicMock(
            spec=recorder._reject_path_convergence_total
        )

        with patch.object(recorder, "set_state") as mock_set_state:
            recorder.record_reject_path_convergence("svc", outcome)

        recorder._reject_path_convergence_total.labels.assert_called_once_with(
            service="svc", outcome=outcome
        )
        recorder._reject_path_convergence_total.labels.return_value.inc.assert_called_once()
        mock_set_state.assert_not_called()

    def test_swallows_exceptions(self):
        """A broken metric must never surface out of the convergence task."""
        recorder = CBMetricRecorder()
        recorder._reject_path_convergence_total = MagicMock(
            spec=recorder._reject_path_convergence_total
        )
        recorder._reject_path_convergence_total.labels.side_effect = RuntimeError(
            "metric broken"
        )

        # Must not raise.
        recorder.record_reject_path_convergence("svc", "converged")


# =============================================================================
# Behavior — module-level shortcut sticky-cache parity
# =============================================================================


class TestRejectPathConvergenceShortcutBehavior:
    """The module-level shortcut honors the shared sticky-flag cache."""

    def test_none_recorder_is_noop(self):
        recorder_module._cb_recorder = None
        recorder_module._cb_recorder_init_failed = True

        # Must not raise even though the cached recorder is unavailable.
        record_reject_path_convergence("svc", "converged")

    def test_valid_recorder_delegates(self):
        fake_recorder = MagicMock(spec=CBMetricRecorder)
        recorder_module._cb_recorder = fake_recorder

        record_reject_path_convergence("payment_api", "repaired")

        fake_recorder.record_reject_path_convergence.assert_called_once_with(
            "payment_api", "repaired"
        )

    def test_uses_sticky_fast_path(self):
        """Sticky flag short-circuits ``get_metrics`` re-import after a prior
        failure — the task runs on a shared pool thread and must not pay an
        import attempt per outcome.
        """
        recorder_module._cb_recorder_init_failed = True

        with patch("baldur.metrics.prometheus.get_metrics") as mock_get:
            record_reject_path_convergence("svc", "noop")

        mock_get.assert_not_called()
