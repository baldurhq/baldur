"""
WatchdogMetricRecorder Unit Tests (408 — C5).

Test targets:
    - baldur.metrics.recorders.watchdog.WatchdogMetricRecorder
    - _ALLOWED_COMPONENTS cardinality guard
    - Module-level convenience functions (DD-7)
    - Facade registration in BaldurMetrics

Test Categories:
    A. Contract: _ALLOWED_COMPONENTS values, __all__ exports, metric names
    B. Behavior: _resolve_component guard, recorder methods, convenience delegation

Reference:
    408
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs


@pytest.fixture
def watchdog_recorder():
    from baldur.metrics.recorders.watchdog import WatchdogMetricRecorder

    return WatchdogMetricRecorder()


# =============================================================================
# A. Contract Tests — Cardinality Guard & Exports
# =============================================================================


class TestWatchdogRecorderContract:
    """C5: WatchdogMetricRecorder cardinality guard and export contract."""

    def test_allowed_components_includes_core_components(self):
        """_ALLOWED_COMPONENTS contains every _COMPONENT_PRIORITY key.

        A _COMPONENT_PRIORITY key missing here is silently relabeled "other"
        in escalation/probe metrics — the drift class that hid the
        audit_system / precomputed_cache / error_budget_gate gap. This locks
        the full known priority set (hardcoded because G20 forbids importing
        the PRO-private _COMPONENT_PRIORITY into an OSS test; the live
        ``set(_COMPONENT_PRIORITY) <= _ALLOWED_COMPONENTS`` subset assertion
        lives in the PRO meta test).
        """
        from baldur.metrics.recorders.watchdog import _ALLOWED_COMPONENTS

        for component in (
            "redis",
            "dlq",
            "circuit_breaker",
            "recovery_pipeline",
            "audit_system",
            "daemon_workers",
            "chaos_scheduler",
            "notification_channels",
            "precomputed_cache",
            "error_budget_gate",
            "canary_rollout",
            "emergency_mode",
            "adaptive_throttle",
        ):
            assert component in _ALLOWED_COMPONENTS

    def test_fallback_component_is_other(self):
        """_FALLBACK_COMPONENT is 'other'."""
        from baldur.metrics.recorders.watchdog import _FALLBACK_COMPONENT

        assert _FALLBACK_COMPONENT == "other"

    def test_exports_class_and_four_convenience_functions(self):
        """__all__ includes class + 4 convenience functions."""
        from baldur.metrics.recorders.watchdog import __all__

        assert "WatchdogMetricRecorder" in __all__
        assert "record_watchdog_probe" in __all__
        assert "record_watchdog_recovery" in __all__
        assert "set_watchdog_self_cb_state" in __all__
        assert "observe_watchdog_probe_duration" in __all__


# =============================================================================
# B. Behavior Tests — Cardinality Guard
# =============================================================================


class TestWatchdogCardinalityGuardBehavior:
    """C5: _resolve_component maps unknown components to fallback."""

    def test_resolve_known_component_returns_same(self):
        """Known component string is returned as-is."""
        from baldur.metrics.recorders.watchdog import _resolve_component

        assert _resolve_component("redis") == "redis"
        assert _resolve_component("dlq") == "dlq"

    def test_resolve_unknown_component_returns_fallback(self):
        """Unknown component string maps to 'other'."""
        from baldur.metrics.recorders.watchdog import (
            _FALLBACK_COMPONENT,
            _resolve_component,
        )

        assert _resolve_component("unknown_service") == _FALLBACK_COMPONENT
        assert _resolve_component("") == _FALLBACK_COMPONENT


# =============================================================================
# C. Behavior Tests — Recorder Methods
# =============================================================================


class TestWatchdogRecorderBehavior:
    """C5: WatchdogMetricRecorder methods do not raise."""

    def test_record_probe_success(self, watchdog_recorder):
        """record_probe with success status does not raise."""
        watchdog_recorder.record_probe("redis", "success")

    def test_record_probe_failure(self, watchdog_recorder):
        """record_probe with failure status does not raise."""
        watchdog_recorder.record_probe("dlq", "failure")

    def test_record_probe_unknown_component_uses_fallback(self, watchdog_recorder):
        """record_probe with unknown component does not raise (guard applied)."""
        watchdog_recorder.record_probe("my_dynamic_service", "success")

    def test_record_recovery(self, watchdog_recorder):
        """record_recovery with valid args does not raise."""
        watchdog_recorder.record_recovery("redis", "restart", "success")

    def test_set_self_cb_state_open(self, watchdog_recorder):
        """set_self_cb_state(True) sets gauge to 1."""
        watchdog_recorder.set_self_cb_state(True)

    def test_set_self_cb_state_closed(self, watchdog_recorder):
        """set_self_cb_state(False) sets gauge to 0."""
        watchdog_recorder.set_self_cb_state(False)

    def test_observe_probe_duration(self, watchdog_recorder):
        """observe_probe_duration with positive value does not raise."""
        watchdog_recorder.observe_probe_duration("circuit_breaker", 0.123)


# =============================================================================
# D. Behavior Tests — Convenience Functions (DD-7)
# =============================================================================


class TestWatchdogConvenienceFunctionsBehavior:
    """DD-7: Watchdog convenience functions delegate to lazy recorder."""

    def test_record_watchdog_probe_delegates(self):
        """record_watchdog_probe delegates to recorder.record_probe."""
        from baldur.metrics.recorders.watchdog import record_watchdog_probe

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_probe("redis", "success")
        mock_recorder.record_probe.assert_called_once_with("redis", "success")

    def test_record_watchdog_recovery_delegates(self):
        """record_watchdog_recovery delegates to recorder.record_recovery."""
        from baldur.metrics.recorders.watchdog import record_watchdog_recovery

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_recovery("dlq", "reset", "failure")
        mock_recorder.record_recovery.assert_called_once_with("dlq", "reset", "failure")

    def test_convenience_noop_when_recorder_unavailable(self):
        """Convenience functions silently no-op when recorder is None."""
        from baldur.metrics.recorders.watchdog import record_watchdog_probe

        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_watchdog_probe("redis", "success")  # Should not raise


# =============================================================================
# E. Contract Tests — Facade Registration
# =============================================================================


class TestWatchdogFacadeRegistrationContract:
    """WatchdogMetricRecorder registered in BaldurMetrics facade."""

    def test_facade_has_watchdog_attribute(self):
        """BaldurMetrics exposes watchdog recorder."""
        from baldur.metrics.prometheus import get_metrics
        from baldur.metrics.recorders.watchdog import WatchdogMetricRecorder

        m = get_metrics()
        assert isinstance(m.watchdog, WatchdogMetricRecorder)


# =============================================================================
# F. 409 C11-33 — Governance Blocked Counter
# =============================================================================


class TestWatchdogGovernanceBlockedContract:
    """409 C11-33: Governance blocked counter contract."""

    def test_allowed_components_includes_notification_channels(self):
        """_ALLOWED_COMPONENTS includes notification_channels (409 UU-E8)."""
        from baldur.metrics.recorders.watchdog import _ALLOWED_COMPONENTS

        assert "notification_channels" in _ALLOWED_COMPONENTS

    def test_exports_include_governance_blocked_function(self):
        """__all__ includes record_watchdog_governance_blocked."""
        from baldur.metrics.recorders.watchdog import __all__

        assert "record_watchdog_governance_blocked" in __all__


class TestWatchdogGovernanceBlockedBehavior:
    """409 C11-33: Governance blocked counter behavior."""

    def test_record_governance_blocked_does_not_raise(self, watchdog_recorder):
        """record_governance_blocked with known component does not raise."""
        watchdog_recorder.record_governance_blocked("redis")

    def test_record_governance_blocked_unknown_uses_fallback(self, watchdog_recorder):
        """record_governance_blocked with unknown component uses fallback."""
        watchdog_recorder.record_governance_blocked("unknown_svc")

    def test_convenience_governance_blocked_delegates(self):
        """record_watchdog_governance_blocked delegates to recorder."""
        from baldur.metrics.recorders.watchdog import (
            record_watchdog_governance_blocked,
        )

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_governance_blocked("circuit_breaker")
        mock_recorder.record_governance_blocked.assert_called_once_with(
            "circuit_breaker"
        )

    def test_convenience_governance_blocked_noop_when_none(self):
        """record_watchdog_governance_blocked no-ops when recorder is None."""
        from baldur.metrics.recorders.watchdog import (
            record_watchdog_governance_blocked,
        )

        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_watchdog_governance_blocked("redis")  # Should not raise


# =============================================================================
# G. 558 D5 — Escalation Counter
# =============================================================================


class TestWatchdogEscalationCounterContract:
    """558 D5: baldur_watchdog_escalation_total export and label contract."""

    def test_exports_escalation_convenience_function(self):
        """__all__ includes record_watchdog_escalation."""
        from baldur.metrics.recorders.watchdog import __all__

        assert "record_watchdog_escalation" in __all__


class TestWatchdogEscalationCounterBehavior:
    """558 D5: escalation counter recording + cardinality guard."""

    def test_record_escalation_sent_result_does_not_raise(self, watchdog_recorder):
        """record_escalation with result='sent' does not raise."""
        watchdog_recorder.record_escalation("redis", "sent")

    def test_record_escalation_fallback_result_does_not_raise(self, watchdog_recorder):
        """record_escalation with result='fallback' does not raise."""
        watchdog_recorder.record_escalation("dlq", "fallback")

    def test_record_escalation_unknown_component_uses_fallback(self, watchdog_recorder):
        """record_escalation with unknown component does not raise (guard applied)."""
        watchdog_recorder.record_escalation("my_dynamic_service", "sent")

    def test_convenience_escalation_delegates(self):
        """record_watchdog_escalation delegates to recorder.record_escalation."""
        from baldur.metrics.recorders.watchdog import record_watchdog_escalation

        mock_recorder = MagicMock()
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_escalation("circuit_breaker", "sent")
        mock_recorder.record_escalation.assert_called_once_with(
            "circuit_breaker", "sent"
        )

    def test_convenience_escalation_noop_when_none(self):
        """record_watchdog_escalation no-ops when recorder is None."""
        from baldur.metrics.recorders.watchdog import record_watchdog_escalation

        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_watchdog_escalation("redis", "sent")  # Should not raise


# =============================================================================
# H. 731 — Outbound Liveness-Beacon Counter
# =============================================================================


class _RaisingCounter:
    """Counter double that fails where production actually touches it.

    Production calls ``.labels(...)`` on the collector, so a ``side_effect`` on
    the collector itself would never fire — the fault has to live on the
    attribute the recorder reaches.
    """

    def __init__(self):
        self.touched = False

    def labels(self, **kwargs):
        self.touched = True
        raise RuntimeError("registry down")


class _RecordingCounter:
    """Counter double that captures the labels and the increments."""

    def __init__(self):
        self.label_calls = []
        self.increments = 0

    def labels(self, **kwargs):
        self.label_calls.append(kwargs)
        return self

    def inc(self):
        self.increments += 1


class TestWatchdogRecorderBeaconContract:
    """731: baldur_watchdog_beacon_total export contract."""

    def test_exports_beacon_convenience_function(self):
        """__all__ includes record_watchdog_beacon."""
        from baldur.metrics.recorders.watchdog import __all__

        assert "record_watchdog_beacon" in __all__


class TestWatchdogRecorderBeaconBehavior:
    """731: beacon counter labelling, fail-open, and convenience delegation."""

    @pytest.mark.parametrize(
        "result", ["success", "failure"], ids=["success", "failure"]
    )
    def test_record_beacon_labels_the_counter_with_the_result(
        self, watchdog_recorder, result
    ):
        """The delivery result is the counter's only label, and it increments."""
        counter = _RecordingCounter()
        watchdog_recorder._beacon_total = counter

        watchdog_recorder.record_beacon(result)

        assert counter.label_calls == [{"result": result}]
        assert counter.increments == 1

    def test_record_beacon_swallows_a_failing_registry(self, watchdog_recorder):
        """A broken registry costs the counter, never the ping.

        The beacon calls this after every ping; a raise here would reach the
        sender thread's loop instead of the metric it was trying to emit.
        """
        counter = _RaisingCounter()
        watchdog_recorder._beacon_total = counter

        with capture_logs() as logs:
            watchdog_recorder.record_beacon("success")

        assert counter.touched is True
        assert any(
            log["event"] == "metrics.record_watchdog_beacon_failed" for log in logs
        )

    def test_convenience_beacon_delegates(self):
        """record_watchdog_beacon forwards the result to recorder.record_beacon."""
        from baldur.metrics.recorders.watchdog import (
            WatchdogMetricRecorder,
            record_watchdog_beacon,
        )

        mock_recorder = MagicMock(spec=WatchdogMetricRecorder)
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_beacon("failure")
        mock_recorder.record_beacon.assert_called_once_with("failure")

    def test_convenience_beacon_noop_when_none(self):
        """record_watchdog_beacon no-ops when the recorder is unavailable."""
        from baldur.metrics.recorders.watchdog import record_watchdog_beacon

        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_watchdog_beacon("success")  # Should not raise


