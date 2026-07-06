"""WAL drift-metric gauges for v1.0 lifecycle hygiene (484 D1).

Covers:
- ``baldur_wal_total_files`` and ``baldur_wal_current_size_bytes`` gauges
  registered in :mod:`baldur.metrics.drift_metrics`.
- ``update_wal_total_files()`` and ``update_wal_current_size_bytes()``
  helper functions — gauge mutators called by the periodic refresh task.
- ``__all__`` exports the new gauges and helpers, matching the existing
  WAL gauge/helper export pattern in :mod:`baldur.metrics.drift_metrics`.

Reference: ``484`` D1.
"""

from __future__ import annotations

import pytest

from baldur.metrics import drift_metrics


@pytest.fixture(autouse=True)
def _skip_when_prometheus_unavailable():
    """The drift_metrics module no-ops when ``prometheus_client`` is missing.

    The tests below exercise gauge values via ``_value.get()``, which only
    exists when Prometheus is installed, so we skip the suite cleanly when
    it is not.
    """
    if not drift_metrics.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")


# =============================================================================
# Contract — gauge presence
# =============================================================================


class TestWalDriftGaugeContract:
    """484 D1: New WAL gauges are registered as module-level attributes."""

    def test_wal_total_files_gauge_exists(self):
        """``wal_total_files`` is registered (not None)."""
        assert drift_metrics.wal_total_files is not None

    def test_wal_current_size_bytes_gauge_exists(self):
        """``wal_current_size_bytes`` is registered (not None)."""
        assert drift_metrics.wal_current_size_bytes is not None

    # 525 D4: xdist state_leak — prometheus default REGISTRY races with
    # sibling tests' metric registration under -n 6 (project_xdist_isolation
    # pattern).
    @pytest.mark.flaky_quarantine(
        issue="525", first_seen="2026-05-20", category="state_leak"
    )
    def test_wal_gauge_metric_names(self):
        """Exposed metric names follow ``baldur_wal_*`` convention."""
        from prometheus_client import REGISTRY

        assert "baldur_wal_total_files" in REGISTRY._names_to_collectors, (
            "wal_total_files must be registered with the canonical name"
        )
        assert "baldur_wal_current_size_bytes" in REGISTRY._names_to_collectors, (
            "wal_current_size_bytes must be registered with the canonical name"
        )


# =============================================================================
# Behavior — helper functions update the gauge value
# =============================================================================


class TestWalDriftHelperBehavior:
    """484 D1: Update helpers mutate the corresponding gauge value."""

    def test_update_wal_total_files_sets_gauge(self):
        """``update_wal_total_files(N)`` writes N onto the gauge."""
        drift_metrics.update_wal_total_files(0)  # baseline
        drift_metrics.update_wal_total_files(7)

        assert drift_metrics.wal_total_files._value.get() == 7

    def test_update_wal_current_size_bytes_sets_gauge(self):
        """``update_wal_current_size_bytes(N)`` writes N onto the gauge."""
        drift_metrics.update_wal_current_size_bytes(0)  # baseline
        drift_metrics.update_wal_current_size_bytes(1024 * 1024)

        assert drift_metrics.wal_current_size_bytes._value.get() == 1024 * 1024

    def test_update_helpers_are_idempotent_on_repeated_set(self):
        """Calling the same helper twice with the same value is a no-op."""
        drift_metrics.update_wal_total_files(3)
        drift_metrics.update_wal_total_files(3)

        assert drift_metrics.wal_total_files._value.get() == 3

    def test_update_helpers_overwrite_previous_value(self):
        """Gauge is set, not incremented — last write wins."""
        drift_metrics.update_wal_total_files(10)
        drift_metrics.update_wal_total_files(2)

        assert drift_metrics.wal_total_files._value.get() == 2
