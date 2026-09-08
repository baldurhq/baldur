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

    def test_exports_the_convenience_functions(self):
        """__all__ includes the class + every module-level convenience function."""
        from baldur.metrics.recorders.emergency_mode import __all__

        assert "EmergencyModeMetricRecorder" in __all__
        assert "set_em_level" in __all__
        assert "set_em_active" in __all__
        assert "record_em_activation" in __all__
        assert "record_em_duration" in __all__
        assert "set_em_recovery_active" in __all__
        assert "record_em_recovery_step" in __all__
        assert "record_em_recovery_rollback" in __all__
        assert "record_em_shed" in __all__


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
# D. Behavior Tests — HTTP shed counter
# =============================================================================


_SHED_METRIC = "baldur_emergency_mode_shed_requests_total"


class _BrokenCounter:
    """A collector whose child lookup fails — the recorder must absorb it."""

    def labels(self, **kwargs):
        raise RuntimeError("collector detached from the registry")


class TestEmergencyShedMetricBehavior:
    """The shed counter is registered, labelled by value strings, and fail-open.

    Before the extraction the counter was built per rejection with
    ``registry=None`` and thrown away, so it was never scraped on any framework.
    """

    def test_counter_is_registered_with_the_default_registry(
        self, emergency_mode_recorder
    ):
        """Construction registers the collector — a per-call Counter would not."""
        from prometheus_client import REGISTRY

        assert _SHED_METRIC in REGISTRY._names_to_collectors

    def test_registered_collector_is_the_one_the_recorder_increments(
        self, emergency_mode_recorder
    ):
        """The instance attribute IS the registered collector, not a private copy."""
        from prometheus_client import REGISTRY

        assert (
            REGISTRY._names_to_collectors[_SHED_METRIC]
            is emergency_mode_recorder._shed_requests_total
        )

    def test_repeated_recorder_construction_reuses_one_collector(self):
        """``get_or_create_counter`` is idempotent — a second recorder must not raise."""
        from baldur.metrics.recorders.emergency_mode import (
            EmergencyModeMetricRecorder,
        )

        first = EmergencyModeMetricRecorder()
        second = EmergencyModeMetricRecorder()

        assert first._shed_requests_total is second._shed_requests_total

    @pytest.mark.parametrize("input_form", ["enum_member", "value_string"])
    def test_record_shed_exports_value_string_level_labels(
        self, emergency_mode_recorder, input_form
    ):
        """prometheus_client str()-coerces labels: a raw member exports
        ``EmergencyLevel.LEVEL_3`` and every PromQL filter on the documented
        value silently matches nothing.
        """
        from baldur.settings.backpressure import BackpressureLevel

        if input_form == "enum_member":
            level_input, bp_input = EmergencyLevel.LEVEL_3, BackpressureLevel.HIGH
        else:
            level_input, bp_input = (
                EmergencyLevel.LEVEL_3.value,
                BackpressureLevel.HIGH.value,
            )
        child = emergency_mode_recorder._shed_requests_total.labels(
            tier="standard",
            emergency_level=EmergencyLevel.LEVEL_3.value,
            backpressure_level=BackpressureLevel.HIGH.value,
        )
        before = child._value.get()

        emergency_mode_recorder.record_shed("standard", level_input, bp_input)

        assert child._value.get() - before == 1

    def test_pure_backpressure_shed_is_attributable(self, emergency_mode_recorder):
        """Emergency ``normal`` + backpressure ``high``: the label pair names the cause."""
        from baldur.settings.backpressure import BackpressureLevel

        child = emergency_mode_recorder._shed_requests_total.labels(
            tier="non_essential",
            emergency_level=EmergencyLevel.NORMAL.value,
            backpressure_level=BackpressureLevel.HIGH.value,
        )
        before = child._value.get()

        emergency_mode_recorder.record_shed(
            "non_essential", EmergencyLevel.NORMAL, BackpressureLevel.HIGH
        )

        assert child._value.get() - before == 1

    def test_record_shed_absorbs_a_collector_failure(self, emergency_mode_recorder):
        """A shed must never depend on the metric — the 503 is already decided."""
        emergency_mode_recorder._shed_requests_total = _BrokenCounter()

        with capture_logs() as logs:
            emergency_mode_recorder.record_shed("standard", "level_1", "none")

        events = [
            e for e in logs if e.get("event") == "metrics.record_emergency_shed_failed"
        ]
        assert len(events) == 1
        assert events[0]["log_level"] == "warning"

    def test_convenience_delegates_to_the_recorder(self):
        from baldur.metrics.recorders.emergency_mode import (
            EmergencyModeMetricRecorder,
            record_em_shed,
        )

        mock_recorder = MagicMock(spec=EmergencyModeMetricRecorder)
        with patch(
            "baldur.metrics.recorders.emergency_mode._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_em_shed("standard", "level_2", "low")

        mock_recorder.record_shed.assert_called_once_with("standard", "level_2", "low")

    def test_convenience_is_a_no_op_without_a_recorder(self):
        """Metrics unavailable -> the rejection path still returns normally."""
        from baldur.metrics.recorders.emergency_mode import record_em_shed

        with patch(
            "baldur.metrics.recorders.emergency_mode._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            assert record_em_shed("standard", "level_2", "low") is None


class TestShedMetricRelocationContract:
    """The producer-less name it replaced has no producer left.

    Metric-relocation discipline: asserting the new counter exists proves
    nothing about the old one, which was constructed per rejection against no
    registry and had zero consumers.
    """

    @staticmethod
    def _shipped_source_roots():
        """Every installed first-party package root present in this checkout."""
        from pathlib import Path

        import baldur

        roots = [Path(baldur.__file__).parent]
        for private in ("baldur_pro", "baldur_dormant"):
            try:
                module = __import__(private)
            except ImportError:
                continue
            roots.append(Path(module.__file__).parent)
        return roots

    def test_old_counter_name_has_no_producer(self):
        offenders = [
            str(path)
            for root in self._shipped_source_roots()
            for path in root.rglob("*.py")
            if "baldur_tiering_load_shedding_total" in path.read_text(encoding="utf-8")
        ]

        assert offenders == [], (
            "baldur_tiering_load_shedding_total was replaced by "
            f"{_SHED_METRIC}; a surviving producer means two names for one "
            f"signal: {offenders}"
        )

    def test_new_counter_name_has_a_producer(self):
        """Non-vacuity: the same scan finds the replacement."""
        producers = [
            str(path)
            for root in self._shipped_source_roots()
            for path in root.rglob("*.py")
            if _SHED_METRIC.removeprefix("baldur_") in path.read_text(encoding="utf-8")
        ]

        assert producers != []


# =============================================================================
# E. Contract Tests — Facade Registration
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
