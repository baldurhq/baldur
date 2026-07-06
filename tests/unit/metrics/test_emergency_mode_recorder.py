"""
EmergencyModeMetricRecorder Unit Tests (394 — R9).

Test targets:
    - baldur.metrics.recorders.emergency_mode.EmergencyModeMetricRecorder
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: Level map, __all__ exports (DD-5, DD-6)
    B. Behavior: Fail-open, convenience function delegation, facade access

Reference:
    394
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from baldur.metrics.recorders.emergency_mode import _LEVEL_MAP
from baldur.models.emergency import EmergencyLevel


@pytest.fixture
def emergency_mode_recorder():
    from baldur.metrics.recorders.emergency_mode import (
        EmergencyModeMetricRecorder,
    )

    return EmergencyModeMetricRecorder()


# =============================================================================
# A. Contract Tests — Level Map (DD-6)
# =============================================================================


class TestEmergencyModeRecorderContract:
    """R9: EmergencyModeMetricRecorder level map contract values."""

    def test_level_map_values(self):
        """LEVEL_MAP: normal=0, level_1=1, level_2=2, level_3=3."""

        assert _LEVEL_MAP == {
            "normal": 0,
            "level_1": 1,
            "level_2": 2,
            "level_3": 3,
        }

    def test_exports_seven_convenience_functions(self):
        """__all__ includes class + 7 convenience functions."""
        from baldur.metrics.recorders.emergency_mode import __all__

        assert "EmergencyModeMetricRecorder" in __all__
        assert "set_em_level" in __all__
        assert "set_em_active" in __all__
        assert "record_em_activation" in __all__
        assert "record_em_duration" in __all__
        assert "set_em_recovery_active" in __all__
        assert "record_em_recovery_step" in __all__
        assert "record_em_recovery_rollback" in __all__


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestEmergencyModeRecorderBehavior:
    """R9: EmergencyModeMetricRecorder method behavior."""

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    @pytest.mark.parametrize(
        "level_member", list(EmergencyLevel), ids=[m.value for m in EmergencyLevel]
    )
    def test_set_level_maps_input_to_gauge_value(
        self, emergency_mode_recorder, level_member, input_form
    ):
        """set_level maps enum members AND plain value strings to the mapped int (596 D2).

        PRO production call sites pass EmergencyLevel members directly
        (DD-6 enum-direct pass-through); the gauge must read the mapped
        int, not the silent default 0.
        """
        level_input = (
            level_member if input_form == "enum_member" else level_member.value
        )

        emergency_mode_recorder.set_level(level_input)

        assert (
            emergency_mode_recorder._level._value.get()
            == _LEVEL_MAP[level_member.value]
        )

    @pytest.mark.parametrize(
        "unknown_input",
        ["nonexistent", 42],
        ids=["typo_string", "non_string_hashable"],
    )
    def test_set_level_unknown_hashable_input_sets_zero_and_warns(
        self, emergency_mode_recorder, unknown_input
    ):
        """Unknown hashable input maps to 0 AND emits the unmapped-value WARNING (596 D2)."""
        # Given — a non-zero gauge so the reset to 0 is observable
        emergency_mode_recorder.set_level(EmergencyLevel.LEVEL_2)

        # When
        with capture_logs() as logs:
            emergency_mode_recorder.set_level(unknown_input)

        # Then — fail-open to 0, but never silently
        assert emergency_mode_recorder._level._value.get() == 0
        events = [
            e for e in logs if e.get("event") == "metrics.set_emergency_level_failed"
        ]
        assert len(events) == 1
        assert events[0]["reason"] == "unmapped_value"
        assert events[0]["log_level"] == "warning"

    @pytest.mark.parametrize(
        ("active", "expected"), [(True, 1), (False, 0)], ids=["on", "off"]
    )
    def test_set_active_sets_gauge(self, emergency_mode_recorder, active, expected):
        """set_active sets the active gauge to 1/0."""
        emergency_mode_recorder.set_active(active)

        assert emergency_mode_recorder._active._value.get() == expected

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    def test_record_activation_exports_value_string_level_label(
        self, emergency_mode_recorder, input_form
    ):
        """record_activation exports level="level_2", not the member path (596 D3).

        prometheus_client str()-coerces label values, so an enum member
        passed through uncoerced would export
        level="EmergencyLevel.LEVEL_2" — PromQL filters on the documented
        value strings would silently match nothing.
        """
        from baldur.core.test_mode_context import TestModeContext

        # Given — the labeled child addressed by the documented value-string label
        level_input = (
            EmergencyLevel.LEVEL_2
            if input_form == "enum_member"
            else EmergencyLevel.LEVEL_2.value
        )
        child = emergency_mode_recorder._activations_total.labels(
            level=EmergencyLevel.LEVEL_2.value,
            trigger_type="manual",
            is_synthetic=TestModeContext.get_synthetic_label_value(),
        )
        before = child._value.get()

        # When
        emergency_mode_recorder.record_activation(level_input, "manual")

        # Then — the increment landed on the value-string label child
        assert child._value.get() - before == 1

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    def test_record_duration_exports_value_string_level_label(
        self, emergency_mode_recorder, input_form
    ):
        """record_duration observes under the value-string level label (596 D3)."""
        # Given
        level_input = (
            EmergencyLevel.LEVEL_2
            if input_form == "enum_member"
            else EmergencyLevel.LEVEL_2.value
        )
        child = emergency_mode_recorder._duration.labels(
            level=EmergencyLevel.LEVEL_2.value
        )
        before = child._sum.get()

        # When
        emergency_mode_recorder.record_duration(level_input, 600.0)

        # Then — the observation landed on the value-string label child
        assert child._sum.get() - before == pytest.approx(600.0)

    @pytest.mark.parametrize(
        ("active", "expected"), [(True, 1), (False, 0)], ids=["on", "off"]
    )
    def test_set_recovery_active_sets_gauge(
        self, emergency_mode_recorder, active, expected
    ):
        """set_recovery_active sets the recovery-active gauge to 1/0."""
        emergency_mode_recorder.set_recovery_active(active)

        assert emergency_mode_recorder._recovery_active._value.get() == expected

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    def test_record_recovery_step_exports_value_string_labels(
        self, emergency_mode_recorder, input_form
    ):
        """record_recovery_step exports value-string from_level/to_level labels (596 D3)."""
        # Given
        if input_form == "enum_member":
            from_input, to_input = EmergencyLevel.LEVEL_2, EmergencyLevel.LEVEL_1
        else:
            from_input, to_input = (
                EmergencyLevel.LEVEL_2.value,
                EmergencyLevel.LEVEL_1.value,
            )
        child = emergency_mode_recorder._recovery_steps_total.labels(
            from_level=EmergencyLevel.LEVEL_2.value,
            to_level=EmergencyLevel.LEVEL_1.value,
        )
        before = child._value.get()

        # When
        emergency_mode_recorder.record_recovery_step(from_input, to_input)

        # Then — the increment landed on the value-string label child
        assert child._value.get() - before == 1

    def test_record_recovery_rollback_increments_counter_by_one(
        self, emergency_mode_recorder
    ):
        """record_recovery_rollback increments the rollback counter by 1."""
        before = emergency_mode_recorder._recovery_rollbacks_total._value.get()

        emergency_mode_recorder.record_recovery_rollback()

        assert (
            emergency_mode_recorder._recovery_rollbacks_total._value.get() - before == 1
        )


# =============================================================================
# C. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestEmergencyModeConvenienceFunctionsBehavior:
    """DD-7: Emergency mode convenience functions delegate to lazy recorder."""

    def test_convenience_delegates_to_recorder(self):
        """set_em_level delegates to recorder.set_level."""
        from baldur.metrics.recorders.emergency_mode import set_em_level

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.emergency_mode._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            set_em_level("level_1")
        mock_recorder.set_level.assert_called_once_with("level_1")


# =============================================================================
# D. Contract Tests — Facade Registration
# =============================================================================


class TestEmergencyModeFacadeRegistrationContract:
    """EmergencyModeMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_emergency_mode_attribute(self):
        """BaldurMetrics exposes emergency_mode recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.emergency_mode import (
            EmergencyModeMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.emergency_mode, EmergencyModeMetricRecorder)
