"""
CorruptionShieldMetricRecorder Unit Tests (394 — R22).

Test targets:
    - baldur.metrics.recorders.corruption_shield.CorruptionShieldMetricRecorder
    - Internal _stats tracking and get_stats() snapshot
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, initial stats, facade registration
    B. Behavior: record_validation, record_violation, stats snapshot, thread safety

Reference:
    394
"""

from __future__ import annotations

import concurrent.futures

import pytest


@pytest.fixture
def shield_recorder():
    from baldur.metrics.recorders.corruption_shield import (
        CorruptionShieldMetricRecorder,
    )

    return CorruptionShieldMetricRecorder()


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestCorruptionShieldRecorderContract:
    """CorruptionShieldMetricRecorder contract: exports, initial stats."""

    def test_all_exports_exactly_class(self):
        """__all__ exports exactly ['CorruptionShieldMetricRecorder']."""
        from baldur.metrics.recorders.corruption_shield import __all__

        assert __all__ == ["CorruptionShieldMetricRecorder"]

    def test_initial_stats_all_zero(self, shield_recorder):
        """Initial get_stats() returns dict with all keys at 0."""
        stats = shield_recorder.get_stats()
        expected_keys = {
            "total_validations",
            "passed",
            "blocked",
            "l1_violations",
            "l2_violations",
            "l3_violations",
        }
        assert set(stats.keys()) == expected_keys
        for key, value in stats.items():
            assert value == 0, f"Expected 0 for {key}, got {value}"

    def test_facade_has_corruption_shield_attribute(self):
        """BaldurMetrics exposes corruption_shield recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.corruption_shield import (
            CorruptionShieldMetricRecorder,
        )

        m = get_metrics()
        assert isinstance(m.corruption_shield, CorruptionShieldMetricRecorder)


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestCorruptionShieldRecorderBehavior:
    """CorruptionShieldMetricRecorder method behavior."""

    def test_record_validation_valid_passed(self, shield_recorder):
        """record_validation(is_valid=True, blocked=False) increments total and passed."""
        shield_recorder.record_validation(is_valid=True, blocked=False)
        stats = shield_recorder.get_stats()
        assert stats["total_validations"] == 1
        assert stats["passed"] == 1
        assert stats["blocked"] == 0

    def test_record_validation_invalid_blocked(self, shield_recorder):
        """record_validation(is_valid=False, blocked=True) increments total and blocked."""
        shield_recorder.record_validation(is_valid=False, blocked=True)
        stats = shield_recorder.get_stats()
        assert stats["total_validations"] == 1
        assert stats["passed"] == 0
        assert stats["blocked"] == 1

    def test_record_violation_l1_with_count(self, shield_recorder):
        """record_violation('l1', 2) increments l1_violations by 2."""
        shield_recorder.record_violation("l1", 2)
        stats = shield_recorder.get_stats()
        assert stats["l1_violations"] == 2

    def test_record_violation_l2_default_count(self, shield_recorder):
        """record_violation('l2') increments l2_violations by 1."""
        shield_recorder.record_violation("l2")
        stats = shield_recorder.get_stats()
        assert stats["l2_violations"] == 1

    def test_record_violation_l3_default_count(self, shield_recorder):
        """record_violation('l3') increments l3_violations by 1."""
        shield_recorder.record_violation("l3")
        stats = shield_recorder.get_stats()
        assert stats["l3_violations"] == 1

    def test_record_violation_unknown_layer_no_increment(self, shield_recorder):
        """record_violation('unknown_layer') does NOT increment any stats key."""
        shield_recorder.record_violation("unknown_layer")
        stats = shield_recorder.get_stats()
        for key, value in stats.items():
            assert value == 0, f"Expected 0 for {key}, got {value}"

    def test_get_stats_returns_snapshot(self, shield_recorder):
        """get_stats() returns a copy, not a reference to internal dict."""
        snapshot = shield_recorder.get_stats()
        snapshot["total_validations"] = 999
        assert shield_recorder.get_stats()["total_validations"] == 0

    def test_thread_safety_concurrent_validations(self, shield_recorder):
        """Concurrent record_validation calls produce correct totals."""
        call_count = 100

        def do_validation(_: int) -> None:
            shield_recorder.record_validation(is_valid=True, blocked=False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(do_validation, range(call_count)))

        stats = shield_recorder.get_stats()
        assert stats["total_validations"] == call_count
        assert stats["passed"] == call_count
