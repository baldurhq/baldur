"""The panic threshold's two entry points, and the policy that separates them.

``evaluate()`` is a pure probe: it answers whether the fleet-wide OPEN ratio
meets the threshold *right now*, moves no counter and writes nothing. The chaos
safety pre-check consumes it, so it raises rather than substituting an empty
list -- for a guard asking "may this experiment run?", an empty fleet reads as
"allow", which is the wrong direction.

``tick()`` owns the hysteresis and the escalation. It is the scheduled job's
only entry point, it declares Emergency Level 3 through ``activate_auto``, and
it confirms the declaration on the returned state rather than assuming it --
a kill-switched activation returns the *unchanged* state and must record no
audit.

Verification techniques applied:
- Boundary analysis: the ``(open, total)`` grid either side of
  ``min_registered_services`` and ``threshold_percent``
- Idempotency: two probes leave the counter and the emergency manager alone
- State transition: the consecutive-trigger walk, and its reset on an
  untriggered tick
- Exception/edge cases: a cluster read that raises reaches the probe's caller
  but is absorbed by the tick
- Side effects: the audit record and the CRITICAL line, both owed only to a
  confirmed escalation
- Time dependency: the cooldown window, driven from a stamped reference rather
  than a sleep
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from structlog.testing import capture_logs

from baldur.interfaces.emergency import EmergencyManager
from baldur.models.emergency import EmergencyLevel
from baldur.services.circuit_breaker import CircuitBreakerService
from baldur.services.circuit_breaker.exceptions import (
    CircuitBreakerStateUnavailableError,
)
from baldur.services.circuit_breaker.panic_threshold import (
    PanicThresholdConfig,
    PanicThresholdMonitor,
    PanicThresholdResult,
)
from baldur.utils.time import utc_now

STABILIZATION_PERIOD = 300


# =============================================================================
# Helpers
# =============================================================================


def _rows(open_names: list[str], all_names: list[str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            service_name=name,
            state="open" if name in open_names else "closed",
        )
        for name in all_names
    ]


def _cb_service(open_names: list[str], all_names: list[str]) -> Mock:
    service = Mock(spec=CircuitBreakerService)
    service.repository.get_cluster_states.return_value = _rows(open_names, all_names)
    return service


def _monitor(
    open_names: list[str],
    all_names: list[str],
    *,
    emergency_manager: Any = None,
    **config_kwargs: Any,
) -> PanicThresholdMonitor:
    """A monitor whose cluster read returns the given rows."""
    return PanicThresholdMonitor(
        config=PanicThresholdConfig(**config_kwargs),
        circuit_breaker_service=_cb_service(open_names, all_names),
        emergency_manager=emergency_manager,
    )


def _tripping_monitor(**kwargs: Any) -> PanicThresholdMonitor:
    """A monitor whose fleet is 4-of-5 OPEN -- 80%, past the 70% default."""
    return _monitor(
        ["s1", "s2", "s3", "s4"],
        ["s1", "s2", "s3", "s4", "s5"],
        **kwargs,
    )


def _settings(*, enabled: bool = True, percent: float = 70.0, action: str = "freeze"):
    return SimpleNamespace(
        enabled=enabled,
        panic_threshold_percent=percent,
        panic_threshold_action=action,
    )


def _with_settings(settings: Any):
    """Pin the settings ``tick()`` reads, so the tick is not gated off by default.

    The advanced-protection flag ships default-OFF, so a tick against the real
    settings returns before any of the policy below is reached.
    """
    return patch(
        "baldur.services.circuit_breaker.panic_threshold._advanced_settings",
        return_value=settings,
    )


def _with_stabilization(period: int | None = STABILIZATION_PERIOD):
    return patch(
        "baldur.services.circuit_breaker.panic_threshold._stabilization_period_seconds",
        return_value=period,
    )


def _emergency(level: Any, **state_fields: Any) -> Mock:
    """A stub emergency manager whose state carries the given fields.

    ``activate_auto`` answers with LEVEL_3 by default -- the confirmed
    declaration. A kill-switched manager is built by overriding it.
    """
    manager = Mock(spec=EmergencyManager)
    fields: dict[str, Any] = {
        "level": level,
        "is_recovering": False,
        "deactivated_at": None,
    }
    fields.update(state_fields)
    manager.get_state.return_value = SimpleNamespace(**fields)
    manager.get_current_level.return_value = level
    manager.activate_auto.return_value = SimpleNamespace(level=EmergencyLevel.LEVEL_3)
    return manager


# =============================================================================
# The probe
# =============================================================================


class TestPanicThresholdProbe:
    """``evaluate()`` -- the instantaneous verdict, with no side effects."""

    @pytest.mark.parametrize(
        ("open_count", "total", "expected"),
        [
            (2, 2, False),
            (3, 3, True),
            (2, 3, False),
            (7, 10, True),
            (69, 100, False),
            (70, 100, True),
        ],
        ids=[
            "below_minimum_fleet",
            "at_minimum_fleet",
            "at_minimum_fleet_under_rate",
            "at_the_threshold",
            "one_below_the_threshold",
            "exactly_at_the_threshold",
        ],
    )
    def test_probe_boundary_grid(self, open_count, total, expected):
        """The two boundaries the config draws: fleet size, then OPEN rate."""
        names = [f"s{index}" for index in range(total)]
        monitor = _monitor(names[:open_count], names)

        assert monitor.evaluate().triggered is expected

    def test_probe_below_threshold_reports_the_rate_it_measured(self):
        """A verdict carries its own evidence, not just its boolean."""
        monitor = _monitor(
            ["s1", "s2", "s3"],
            ["s1", "s2", "s3", "s4", "s5", "s6"],
            threshold_percent=70.0,
        )

        result = monitor.evaluate()

        assert result.triggered is False
        assert result.open_rate == 50.0
        assert result.open_count == 3
        assert result.total_count == 6

    def test_probe_does_not_judge_a_fleet_below_the_minimum(self):
        """Two registered breakers are not a fleet, so nothing is concluded."""
        monitor = _monitor(["s1", "s2"], ["s1", "s2"])

        result = monitor.evaluate()

        assert result.triggered is False
        assert "Insufficient services" in result.reason

    def test_probe_names_the_open_circuits_it_counted(self):
        """The OPEN list is what an operator reads off the block message."""
        monitor = _tripping_monitor()

        result = monitor.evaluate()

        assert result.triggered is True
        assert result.open_rate == 80.0
        assert result.open_circuits == ["s1", "s2", "s3", "s4"]

    def test_probe_leaves_the_hysteresis_counter_untouched(self):
        """The probe never advances the escalation lane's counter."""
        monitor = _tripping_monitor()

        monitor.evaluate()
        monitor.evaluate()

        assert monitor._consecutive_triggers == 0

    def test_probe_never_touches_the_emergency_manager(self):
        """Side-effect freedom includes not reading the state it would write."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(emergency_manager=manager)

        monitor.evaluate()
        monitor.evaluate()

        manager.get_state.assert_not_called()
        manager.activate_auto.assert_not_called()

    def test_probe_propagates_an_unreadable_cluster_to_its_caller(self):
        """An unreadable fleet is raised, not folded into "not triggered".

        The consumer is a safety pre-check whose safe direction is *block*;
        an empty list would read as *allow*.
        """
        service = Mock(spec=CircuitBreakerService)
        service.repository.get_cluster_states.side_effect = (
            CircuitBreakerStateUnavailableError("get_cluster_states", "l2_timeout")
        )
        monitor = PanicThresholdMonitor(circuit_breaker_service=service)

        with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
            monitor.evaluate()

        assert excinfo.value.reason == "l2_timeout"

    def test_probe_raises_when_no_circuit_breaker_service_is_resolvable(self):
        """No service is the same failure class as no readable store."""
        monitor = PanicThresholdMonitor(config=PanicThresholdConfig())

        with patch.object(
            PanicThresholdMonitor, "cb_service", property(lambda self: None)
        ):
            with pytest.raises(CircuitBreakerStateUnavailableError) as excinfo:
                monitor.evaluate()

        assert excinfo.value.reason == "circuit_breaker_service_unavailable"

    def test_probe_result_carries_no_action_on_the_probe_lane(self):
        """``action_taken`` belongs to the escalation lane alone."""
        monitor = _tripping_monitor()

        assert monitor.evaluate().action_taken is None

    def test_probe_refreshes_the_observation_cache(self):
        """The convenience "was it triggered?" reader answers from this cache."""
        monitor = _tripping_monitor()

        result = monitor.evaluate()

        assert monitor.get_last_result() is result


# =============================================================================
# The tick -- hysteresis
# =============================================================================


class TestPanicThresholdTick:
    """Consecutive triggers, and the flag that gates the whole lane."""

    def test_tick_short_circuits_when_advanced_protection_is_disabled(self):
        """Disabled means the store is not even read."""
        service = Mock(spec=CircuitBreakerService)
        monitor = PanicThresholdMonitor(circuit_breaker_service=service)

        with _with_settings(_settings(enabled=False)):
            result = monitor.tick()

        assert result.triggered is False
        assert result.reason == "advanced protection disabled"
        service.repository.get_cluster_states.assert_not_called()

    def test_tick_waits_for_the_required_consecutive_triggers(self):
        """One trigger is not a collapse: the first tick reports and waits."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=2
        )

        with _with_settings(_settings()):
            result = monitor.tick()

        assert result.triggered is True
        assert "waiting for consecutive triggers (1/2)" in result.reason
        manager.activate_auto.assert_not_called()

    def test_tick_escalates_once_the_consecutive_count_is_met(self):
        """The second consecutive trigger crosses into the escalation lane."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=2
        )

        with _with_settings(_settings()), _with_stabilization(None):
            monitor.tick()
            result = monitor.tick()

        assert result.action_taken == "emergency_level_3_escalation"
        manager.activate_auto.assert_called_once()

    def test_tick_resets_the_counter_on_an_untriggered_tick(self):
        """A fleet that recovers between ticks starts the walk over."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=2
        )

        with _with_settings(_settings()):
            monitor.tick()
            assert monitor._consecutive_triggers == 1

            monitor._cb_service = _cb_service(["s1"], ["s1", "s2", "s3"])
            monitor.tick()

        assert monitor._consecutive_triggers == 0
        manager.activate_auto.assert_not_called()

    def test_tick_takes_the_alert_only_lane_without_escalating(self):
        """``alert_only`` warns and records the action; it declares nothing."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        with _with_settings(_settings(action="alert_only")):
            with capture_logs() as logs:
                result = monitor.tick()

        assert result.action_taken == "alert_only"
        manager.activate_auto.assert_not_called()
        assert [
            entry for entry in logs if entry["event"] == "panic_threshold.alert_only"
        ]


# =============================================================================
# The tick -- escalation policy
# =============================================================================


class TestPanicThresholdEscalationPolicy:
    """When a confirmed collapse may declare Level 3, and when it may not."""

    @staticmethod
    def _escalating(manager: Mock, *, period: int | None = STABILIZATION_PERIOD):
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )
        with _with_settings(_settings()), _with_stabilization(period):
            return monitor, monitor.tick()

    def test_escalation_declares_level_3_from_an_inactive_state(self):
        """The ordinary case: nothing is declared, so the collapse declares."""
        manager = _emergency(EmergencyLevel.NORMAL)

        _, result = self._escalating(manager, period=None)

        manager.activate_auto.assert_called_once_with(
            level=EmergencyLevel.LEVEL_3,
            reason="Panic Threshold: 80.0% of circuits are OPEN",
            duration_minutes=None,
        )
        assert result.action_taken == "emergency_level_3_escalation"

    def test_escalation_outranks_a_lower_active_level(self):
        """A LEVEL_1 from another subsystem does not suppress a fleet collapse."""
        manager = _emergency(EmergencyLevel.LEVEL_1)

        _, result = self._escalating(manager, period=None)

        manager.activate_auto.assert_called_once()
        assert result.action_taken == "emergency_level_3_escalation"

    def test_escalation_is_skipped_when_the_level_is_already_declared(self):
        """Re-declaring a live LEVEL_3 would restamp somebody else's incident."""
        manager = _emergency(EmergencyLevel.LEVEL_3)

        _, result = self._escalating(manager)

        manager.activate_auto.assert_not_called()
        assert result.action_taken is None

    def test_escalation_is_skipped_while_a_gradual_recovery_walks_the_level_down(self):
        """A recovery walk is under the operator's control and is not fought."""
        manager = _emergency(EmergencyLevel.LEVEL_2, is_recovering=True)

        _, result = self._escalating(manager, period=None)

        manager.activate_auto.assert_not_called()
        assert result.action_taken is None

    def test_escalation_is_skipped_inside_the_cooldown_after_an_observed_freeze(self):
        """The window is measured from the freeze this monitor saw lift.

        The breakers need the stabilization period to leave OPEN; re-declaring
        before they move would hold them for another whole window.
        """
        manager = _emergency(EmergencyLevel.LEVEL_3)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        with _with_settings(_settings()), _with_stabilization():
            monitor.tick()  # observes the freeze and stamps it
            manager.get_state.return_value = SimpleNamespace(
                level=EmergencyLevel.NORMAL,
                is_recovering=False,
                deactivated_at=None,
            )
            result = monitor.tick()

        assert monitor._last_seen_level3_at is not None
        manager.activate_auto.assert_not_called()
        assert result.action_taken is None

    def test_escalation_resumes_once_the_observed_cooldown_has_elapsed(self):
        """Past the window the collapse may declare again."""
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )
        monitor._last_seen_level3_at = utc_now() - timedelta(
            seconds=STABILIZATION_PERIOD + 60
        )

        with _with_settings(_settings()), _with_stabilization():
            result = monitor.tick()

        manager.activate_auto.assert_called_once()
        assert result.action_taken == "emergency_level_3_escalation"

    def test_escalation_falls_back_to_the_deactivation_stamp_on_a_fresh_process(self):
        """A process that has never seen a freeze reads the emergency stamp.

        The fallback covers exactly one case -- a restart -- and its precision
        cost is at most one window, the same acceptance the counter carries.
        """
        manager = _emergency(
            EmergencyLevel.NORMAL,
            deactivated_at=(utc_now() - timedelta(seconds=10)).isoformat(),
        )
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        with _with_settings(_settings()), _with_stabilization():
            result = monitor.tick()

        assert monitor._last_seen_level3_at is None
        manager.activate_auto.assert_not_called()
        assert result.action_taken is None

    def test_escalation_ignores_an_unparsable_deactivation_stamp(self):
        """A stamp that cannot be read is no window at all, not an open one."""
        manager = _emergency(EmergencyLevel.NORMAL, deactivated_at="not-a-timestamp")
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        with _with_settings(_settings()), _with_stabilization():
            result = monitor.tick()

        manager.activate_auto.assert_called_once()
        assert result.action_taken == "emergency_level_3_escalation"

    def test_escalation_is_not_suppressed_by_a_manual_lower_level_inside_the_window(
        self,
    ):
        """A LEVEL_1 arriving inside the window froze nothing.

        Its ``deactivated_at`` is cleared by the manual activation, so keying
        the cooldown on the emergency state would cut the window short here.
        The observed-freeze stamp keeps it running.
        """
        manager = _emergency(EmergencyLevel.LEVEL_3)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        with _with_settings(_settings()), _with_stabilization():
            monitor.tick()
            manager.get_state.return_value = SimpleNamespace(
                level=EmergencyLevel.LEVEL_1,
                is_recovering=False,
                deactivated_at=None,
            )
            result = monitor.tick()

        manager.activate_auto.assert_not_called()
        assert result.action_taken is None

    def test_escalation_is_skipped_when_no_emergency_manager_is_registered(self):
        """Without a manager there is nothing to declare through."""
        monitor = _tripping_monitor(consecutive_triggers_required=1)

        with (
            _with_settings(_settings()),
            patch(
                "baldur.factory.registry.ProviderRegistry.emergency_manager.safe_get",
                return_value=None,
            ),
        ):
            result = monitor.tick()

        assert result.triggered is True
        assert result.action_taken is None


