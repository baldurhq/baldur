"""
BulkheadMetricRecorder Unit Tests (644 — D3).

Test targets:
    - baldur.metrics.recorders.bulkhead.BulkheadMetricRecorder
    - Facade registration in BaldurMetrics

The 5 ``baldur_bulkhead_*`` series were relocated out of the PRO tree into this
OSS recorder so they register through the ``get_metrics()`` facade (G43-visible
without ``baldur_pro``), exactly like DLQ/Throttle/Canary. The PRO updater daemon
and every reject site write *through* this recorder.

Test Categories:
    A. Contract: 5-series names + label parity with the prior PRO defs
    B. Behavior: poll-path gauge writes, utilization boundary, rejection counter
    C. Contract: facade registration

Reference:
    644
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY


@pytest.fixture
def bulkhead_recorder():
    from baldur.metrics.recorders.bulkhead import BulkheadMetricRecorder

    return BulkheadMetricRecorder()


# =============================================================================
# A. Contract Tests — 5-Series Names + Label Parity (D3)
# =============================================================================


class TestBulkheadRecorderContract:
    """D3: the 5 series register with exact name/label parity with the prior PRO
    defs — ``active_count{bulkhead_name,bulkhead_type}`` + four ``{bulkhead_name}``.

    A label-set drift here is the two-definition footgun D3 exists to remove: a
    panel PromQL filter on the wrong label silently renders empty.
    """

    def test_active_count_registered_with_name_and_type_labels(self, bulkhead_recorder):
        """active_count carries both bulkhead_name and bulkhead_type."""
        assert "baldur_bulkhead_active_count" in REGISTRY._names_to_collectors
        assert bulkhead_recorder._active_count._labelnames == (
            "bulkhead_name",
            "bulkhead_type",
        )

    def test_max_concurrent_registered_with_name_label_only(self, bulkhead_recorder):
        """max_concurrent carries only bulkhead_name."""
        assert "baldur_bulkhead_max_concurrent" in REGISTRY._names_to_collectors
        assert bulkhead_recorder._max_concurrent._labelnames == ("bulkhead_name",)

    def test_rejected_total_registered_with_name_label_only(self, bulkhead_recorder):
        """rejected_total (counter) is registered under its full ``_total`` sample
        name with only bulkhead_name."""
        assert "baldur_bulkhead_rejected_total" in REGISTRY._names_to_collectors
        assert bulkhead_recorder._rejected_total._labelnames == ("bulkhead_name",)

    def test_utilization_percent_registered_with_name_label_only(
        self, bulkhead_recorder
    ):
        """utilization_percent carries only bulkhead_name."""
        assert "baldur_bulkhead_utilization_percent" in REGISTRY._names_to_collectors
        assert bulkhead_recorder._utilization_percent._labelnames == ("bulkhead_name",)

    def test_waiting_count_registered_with_name_label_only(self, bulkhead_recorder):
        """waiting_count carries only bulkhead_name."""
        assert "baldur_bulkhead_waiting_count" in REGISTRY._names_to_collectors
        assert bulkhead_recorder._waiting_count._labelnames == ("bulkhead_name",)

    def test_exports_recorder_class(self):
        """__all__ exposes the recorder class."""
        from baldur.metrics.recorders.bulkhead import __all__

        assert "BulkheadMetricRecorder" in __all__


# =============================================================================
# B. Behavior Tests — Poll-Path Gauge Writes (D3)
# =============================================================================


class TestBulkheadRecorderBehavior:
    """D3: update_metrics sets the state gauges; increment_rejected advances the
    counter monotonically."""

    @pytest.mark.parametrize("bulkhead_type", ["semaphore", "thread_pool"])
    def test_update_metrics_sets_state_gauges(self, bulkhead_recorder, bulkhead_type):
        """update_metrics writes active/max/waiting and the derived utilization.

        Utilization is computed in the recorder (active / max * 100), so a caller
        passing raw counts gets the right derived gauge value.
        """
        # Given — a distinct bulkhead per type so the shared-name gauges
        # (max/waiting/utilization) do not collide across parametrize runs
        name = f"svc_{bulkhead_type}"

        # When
        bulkhead_recorder.update_metrics(
            bulkhead_name=name,
            bulkhead_type=bulkhead_type,
            active_count=3,
            max_concurrent=10,
            waiting_count=2,
        )

        # Then — each gauge reflects the written / derived value
        assert (
            bulkhead_recorder._active_count.labels(
                bulkhead_name=name, bulkhead_type=bulkhead_type
            )._value.get()
            == 3
        )
        assert (
            bulkhead_recorder._max_concurrent.labels(bulkhead_name=name)._value.get()
            == 10
        )
        assert (
            bulkhead_recorder._waiting_count.labels(bulkhead_name=name)._value.get()
            == 2
        )
        # 3 / 10 * 100 = 30
        assert (
            bulkhead_recorder._utilization_percent.labels(
                bulkhead_name=name
            )._value.get()
            == 30.0
        )

    def test_update_metrics_zero_max_concurrent_resets_utilization_to_zero(
        self, bulkhead_recorder
    ):
        """max_concurrent=0 yields utilization 0 with no ZeroDivisionError.

        The recorder swallows exceptions, so a naive ZeroDivision check would be
        masked. Seeding a non-zero utilization first makes the boundary observable:
        if the division guard regressed, the swallowed exception would leave the
        gauge at 50.0 instead of resetting it to 0.
        """
        # Given — a non-zero utilization for this bulkhead
        bulkhead_recorder.update_metrics(
            bulkhead_name="boundary",
            bulkhead_type="semaphore",
            active_count=5,
            max_concurrent=10,
            waiting_count=0,
        )
        assert (
            bulkhead_recorder._utilization_percent.labels(
                bulkhead_name="boundary"
            )._value.get()
            == 50.0
        )

        # When — max_concurrent drops to 0 (fully drained / misconfigured)
        bulkhead_recorder.update_metrics(
            bulkhead_name="boundary",
            bulkhead_type="semaphore",
            active_count=0,
            max_concurrent=0,
            waiting_count=0,
        )

        # Then — utilization is 0 (guard taken), not the stale 50.0
        assert (
            bulkhead_recorder._utilization_percent.labels(
                bulkhead_name="boundary"
            )._value.get()
            == 0
        )

    def test_increment_rejected_advances_counter_monotonically(self, bulkhead_recorder):
        """increment_rejected adds exactly 1 per call to the per-bulkhead counter."""
        before = bulkhead_recorder._rejected_total.labels(
            bulkhead_name="rej"
        )._value.get()

        bulkhead_recorder.increment_rejected("rej")
        bulkhead_recorder.increment_rejected("rej")

        after = bulkhead_recorder._rejected_total.labels(
            bulkhead_name="rej"
        )._value.get()
        assert after - before == 2


# =============================================================================
# C. Contract Tests — Facade Registration (D3)
# =============================================================================


class TestBulkheadFacadeRegistrationContract:
    """D3: BulkheadMetricRecorder is registered on the metrics facade so the
    series exist without ``baldur_pro`` (G43-visible)."""

    def test_facade_has_bulkhead_attribute(self):
        """get_metrics() exposes a bulkhead recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.bulkhead import BulkheadMetricRecorder

        m = get_metrics()
        assert isinstance(m.bulkhead, BulkheadMetricRecorder)
