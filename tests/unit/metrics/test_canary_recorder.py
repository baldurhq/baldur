"""
CanaryMetricRecorder Unit Tests (394 — R21).

Test targets:
    - baldur.metrics.recorders.canary.CanaryMetricRecorder
    - Cardinality guard (_guard_stage_name)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: __all__ exports, _MAX_STAGE_NAMES, facade registration
    B. Behavior: Method invocations, cardinality guard, thread safety

Reference:
    394
"""

from __future__ import annotations

import concurrent.futures
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def canary_recorder():
    from baldur.metrics.recorders.canary import CanaryMetricRecorder

    return CanaryMetricRecorder()


def _exposition() -> str:
    """Current prometheus exposition text from the shared REGISTRY."""
    from prometheus_client import generate_latest

    return generate_latest().decode()


def _counter_total(text: str, metric: str) -> float:
    """Sum the sample values of counter ``metric`` across every label set.

    The shared REGISTRY is never reset between tests, so an absolute counter
    value is non-deterministic — assert on the before/after delta instead. The
    ``metric + "{"`` prefix excludes the sibling ``*_created`` series.
    """
    prefix = metric + "{"
    return sum(
        float(line.rsplit(" ", 1)[-1])
        for line in text.splitlines()
        if line.startswith(prefix)
    )


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestCanaryRecorderContract:
    """CanaryMetricRecorder contract: exports, constants, facade."""

    def test_all_exports_class_and_lifecycle_shortcuts(self):
        """__all__ exports the recorder class plus the module-level shortcuts."""
        from baldur.metrics.recorders.canary import __all__

        assert __all__ == [
            "CanaryMetricRecorder",
            "record_rollback",
            "record_rollout_completed",
            "record_rollout_started",
            "record_stage_advanced",
        ]

    def test_max_stage_names_is_20(self):
        """_MAX_STAGE_NAMES constant is 20."""
        from baldur.metrics.recorders.canary import _MAX_STAGE_NAMES

        assert _MAX_STAGE_NAMES == 20

    def test_facade_has_canary_attribute(self):
        """BaldurMetrics exposes canary recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.canary import CanaryMetricRecorder

        m = get_metrics()
        assert isinstance(m.canary, CanaryMetricRecorder)


# =============================================================================
# B. Behavior Tests — Recorder Methods
# =============================================================================


class TestCanaryRecorderBehavior:
    """CanaryMetricRecorder method behavior."""

    def test_record_rollout_started_does_not_raise(self, canary_recorder):
        """record_rollout_started does not raise."""
        canary_recorder.record_rollout_started()

    def test_record_rollout_completed_does_not_raise(self, canary_recorder):
        """record_rollout_completed does not raise."""
        canary_recorder.record_rollout_completed()

    def test_record_stage_advanced_does_not_raise(self, canary_recorder):
        """record_stage_advanced with stage_name does not raise."""
        canary_recorder.record_stage_advanced("stage_1")

    def test_record_rollback_does_not_raise(self, canary_recorder):
        """record_rollback with stage_name does not raise."""
        canary_recorder.record_rollback("stage_1")

    def test_cardinality_guard_20_stages_accepted(self, canary_recorder):
        """Registering 20 unique stages all return their own names."""
        from baldur.metrics.recorders.canary import _MAX_STAGE_NAMES

        for i in range(_MAX_STAGE_NAMES):
            result = canary_recorder._guard_stage_name(f"stage_{i}")
            assert result == f"stage_{i}"

    def test_cardinality_guard_21st_returns_other(self, canary_recorder):
        """21st unique stage name returns 'other'."""
        from baldur.metrics.recorders.canary import _MAX_STAGE_NAMES

        for i in range(_MAX_STAGE_NAMES):
            canary_recorder._guard_stage_name(f"stage_{i}")

        result = canary_recorder._guard_stage_name("stage_overflow")
        assert result == "other"

    def test_cardinality_guard_reuse_moves_to_end(self, canary_recorder):
        """Re-using existing stage moves it to end of OrderedDict (LRU)."""
        canary_recorder._guard_stage_name("alpha")
        canary_recorder._guard_stage_name("beta")
        canary_recorder._guard_stage_name("gamma")

        # Re-use alpha — should move to end
        canary_recorder._guard_stage_name("alpha")

        keys = list(canary_recorder._seen_stages.keys())
        assert keys[-1] == "alpha"

    def test_cardinality_guard_thread_safety(self, canary_recorder):
        """Concurrent calls don't corrupt the OrderedDict."""
        from baldur.metrics.recorders.canary import _MAX_STAGE_NAMES

        def register_stage(idx: int) -> str:
            return canary_recorder._guard_stage_name(f"thread_stage_{idx}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(register_stage, i) for i in range(50)]
            results = [f.result() for f in futures]

        # Total distinct stages in OrderedDict must not exceed _MAX_STAGE_NAMES
        assert len(canary_recorder._seen_stages) <= _MAX_STAGE_NAMES

        # Overflow stages must return "other"
        other_count = results.count("other")
        assert other_count >= 50 - _MAX_STAGE_NAMES


# =============================================================================
# C. Behavior Tests — Module-level lifecycle shortcuts (650 D2)
# =============================================================================


class TestCanaryShortcutBehavior:
    """650 D2: the module-level ``record_rollout_*`` shortcuts delegate to the
    live ``get_metrics().canary`` recorder behind a getattr guard.

    The shortcuts are the production wiring point for the (formerly dead) canary
    lifecycle panel: each advances its series in the shared exposition when the
    recorder is present, and is a backend-safe no-op (no raise) when it is not.
    """

    @pytest.mark.parametrize(
        ("shortcut", "args", "metric"),
        [
            ("record_rollout_started", (), "baldur_canary_rollout_started_total"),
            ("record_rollout_completed", (), "baldur_canary_rollout_completed_total"),
            (
                "record_stage_advanced",
                ("stage_650_advance",),
                "baldur_canary_stage_advanced_total",
            ),
            (
                "record_rollback",
                ("stage_650_rollback",),
                "baldur_canary_rollback_total",
            ),
        ],
    )
    def test_shortcut_advances_its_series(self, shortcut, args, metric):
        """A shortcut call increments its counter by exactly one in the shared
        exposition (delta, since the REGISTRY is never reset between tests)."""
        import baldur.metrics.recorders.canary as canary_mod

        before = _counter_total(_exposition(), metric)
        getattr(canary_mod, shortcut)(*args)
        after = _counter_total(_exposition(), metric)

        assert after == before + 1.0

    @pytest.mark.parametrize(
        ("shortcut", "args", "metric"),
        [
            ("record_rollout_started", (), "baldur_canary_rollout_started_total"),
            ("record_rollout_completed", (), "baldur_canary_rollout_completed_total"),
            (
                "record_stage_advanced",
                ("stage_650_guard",),
                "baldur_canary_stage_advanced_total",
            ),
            ("record_rollback", ("stage_650_guard",), "baldur_canary_rollback_total"),
        ],
    )
    def test_shortcut_is_noop_when_recorder_absent(self, shortcut, args, metric):
        """getattr-guard: a backend whose facade lacks a ``canary`` attribute
        (partial-init / NoOp) makes the shortcut a no-op — no raise, no series
        advance. ``MagicMock(spec=[])`` exposes no attributes, so
        ``getattr(metrics, "canary", None)`` returns None."""
        import baldur.metrics.recorders.canary as canary_mod

        before = _counter_total(_exposition(), metric)
        with patch(
            "baldur.metrics.prometheus.get_metrics", return_value=MagicMock(spec=[])
        ):
            getattr(canary_mod, shortcut)(*args)  # must not raise
        after = _counter_total(_exposition(), metric)

        assert after == before

    def test_recorder_lookup_returns_none_when_get_metrics_raises(self):
        """``_canary_recorder`` swallows a ``get_metrics()`` crash and returns
        None, so the shortcuts stay fail-open under a broken facade."""
        from baldur.metrics.recorders.canary import _canary_recorder

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("facade boom"),
        ):
            assert _canary_recorder() is None

    def test_shortcut_is_noop_when_get_metrics_raises(self):
        """A facade crash on lookup makes the shortcut a no-op (no propagation)."""
        from baldur.metrics.recorders.canary import record_rollout_started

        with patch(
            "baldur.metrics.prometheus.get_metrics",
            side_effect=RuntimeError("facade boom"),
        ):
            record_rollout_started()  # must not raise

    def test_recorder_lookup_returns_live_recorder_when_present(self):
        """With the default prometheus facade the lookup returns the live
        ``CanaryMetricRecorder`` (the wiring the panel depends on)."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.canary import (
            CanaryMetricRecorder,
            _canary_recorder,
        )

        rec = _canary_recorder()
        assert isinstance(rec, CanaryMetricRecorder)
        assert rec is get_metrics().canary
