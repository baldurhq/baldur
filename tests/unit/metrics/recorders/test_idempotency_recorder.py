"""IdempotencyMetricRecorder unit tests (484 D4).

Test targets:
- :class:`baldur.metrics.recorders.idempotency.IdempotencyMetricRecorder`
- ``baldur_idempotency_check_total{result, domain}`` counter
- Recorder slot registration on ``BaldurMetrics`` (D10)

Result label values: ``cache_hit | db_hit | miss``.
Domain label values come from :class:`baldur.services.idempotency.models.IdempotencyDomain`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def idempotency_recorder():
    from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

    return IdempotencyMetricRecorder()


# =============================================================================
# Contract — recorder shape, exports, facade registration
# =============================================================================


class TestIdempotencyRecorderContract:
    """484 D4 / D10 contract: exports, facade slot, metric name."""

    def test_module_exports_recorder_class(self):
        """``__all__`` exposes ``IdempotencyMetricRecorder``."""
        from baldur.metrics.recorders.idempotency import __all__

        assert "IdempotencyMetricRecorder" in __all__

    def test_recorders_package_re_exports_recorder(self):
        """``baldur.metrics.recorders`` package surfaces the new recorder."""
        from baldur.metrics.recorders import IdempotencyMetricRecorder
        from baldur.metrics.recorders.idempotency import (
            IdempotencyMetricRecorder as Direct,
        )

        assert IdempotencyMetricRecorder is Direct

    def test_facade_has_idempotency_attribute(self):
        """484 D10: ``BaldurMetrics`` exposes an ``idempotency`` recorder slot."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        m = get_metrics()
        assert isinstance(m.idempotency, IdempotencyMetricRecorder)

    def test_check_total_metric_name_registered(self):
        """Counter is registered under the canonical name."""
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            pytest.skip("prometheus_client not installed")

        # Force construction in case the test ordering resets the registry.
        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        IdempotencyMetricRecorder()

        assert "baldur_idempotency_check_total" in REGISTRY._names_to_collectors, (
            "IdempotencyMetricRecorder must register baldur_idempotency_check_total"
        )


# =============================================================================
# Behavior — record_check() applies result + domain labels
# =============================================================================


