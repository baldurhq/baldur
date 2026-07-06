"""
BackpressureLevel comparison operators unit tests (409 UU-E4).

Test targets:
    - BackpressureLevel.__ge__, __gt__, __le__, __lt__
    - BackpressureLevel.severity property
    - _BP_SEVERITY_ORDER dict

Test categories:
    A. Contract: severity ordering values, operator count
    B. Behavior: ordering correctness, boundary analysis, NotImplemented
"""

from __future__ import annotations

import pytest

from baldur.settings.backpressure import _BP_SEVERITY_ORDER, BackpressureLevel

# =============================================================================
# A. Contract Tests
# =============================================================================


class TestBackpressureLevelSeverityContract:
    """409 UU-E4: BackpressureLevel severity ordering contract values."""

    def test_severity_order_none_is_0(self):
        assert BackpressureLevel.NONE.severity == 0

    def test_severity_order_low_is_1(self):
        assert BackpressureLevel.LOW.severity == 1

    def test_severity_order_medium_is_2(self):
        assert BackpressureLevel.MEDIUM.severity == 2

    def test_severity_order_high_is_3(self):
        assert BackpressureLevel.HIGH.severity == 3

    def test_severity_order_critical_is_4(self):
        assert BackpressureLevel.CRITICAL.severity == 4

    def test_severity_order_covers_all_members(self):
        """_BP_SEVERITY_ORDER covers every BackpressureLevel member."""
        assert set(_BP_SEVERITY_ORDER.keys()) == set(BackpressureLevel)

    def test_severity_order_is_strictly_increasing(self):
        """NONE < LOW < MEDIUM < HIGH < CRITICAL in severity."""
        ordered = [
            BackpressureLevel.NONE,
            BackpressureLevel.LOW,
            BackpressureLevel.MEDIUM,
            BackpressureLevel.HIGH,
            BackpressureLevel.CRITICAL,
        ]
        for i in range(len(ordered) - 1):
            assert ordered[i].severity < ordered[i + 1].severity


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestBackpressureLevelComparisonBehavior:
    """409 UU-E4: BackpressureLevel comparison operator behavior."""

    # --- __gt__ ---

    def test_critical_gt_high(self):
        assert BackpressureLevel.CRITICAL > BackpressureLevel.HIGH

    def test_none_not_gt_none(self):
        assert not (BackpressureLevel.NONE > BackpressureLevel.NONE)

    def test_low_not_gt_medium(self):
        assert not (BackpressureLevel.LOW > BackpressureLevel.MEDIUM)

    # --- __ge__ ---

    def test_critical_ge_critical(self):
        assert BackpressureLevel.CRITICAL >= BackpressureLevel.CRITICAL

    def test_high_ge_medium(self):
        assert BackpressureLevel.HIGH >= BackpressureLevel.MEDIUM

    def test_low_not_ge_high(self):
        assert not (BackpressureLevel.LOW >= BackpressureLevel.HIGH)

    # --- __lt__ ---

    def test_none_lt_low(self):
        assert BackpressureLevel.NONE < BackpressureLevel.LOW

    def test_critical_not_lt_high(self):
        assert not (BackpressureLevel.CRITICAL < BackpressureLevel.HIGH)

    # --- __le__ ---

    def test_medium_le_medium(self):
        assert BackpressureLevel.MEDIUM <= BackpressureLevel.MEDIUM

    def test_medium_le_high(self):
        assert BackpressureLevel.MEDIUM <= BackpressureLevel.HIGH

    def test_high_not_le_low(self):
        assert not (BackpressureLevel.HIGH <= BackpressureLevel.LOW)

    # --- Boundary: adjacent levels ---

    @pytest.mark.parametrize(
        ("lower", "higher"),
        [
            (BackpressureLevel.NONE, BackpressureLevel.LOW),
            (BackpressureLevel.LOW, BackpressureLevel.MEDIUM),
            (BackpressureLevel.MEDIUM, BackpressureLevel.HIGH),
            (BackpressureLevel.HIGH, BackpressureLevel.CRITICAL),
        ],
    )
    def test_adjacent_levels_ordering(self, lower, higher):
        """Each adjacent pair satisfies lower < higher."""
        assert lower < higher
        assert higher > lower
        assert lower <= higher
        assert higher >= lower

    # --- NotImplemented for non-BackpressureLevel ---

    def test_ge_non_backpressure_returns_not_implemented(self):
        assert BackpressureLevel.HIGH.__ge__("not_a_level") is NotImplemented

    def test_gt_non_backpressure_returns_not_implemented(self):
        assert BackpressureLevel.HIGH.__gt__(42) is NotImplemented

    def test_le_non_backpressure_returns_not_implemented(self):
        assert BackpressureLevel.HIGH.__le__(None) is NotImplemented

    def test_lt_non_backpressure_returns_not_implemented(self):
        assert BackpressureLevel.HIGH.__lt__(3.14) is NotImplemented


class TestSagaSettingsBackpressureContract:
    """409 UU-E4: SagaSettings.backpressure_rejection_level contract."""

    def test_default_rejection_level_is_critical(self):
        """Default backpressure_rejection_level is 'critical'."""
        from baldur.settings.saga import SagaSettings

        settings = SagaSettings()
        assert settings.backpressure_rejection_level == "critical"