# =============================================================================
# The tick -- escalation confirmation
# =============================================================================


class TestPanicThresholdEscalationConfirmed:
    """The audit record follows the level that actually moved."""

    @staticmethod
    def _tick_with_result(manager: Mock):
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )
        with _with_settings(_settings()), _with_stabilization(None):
            with (
                patch(
                    "baldur.services.circuit_breaker.panic_threshold."
                    "log_panic_threshold_audit"
                ) as audit,
                capture_logs() as logs,
            ):
                result = monitor.tick()
        return result, audit, logs

    def test_confirmed_escalation_records_the_audit_and_the_critical_line(self):
        """A level that moved is the one that owes an audit trail."""
        manager = _emergency(EmergencyLevel.NORMAL)

        result, audit, logs = self._tick_with_result(manager)

        assert result.action_taken == "emergency_level_3_escalation"
        audit.assert_called_once()
        assert audit.call_args.kwargs["open_rate"] == 80.0
        assert audit.call_args.kwargs["threshold"] == 70.0
        assert audit.call_args.kwargs["open_count"] == 4
        assert audit.call_args.kwargs["total_count"] == 5
        assert audit.call_args.kwargs["action_taken"] == "emergency_level_3_escalation"
        assert [
            entry for entry in logs if entry["event"] == "panic_threshold.triggered"
        ]

    def test_a_kill_switched_activation_records_no_audit(self):
        """``activate_auto`` returns the *unchanged* state when kill-switched.

        Assuming the declaration succeeded would file an audit record and a
        CRITICAL line for a lockdown that never happened.
        """
        manager = _emergency(EmergencyLevel.NORMAL)
        manager.activate_auto.return_value = SimpleNamespace(
            level=EmergencyLevel.NORMAL
        )

        result, audit, logs = self._tick_with_result(manager)

        assert result.action_taken is None
        audit.assert_not_called()
        blocked = [
            entry
            for entry in logs
            if entry["event"] == "panic_threshold.escalation_blocked"
        ]
        assert len(blocked) == 1
        assert blocked[0]["resulting_level"] == EmergencyLevel.NORMAL.value

    def test_an_unreadable_activation_result_records_no_audit(self):
        """A manager answering with something unreadable is not a confirmation."""
        manager = _emergency(EmergencyLevel.NORMAL)
        manager.activate_auto.return_value = None

        result, audit, _ = self._tick_with_result(manager)

        assert result.action_taken is None
        audit.assert_not_called()