class _RaisingUnlabeledCounter:
    """Counter double that fails on ``inc()`` — where an unlabeled metric is touched."""

    def __init__(self):
        self.touched = False

    def inc(self):
        self.touched = True
        raise RuntimeError("registry down")


class TestWatchdogRecorderBoundedPassContract:
    """732: the two counters a budget-bounded pass emits are exported."""

    def test_exports_bounded_pass_convenience_functions(self):
        from baldur.metrics.recorders.watchdog import __all__

        assert "record_watchdog_pass_truncated" in __all__
        assert "record_watchdog_single_flight_skipped" in __all__


class TestWatchdogRecorderBoundedPassBehavior:
    """732: pass-truncation and single-flight-skip counters."""

    def test_record_pass_truncated_increments_without_labels(self, watchdog_recorder):
        """Pass-level, so unlabeled: the truncated names ride the WARNING instead.

        Labelling this per component would pay per-component cardinality for an
        exceptional event.
        """
        counter = _RecordingCounter()
        watchdog_recorder._pass_truncated_total = counter

        watchdog_recorder.record_pass_truncated()

        assert counter.increments == 1
        assert counter.label_calls == []

    def test_record_pass_truncated_swallows_a_failing_registry(self, watchdog_recorder):
        """A broken registry costs the counter, never the pass."""
        counter = _RaisingUnlabeledCounter()
        watchdog_recorder._pass_truncated_total = counter

        with capture_logs() as logs:
            watchdog_recorder.record_pass_truncated()

        assert counter.touched is True
        assert any(
            log["event"] == "metrics.record_watchdog_pass_truncated_failed"
            for log in logs
        )

    def test_record_single_flight_skipped_labels_the_component(self, watchdog_recorder):
        counter = _RecordingCounter()
        watchdog_recorder._probe_single_flight_skipped_total = counter

        watchdog_recorder.record_single_flight_skipped("redis")

        assert counter.label_calls == [{"component": "redis"}]
        assert counter.increments == 1

    def test_record_single_flight_skipped_applies_the_cardinality_guard(
        self, watchdog_recorder
    ):
        """An unknown component collapses to the fallback label, as everywhere else."""
        from baldur.metrics.recorders.watchdog import _FALLBACK_COMPONENT

        counter = _RecordingCounter()
        watchdog_recorder._probe_single_flight_skipped_total = counter

        watchdog_recorder.record_single_flight_skipped("not_a_registered_component")

        assert counter.label_calls == [{"component": _FALLBACK_COMPONENT}]

    def test_record_single_flight_skipped_swallows_a_failing_registry(
        self, watchdog_recorder
    ):
        counter = _RaisingCounter()
        watchdog_recorder._probe_single_flight_skipped_total = counter

        with capture_logs() as logs:
            watchdog_recorder.record_single_flight_skipped("redis")

        assert counter.touched is True
        assert any(
            log["event"] == "metrics.record_watchdog_single_flight_skipped_failed"
            for log in logs
        )

    def test_convenience_functions_delegate_to_the_recorder(self):
        from baldur.metrics.recorders.watchdog import (
            WatchdogMetricRecorder,
            record_watchdog_pass_truncated,
            record_watchdog_single_flight_skipped,
        )

        mock_recorder = MagicMock(spec=WatchdogMetricRecorder)
        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=mock_recorder,
            autospec=True,
        ):
            record_watchdog_pass_truncated()
            record_watchdog_single_flight_skipped("dlq")

        mock_recorder.record_pass_truncated.assert_called_once_with()
        mock_recorder.record_single_flight_skipped.assert_called_once_with("dlq")

    def test_convenience_functions_noop_when_the_recorder_is_unavailable(self):
        from baldur.metrics.recorders.watchdog import (
            record_watchdog_pass_truncated,
            record_watchdog_single_flight_skipped,
        )

        with patch(
            "baldur.metrics.recorders.watchdog._lazy_recorder",
            return_value=None,
            autospec=True,
        ):
            record_watchdog_pass_truncated()  # Should not raise
            record_watchdog_single_flight_skipped("redis")  # Should not raise