class TestIdempotencyRecorderBehavior:
    """``record_check(result, domain)`` increments the labeled counter."""

    @pytest.mark.parametrize(
        "result", ["cache_hit", "db_hit", "miss"], ids=["cache_hit", "db_hit", "miss"]
    )
    @pytest.mark.parametrize(
        "domain",
        ["external_service", "chaos_experiment", "wal_recovery"],
        ids=["external_service", "chaos_experiment", "wal_recovery"],
    )
    def test_record_check_passes_result_and_domain_labels(
        self, idempotency_recorder, result, domain
    ):
        """``record_check`` forwards both labels to ``.labels(...).inc()``."""
        with patch.object(idempotency_recorder._check_total, "labels") as mock_labels:
            idempotency_recorder.record_check(result=result, domain=domain)

            mock_labels.assert_called_once_with(result=result, domain=domain)
            mock_labels.return_value.inc.assert_called_once_with()

    def test_record_check_swallows_label_exceptions(self, idempotency_recorder):
        """Metric backend failures must not break ``IdempotencyService.check()``."""
        with patch.object(
            idempotency_recorder._check_total,
            "labels",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — recorder swallows and the hot path continues.
            idempotency_recorder.record_check(result="miss", domain="external_service")

    def test_record_check_increments_real_counter(self, idempotency_recorder):
        """End-to-end: counter value bumps by 1 per record_check call."""
        try:
            from prometheus_client import REGISTRY  # noqa: F401
        except ImportError:
            pytest.skip("prometheus_client not installed")

        # Sample the labeled child before/after to avoid clashes with other
        # tests that may have incremented the same series in this session.
        labeled = idempotency_recorder._check_total.labels(
            result="cache_hit", domain="external_service"
        )
        before = labeled._value.get()
        idempotency_recorder.record_check(result="cache_hit", domain="external_service")
        after = labeled._value.get()

        assert after - before == 1


# =============================================================================
# Gate decision counter — 566 D9
# =============================================================================


class TestGateDecisionRecorderContract:
    """566 D9 contract: ``baldur_idempotency_gate_decision_total`` registration."""

    def test_gate_decision_total_metric_name_registered(self):
        """Counter is registered under the canonical name."""
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            pytest.skip("prometheus_client not installed")

        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        # Force construction in case test ordering reset the registry.
        IdempotencyMetricRecorder()

        assert (
            "baldur_idempotency_gate_decision_total" in REGISTRY._names_to_collectors
        ), (
            "IdempotencyMetricRecorder must register "
            "baldur_idempotency_gate_decision_total"
        )

    def test_decision_label_values_are_bounded_to_three(self):
        """Decision label cardinality is the closed ``IdempotencyDecision`` set (3).

        Documented as ``continue | skip | abort`` (3 series, bounded) — sourced
        from the enum, so the label can never widen accidentally.
        """
        from baldur.core.idempotency_gate import IdempotencyDecision

        assert {d.value for d in IdempotencyDecision} == {
            "continue",
            "skip",
            "abort",
        }


class TestGateDecisionRecorderBehavior:
    """``record_gate_decision(decision)`` increments the labeled counter."""

    @pytest.mark.parametrize(
        "decision", ["continue", "skip", "abort"], ids=["continue", "skip", "abort"]
    )
    def test_record_gate_decision_passes_decision_label(
        self, idempotency_recorder, decision
    ):
        """``record_gate_decision`` forwards the decision label to ``.inc()``."""
        with patch.object(
            idempotency_recorder._gate_decision_total, "labels"
        ) as mock_labels:
            idempotency_recorder.record_gate_decision(decision)

            mock_labels.assert_called_once_with(decision=decision)
            mock_labels.return_value.inc.assert_called_once_with()

    def test_record_gate_decision_swallows_label_exceptions(self, idempotency_recorder):
        """Metric backend failures must not break ``IdempotencyGate`` dedup."""
        with patch.object(
            idempotency_recorder._gate_decision_total,
            "labels",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — recorder swallows and the gate hot path continues.
            idempotency_recorder.record_gate_decision("continue")

    @pytest.mark.parametrize(
        "decision", ["continue", "skip", "abort"], ids=["continue", "skip", "abort"]
    )
    def test_record_gate_decision_increments_real_counter(
        self, idempotency_recorder, decision
    ):
        """End-to-end: counter value bumps by 1 per record_gate_decision call."""
        try:
            from prometheus_client import REGISTRY  # noqa: F401
        except ImportError:
            pytest.skip("prometheus_client not installed")

        labeled = idempotency_recorder._gate_decision_total.labels(decision=decision)
        before = labeled._value.get()
        idempotency_recorder.record_gate_decision(decision)
        after = labeled._value.get()

        assert after - before == 1


# =============================================================================
# Gate takeover counter — 673 D9
# =============================================================================


class TestGateTakeoverRecorderContract:
    """673 D9 contract: ``baldur_idempotency_gate_takeover_total`` registration.

    A takeover returns CONTINUE — indistinguishable from a fresh acquire on the
    decision counter — so it is metered on a SEPARATE counter (not a new label on
    the decision counter) with a bounded ``reason`` ∈ {failed, stale}.
    """

    def test_gate_takeover_total_metric_name_registered(self):
        """Counter is registered under the canonical name."""
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            pytest.skip("prometheus_client not installed")

        from baldur.metrics.recorders.idempotency import IdempotencyMetricRecorder

        # Force construction in case test ordering reset the registry.
        IdempotencyMetricRecorder()

        assert (
            "baldur_idempotency_gate_takeover_total" in REGISTRY._names_to_collectors
        ), (
            "IdempotencyMetricRecorder must register "
            "baldur_idempotency_gate_takeover_total"
        )

    def test_takeover_counter_is_distinct_from_decision_counter(
        self, idempotency_recorder
    ):
        """The takeover series is a separate counter object, so ``continue``'s
        meaning on the decision counter stays stable (not a widened label)."""
        assert (
            idempotency_recorder._gate_takeover_total
            is not idempotency_recorder._gate_decision_total
        )


class TestGateTakeoverRecorderBehavior:
    """``record_takeover(reason)`` increments the labeled takeover counter."""

    @pytest.mark.parametrize("reason", ["failed", "stale"], ids=["failed", "stale"])
    def test_record_takeover_passes_reason_label(self, idempotency_recorder, reason):
        """``record_takeover`` forwards the reason label to ``.inc()``."""
        with patch.object(
            idempotency_recorder._gate_takeover_total, "labels"
        ) as mock_labels:
            idempotency_recorder.record_takeover(reason)

            mock_labels.assert_called_once_with(reason=reason)
            mock_labels.return_value.inc.assert_called_once_with()

    def test_record_takeover_swallows_label_exceptions(self, idempotency_recorder):
        """Metric backend failures must not break the gate dedup hot path."""
        with patch.object(
            idempotency_recorder._gate_takeover_total,
            "labels",
            side_effect=RuntimeError("metrics down"),
        ):
            # No raise — recorder swallows and the gate hot path continues.
            idempotency_recorder.record_takeover("failed")

    @pytest.mark.parametrize("reason", ["failed", "stale"], ids=["failed", "stale"])
    def test_record_takeover_increments_real_counter(
        self, idempotency_recorder, reason
    ):
        """End-to-end: counter value bumps by 1 per record_takeover call."""
        try:
            from prometheus_client import REGISTRY  # noqa: F401
        except ImportError:
            pytest.skip("prometheus_client not installed")

        labeled = idempotency_recorder._gate_takeover_total.labels(reason=reason)
        before = labeled._value.get()
        idempotency_recorder.record_takeover(reason)
        after = labeled._value.get()

        assert after - before == 1