# =============================================================================
# The tick -- store outage
# =============================================================================


class TestPanicThresholdClusterUnavailable:
    """A store outage must not make the scheduler log a traceback per tick."""

    @staticmethod
    def _outage_monitor() -> PanicThresholdMonitor:
        service = Mock(spec=CircuitBreakerService)
        service.repository.get_cluster_states.side_effect = (
            CircuitBreakerStateUnavailableError(
                "get_cluster_states", "backend_degraded"
            )
        )
        return PanicThresholdMonitor(
            config=PanicThresholdConfig(consecutive_triggers_required=2),
            circuit_breaker_service=service,
        )

    def test_tick_returns_instead_of_raising_when_the_cluster_is_unreadable(self):
        """The tick absorbs what the probe raises, and says which read failed."""
        monitor = self._outage_monitor()

        with _with_settings(_settings()):
            with capture_logs() as logs:
                result = monitor.tick()

        assert result.triggered is False
        assert result.reason == "cluster state unavailable"
        failures = [
            entry
            for entry in logs
            if entry["event"] == "panic_threshold.cluster_read_failed"
        ]
        assert len(failures) == 1
        assert failures[0]["log_level"] == "warning"
        assert failures[0]["reason"] == "backend_degraded"

    def test_an_outage_does_not_reset_the_hysteresis_counter(self):
        """A transient store failure is not evidence that the fleet recovered."""
        monitor = self._outage_monitor()
        monitor._consecutive_triggers = 1

        with _with_settings(_settings()):
            monitor.tick()

        assert monitor._consecutive_triggers == 1


