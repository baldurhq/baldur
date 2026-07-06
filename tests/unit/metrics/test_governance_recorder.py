"""
GovernanceMetricRecorder Unit Tests (408 — C5).

Test targets:
    - baldur.metrics.recorders.governance.GovernanceMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Reference:
    408
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def governance_recorder():
    from baldur.metrics.recorders.governance import GovernanceMetricRecorder

    return GovernanceMetricRecorder()


class TestGovernanceRecorderContract:
    """C5: GovernanceMetricRecorder export contract."""

    def test_exports_class_and_two_convenience_functions(self):
        """__all__ includes class + 2 convenience functions."""
        from baldur.metrics.recorders.governance import __all__

        assert "GovernanceMetricRecorder" in __all__
        assert "record_break_glass_activated" in __all__
        assert "record_governance_cache_operation" in __all__


class TestGovernanceRecorderBehavior:
    """C5: GovernanceMetricRecorder methods do not raise."""

    def test_record_break_glass_manual(self, governance_recorder):
        """record_break_glass with 'manual' reason does not raise."""
        governance_recorder.record_break_glass("manual")

    def test_record_break_glass_automatic(self, governance_recorder):
        """record_break_glass with 'automatic' reason does not raise."""
        governance_recorder.record_break_glass("automatic")

    def test_record_cache_operation_hit(self, governance_recorder):
        """record_cache_operation with get/hit does not raise."""
        governance_recorder.record_cache_operation("get", "hit")

    def test_record_cache_operation_invalidate(self, governance_recorder):
        """record_cache_operation with invalidate/success does not raise."""
        governance_recorder.record_cache_operation("invalidate", "success")


class TestGovernanceConvenienceFunctionsBehavior:
    """DD-7: Governance convenience functions delegate to lazy recorder."""

    def test_record_break_glass_activated_delegates(self):
        """record_break_glass_activated delegates to recorder.record_break_glass."""
        from baldur.metrics.recorders.governance import (
            record_break_glass_activated,
        )

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.governance._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_break_glass_activated("emergency")
        mock_recorder.record_break_glass.assert_called_once_with("emergency")

    def test_record_governance_cache_operation_delegates(self):
        """record_governance_cache_operation delegates to recorder."""
        from baldur.metrics.recorders.governance import (
            record_governance_cache_operation,
        )

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.governance._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_governance_cache_operation("set", "success")
        mock_recorder.record_cache_operation.assert_called_once_with("set", "success")


class TestGovernanceFacadeRegistrationContract:
    """GovernanceMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_governance_attribute(self):
        """BaldurMetrics exposes governance recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.governance import GovernanceMetricRecorder

        m = get_metrics()
        assert isinstance(m.governance, GovernanceMetricRecorder)


# =============================================================================
# 484 D3: PENDING 4-eyes approval gauges
# =============================================================================


class TestGovernancePendingApprovalGaugesBehavior:
    """484 D3: pending-approval visibility gauges + setter behavior.

    ``set_pending_approval_count(N)`` and ``set_oldest_pending_approval_age(s)``
    enable operator alerting on leaked approval requests (created → never
    acted on). Bounded cardinality (no labels).
    """

    def test_pending_approval_gauge_attributes_exist(self, governance_recorder):
        """Recorder exposes both gauge slots (D3 wiring)."""
        assert governance_recorder._pending_approval_gauge is not None
        assert governance_recorder._oldest_pending_age_gauge is not None

    @pytest.mark.parametrize(
        "count", [0, 1, 7, 100], ids=["none", "one", "few", "many"]
    )
    def test_set_pending_approval_count_writes_value(self, governance_recorder, count):
        """Setter forwards the count to gauge.set()."""
        with patch.object(
            governance_recorder._pending_approval_gauge, "set"
        ) as mock_set:
            governance_recorder.set_pending_approval_count(count)

            mock_set.assert_called_once_with(count)

    @pytest.mark.parametrize(
        "age_seconds",
        [0.0, 0.5, 60.0, 86_400.0],
        ids=["zero", "subsecond", "minute", "day"],
    )
    def test_set_oldest_pending_approval_age_writes_value(
        self, governance_recorder, age_seconds
    ):
        """Setter forwards the age in seconds to gauge.set()."""
        with patch.object(
            governance_recorder._oldest_pending_age_gauge, "set"
        ) as mock_set:
            governance_recorder.set_oldest_pending_approval_age(age_seconds)

            mock_set.assert_called_once_with(age_seconds)

    def test_setters_idempotent_on_repeated_call_with_same_value(
        self, governance_recorder
    ):
        """Repeated set() with same value is allowed (no exception)."""
        # No mock — exercise the real gauge to verify it tolerates re-set.
        governance_recorder.set_pending_approval_count(5)
        governance_recorder.set_pending_approval_count(5)
        governance_recorder.set_oldest_pending_approval_age(120.0)
        governance_recorder.set_oldest_pending_approval_age(120.0)

    def test_setters_swallow_gauge_exceptions(self, governance_recorder):
        """Gauge backend errors must not break the refresh task hot path."""
        with patch.object(
            governance_recorder._pending_approval_gauge,
            "set",
            side_effect=RuntimeError("metrics down"),
        ):
            governance_recorder.set_pending_approval_count(3)  # no raise

        with patch.object(
            governance_recorder._oldest_pending_age_gauge,
            "set",
            side_effect=RuntimeError("metrics down"),
        ):
            governance_recorder.set_oldest_pending_approval_age(60.0)  # no raise

    def test_pending_gauge_metric_names_registered(self):
        """484 D3 gauges expose canonical Prometheus names."""
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            pytest.skip("prometheus_client not installed")

        # Force recorder construction so names are registered with REGISTRY.
        from baldur.metrics.recorders.governance import GovernanceMetricRecorder

        GovernanceMetricRecorder()

        names = REGISTRY._names_to_collectors
        assert "baldur_governance_pending_approval_requests" in names
        assert "baldur_governance_oldest_pending_approval_age_seconds" in names
