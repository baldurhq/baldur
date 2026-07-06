"""
ShutdownMetricRecorder Unit Tests (394 — R7).

Test targets:
    - baldur.metrics.recorders.shutdown.ShutdownMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: Phase map, __all__ exports (DD-5, DD-6)
    B. Behavior: Fail-open, convenience function delegation, facade access

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.core.shutdown_coordinator import ShutdownPhase
from baldur.metrics.recorders.shutdown import _PHASE_MAP


@pytest.fixture
def shutdown_recorder():
    from baldur.metrics.recorders.shutdown import ShutdownMetricRecorder

    return ShutdownMetricRecorder()


# =============================================================================
# A. Contract Tests — Phase Map (DD-6)
# =============================================================================


class TestShutdownRecorderContract:
    """R7: ShutdownMetricRecorder phase map contract values."""

    def test_phase_map_values(self):
        """PHASE_MAP: running=0, draining=1, terminating=2, terminated=3."""

        assert _PHASE_MAP == {
            "running": 0,
            "draining": 1,
            "terminating": 2,
            "terminated": 3,
        }

    def test_exports_five_convenience_functions(self):
        """__all__ includes class + 5 convenience functions."""
        from baldur.metrics.recorders.shutdown import __all__

        assert "ShutdownMetricRecorder" in __all__
        assert "set_shutdown_phase" in __all__
        assert "record_drain_duration" in __all__
        assert "record_drained" in __all__
        assert "record_aborted" in __all__
        assert "record_shutdown_initiated" in __all__


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestShutdownRecorderBehavior:
    """R7: ShutdownMetricRecorder method behavior."""

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    @pytest.mark.parametrize(
        "phase_member", list(ShutdownPhase), ids=[m.value for m in ShutdownPhase]
    )
    def test_set_phase_maps_input_to_gauge_value(
        self, shutdown_recorder, phase_member, input_form
    ):
        """set_phase maps enum members AND plain value strings to the mapped int (596 D1).

        Production call sites pass ShutdownPhase members directly (DD-6
        enum-direct pass-through); the gauge must read the mapped int,
        not the silent default 0.
        """
        phase_input = (
            phase_member if input_form == "enum_member" else phase_member.value
        )

        shutdown_recorder.set_phase(phase_input)

        assert shutdown_recorder._phase._value.get() == _PHASE_MAP[phase_member.value]

    @pytest.mark.parametrize(
        "unknown_input",
        ["nonexistent", 42],
        ids=["typo_string", "non_string_hashable"],
    )
    def test_set_phase_unknown_hashable_input_sets_zero_and_warns(
        self, shutdown_recorder, unknown_input
    ):
        """Unknown hashable input maps to 0 AND emits the unmapped-value WARNING (596 D1)."""
        # Given — a non-zero gauge so the reset to 0 is observable
        shutdown_recorder.set_phase(ShutdownPhase.DRAINING)

        # When
        with capture_logs() as logs:
            shutdown_recorder.set_phase(unknown_input)

        # Then — fail-open to 0, but never silently
        assert shutdown_recorder._phase._value.get() == 0
        events = [
            e for e in logs if e.get("event") == "metrics.set_shutdown_phase_failed"
        ]
        assert len(events) == 1
        assert events[0]["reason"] == "unmapped_value"
        assert events[0]["log_level"] == "warning"

    def test_record_drain_duration_observes_value_in_histogram_sum(
        self, shutdown_recorder
    ):
        """record_drain_duration adds the duration to the histogram sum."""
        before = shutdown_recorder._drain_duration._sum.get()

        shutdown_recorder.record_drain_duration(15.5)

        after = shutdown_recorder._drain_duration._sum.get()
        assert after - before == pytest.approx(15.5)

    def test_record_drained_increments_counter_by_count(self, shutdown_recorder):
        """record_drained(count) increments the drained counter by exactly count."""
        before = shutdown_recorder._drained_total._value.get()

        shutdown_recorder.record_drained(42)

        assert shutdown_recorder._drained_total._value.get() - before == 42

    def test_record_aborted_increments_counter_by_count(self, shutdown_recorder):
        """record_aborted(count) increments the aborted counter by exactly count."""
        before = shutdown_recorder._aborted_total._value.get()

        shutdown_recorder.record_aborted(3)

        assert shutdown_recorder._aborted_total._value.get() - before == 3

    def test_record_initiated_increments_counter_by_one(self, shutdown_recorder):
        """record_initiated increments the initiation counter by 1."""
        before = shutdown_recorder._initiations_total._value.get()

        shutdown_recorder.record_initiated()

        assert shutdown_recorder._initiations_total._value.get() - before == 1

    def test_record_initiated_repeated_calls_accumulate(self, shutdown_recorder):
        """Repeated record_initiated calls accumulate (counter is monotonic)."""
        before = shutdown_recorder._initiations_total._value.get()

        for _ in range(3):
            shutdown_recorder.record_initiated()

        assert shutdown_recorder._initiations_total._value.get() - before == 3


# =============================================================================
# C. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestShutdownConvenienceFunctionsBehavior:
    """DD-7: Shutdown convenience functions delegate to lazy recorder."""

    def test_convenience_delegates_to_recorder(self):
        """set_shutdown_phase delegates to recorder.set_phase."""
        from baldur.metrics.recorders.shutdown import set_shutdown_phase

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.shutdown._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            set_shutdown_phase("draining")
        mock_recorder.set_phase.assert_called_once_with("draining")

    def test_record_shutdown_initiated_delegates_to_recorder(self):
        """record_shutdown_initiated delegates to recorder.record_initiated."""
        from baldur.metrics.recorders.shutdown import record_shutdown_initiated

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.shutdown._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_shutdown_initiated()
        mock_recorder.record_initiated.assert_called_once_with()


# =============================================================================
# D. Contract Tests — Facade Registration
# =============================================================================


class TestShutdownFacadeRegistrationContract:
    """ShutdownMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_shutdown_attribute(self):
        """BaldurMetrics exposes shutdown recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.shutdown import ShutdownMetricRecorder

        m = get_metrics()
        assert isinstance(m.shutdown, ShutdownMetricRecorder)