# =============================================================================
# The tick -- settings refresh
# =============================================================================


class TestPanicThresholdConfigRefresh:
    """A settings change reaches a long-lived monitor without a new monitor.

    ``enabled`` is read per tick, so the threshold and the action judged
    against it must be too -- otherwise a runtime reset would take effect for
    one field and not the others.
    """

    @pytest.fixture(autouse=True)
    def _clean_settings(self):
        from baldur.settings.circuit_breaker_advanced import (
            reset_circuit_breaker_advanced_settings,
        )

        reset_circuit_breaker_advanced_settings()
        yield
        reset_circuit_breaker_advanced_settings()

    @staticmethod
    def _apply(monkeypatch, **env: str) -> None:
        from baldur.settings.circuit_breaker_advanced import (
            reset_circuit_breaker_advanced_settings,
        )

        for name, value in env.items():
            monkeypatch.setenv(name, value)
        reset_circuit_breaker_advanced_settings()

    def test_a_raised_threshold_takes_effect_on_the_next_tick(self, monkeypatch):
        """80% OPEN stops triggering once the threshold is raised past it."""
        self._apply(
            monkeypatch,
            BALDUR_CB_ADVANCED_ENABLED="true",
            BALDUR_CB_ADVANCED_PANIC_THRESHOLD_PERCENT="90.0",
        )
        monitor = _tripping_monitor(
            emergency_manager=_emergency(EmergencyLevel.NORMAL),
            consecutive_triggers_required=1,
        )

        result = monitor.tick()

        assert monitor.config.threshold_percent == 90.0
        assert result.triggered is False

    def test_a_changed_action_takes_effect_on_the_next_tick(self, monkeypatch):
        """The action is refreshed alongside the flag that gates the lane."""
        self._apply(
            monkeypatch,
            BALDUR_CB_ADVANCED_ENABLED="true",
            BALDUR_CB_ADVANCED_PANIC_THRESHOLD_ACTION="alert_only",
        )
        manager = _emergency(EmergencyLevel.NORMAL)
        monitor = _tripping_monitor(
            emergency_manager=manager, consecutive_triggers_required=1
        )

        result = monitor.tick()

        assert monitor.config.action == "alert_only"
        assert result.action_taken == "alert_only"
        manager.activate_auto.assert_not_called()

    def test_the_refresh_preserves_the_fields_settings_do_not_own(self, monkeypatch):
        """Only the two settings-backed fields are replaced."""
        self._apply(monkeypatch, BALDUR_CB_ADVANCED_ENABLED="true")
        monitor = _tripping_monitor(
            emergency_manager=_emergency(EmergencyLevel.NORMAL),
            consecutive_triggers_required=3,
            min_registered_services=7,
        )

        monitor.tick()

        assert monitor.config.consecutive_triggers_required == 3
        assert monitor.config.min_registered_services == 7


# =============================================================================
# The result object
# =============================================================================


class TestPanicThresholdResultContract:
    """The observation the two lanes share."""

    def test_result_defaults_describe_an_untriggered_observation(self):
        """A bare result is safe to read: nothing triggered, nothing acted on."""
        result = PanicThresholdResult()

        assert result.triggered is False
        assert result.open_rate == 0.0
        assert result.open_count == 0
        assert result.total_count == 0
        assert result.open_circuits == []
        assert result.action_taken is None
        assert result.reason is None

    def test_result_carries_the_observation_and_the_action_separately(self):
        """``action_taken`` is set by the escalation lane, never by the probe."""
        result = PanicThresholdResult(
            triggered=True,
            open_rate=75.0,
            open_count=3,
            total_count=4,
            open_circuits=["a", "b", "c"],
            action_taken="emergency_level_3_escalation",
        )

        assert result.triggered is True
        assert result.open_rate == 75.0
        assert result.open_count == 3
        assert result.action_taken == "emergency_level_3_escalation"
