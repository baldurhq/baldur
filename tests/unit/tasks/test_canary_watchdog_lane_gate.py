"""Canary watchdog beat-lane composition gate.

The lane's three beat entries were composed on every Celery install while no
worker could resolve their names. Registration alone would have started
automatic promotion and rollback on installs that never had either, so the
activation carries a two-step gate plus a per-entry operator switch:

    A. ``_canary_watchdog_lane_enabled`` — PRO distribution present, THEN an
       ACTIVE entitlement verdict. Presence answers first and answers alone on
       an OSS install, so no licence read happens on a tier the lane cannot
       serve; an indeterminate verdict skips the lane (tier boundary).
    B. ``_disabled_beat_entry_keys`` — the operator's
       ``BALDUR_SCHEDULER_DISABLED_JOBS`` names, applied per entry so the two
       lanes (beat and in-process) drop the same job under the same spelling.
       A settings fault leaves every entry composed (convenience knob).
    C. ``get_canary_watchdog_beat_schedule`` — an empty dict when gated out,
       three entries otherwise, each with a cadence and an ``expires`` below it.

No PRO import: deciding without one is the whole point of the gate.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.core.entitlement import EntitlementResult, EntitlementStatus
from baldur.settings.scheduler import SchedulerSettings
from baldur.tasks.canary_watchdog import (
    _BEAT_ENTRY_JOB_NAMES,
    _canary_watchdog_lane_enabled,
    _disabled_beat_entry_keys,
    get_canary_watchdog_beat_schedule,
)

# Cadence (seconds) each entry advertises, paired with its entry key. The
# ``expires`` boundary below is asserted against these rather than against a
# repeated literal.
_ENTRY_CADENCE_SECONDS: dict[str, int] = {
    "canary-scan-zombie-rollouts": 5 * 60,
    "canary-auto-promote-eligible": 1 * 60,
    "canary-collect-metrics": 2 * 60,
}


def _entitlement(status: EntitlementStatus):
    """Patch the verdict producer at the module attribute the gate imports."""
    return patch(
        "baldur.core.entitlement.get_entitlement_status",
        return_value=EntitlementResult(status=status),
    )


def _disable(*job_names: str):
    """Patch the scheduler settings to report ``job_names`` as disabled.

    A real settings object rather than a stub, so the comma-joined spelling an
    operator actually writes into ``BALDUR_SCHEDULER_DISABLED_JOBS`` goes
    through the same parsing the in-process scheduler reads it with.
    """
    return patch(
        "baldur.settings.scheduler.get_scheduler_settings",
        return_value=SchedulerSettings(disabled_jobs=",".join(job_names)),
    )


@pytest.fixture
def entitled_pro_install(mock_pro_tier):
    """PRO installed and the verdict ACTIVE — the lane composes."""
    with _entitlement(EntitlementStatus.ACTIVE):
        yield


@pytest.fixture
def no_disabled_jobs():
    """Operator disable list empty, so per-entry filtering drops nothing.

    Pinned rather than inherited from the ambient environment: a stray
    ``BALDUR_SCHEDULER_DISABLED_JOBS`` would otherwise silently shrink the
    schedule every composition case here asserts against.
    """
    with _disable():
        yield


# =============================================================================
# A. Lane gate ladder
# =============================================================================


class TestCanaryWatchdogLaneGateBehavior:
    """The gate ladder decides composition; each rung fails in one direction."""

    def test_pro_absent_disables_the_lane(self, mock_oss_tier):
        """Every task in the lane needs the PRO canary service — no PRO, no lane."""
        assert _canary_watchdog_lane_enabled() is False

    def test_pro_absent_never_reads_the_entitlement_verdict(self, mock_oss_tier):
        """The short-circuit is load-bearing, so it is asserted directly.

        Reading the verdict on an OSS install adds a licence-file read, an INFO
        line and two entitlement gauge writes to a tier this lane cannot serve.
        Without this assertion the ordering is invisible to tests.
        """
        with patch("baldur.core.entitlement.get_entitlement_status") as mock_verdict:
            assert _canary_watchdog_lane_enabled() is False

        mock_verdict.assert_not_called()

    def test_pro_present_and_active_verdict_enables_the_lane(self, mock_pro_tier):
        """Both rungs pass — the lane composes."""
        with _entitlement(EntitlementStatus.ACTIVE):
            assert _canary_watchdog_lane_enabled() is True

    @pytest.mark.parametrize(
        "status",
        [EntitlementStatus.MISSING, EntitlementStatus.INVALID],
        ids=["missing", "invalid"],
    )
    def test_non_active_verdict_disables_the_lane(self, mock_pro_tier, status):
        """Canary rollouts are a licensed capability; presence is not enough."""
        with _entitlement(status):
            assert _canary_watchdog_lane_enabled() is False

    def test_unreadable_verdict_disables_the_lane(self, mock_pro_tier):
        """Indeterminate reads as not entitled.

        Same direction as the rollout *creation* surface, which is registry-gated
        and equally unavailable without an ACTIVE verdict — so the automation
        half is never the only half of the feature left live.
        """
        with patch(
            "baldur.core.entitlement.get_entitlement_status",
            side_effect=RuntimeError("licence store down"),
        ):
            assert _canary_watchdog_lane_enabled() is False


# =============================================================================
# B. Gated output and the per-entry operator filter
# =============================================================================


class TestCanaryWatchdogBeatScheduleBehavior:
    """The getter composes nothing when gated out, and drops named entries."""

    def test_gated_out_lane_returns_an_empty_schedule(self, mock_oss_tier):
        """No entries at all — not entries naming a lane that cannot run."""
        assert get_canary_watchdog_beat_schedule() == {}

    def test_entitled_install_composes_every_entry(
        self, entitled_pro_install, no_disabled_jobs
    ):
        """The full lane composes: exactly the three known entry keys."""
        schedule = get_canary_watchdog_beat_schedule()

        assert set(schedule) == set(_BEAT_ENTRY_JOB_NAMES)

    def test_named_job_drops_only_its_own_entry(
        self, entitled_pro_install, no_disabled_jobs
    ):
        """One disabled name removes one entry and leaves the other two."""
        with _disable("auto_promote_eligible"):
            schedule = get_canary_watchdog_beat_schedule()

        assert set(schedule) == set(_BEAT_ENTRY_JOB_NAMES) - {
            "canary-auto-promote-eligible"
        }

    def test_all_three_names_drop_the_whole_lane(
        self, entitled_pro_install, no_disabled_jobs
    ):
        """Disabling every job name empties the schedule without a tier change."""
        with _disable(*_BEAT_ENTRY_JOB_NAMES.values()):
            schedule = get_canary_watchdog_beat_schedule()

        assert schedule == {}

    def test_disabled_entry_drop_is_reported(
        self, entitled_pro_install, no_disabled_jobs
    ):
        """Each dropped entry leaves an INFO line naming the in-process job name.

        The operator spells the job once and it disappears from two lanes; this
        breadcrumb is what tells them the beat lane honoured the spelling.
        """
        import baldur.tasks.canary_watchdog as watchdog_module

        with _disable("collect_canary_metrics"):
            with patch.object(watchdog_module, "logger") as mock_logger:
                get_canary_watchdog_beat_schedule()

        logged_jobs = [
            call.kwargs["job"]
            for call in mock_logger.info.call_args_list
            if call.args and call.args[0] == "canary_watchdog.job_disabled_by_settings"
        ]
        assert logged_jobs == ["collect_canary_metrics"]

    def test_unknown_disabled_name_drops_nothing(
        self, entitled_pro_install, no_disabled_jobs
    ):
        """A name matching no canary job leaves the lane whole.

        The disable list is shared with the in-process scheduler, which owns the
        unknown-name WARNING; this filter must not react to names not its own.
        """
        with _disable("sla_drift"):
            schedule = get_canary_watchdog_beat_schedule()

        assert set(schedule) == set(_BEAT_ENTRY_JOB_NAMES)

    def test_beat_entry_keys_map_to_the_in_process_job_names(self):
        """One spelling, two lanes: the map's values are the plain task names."""
        assert set(_BEAT_ENTRY_JOB_NAMES.values()) == {
            "scan_zombie_rollouts",
            "auto_promote_eligible",
            "collect_canary_metrics",
        }

    def test_settings_fault_leaves_every_entry_composed(self):
        """Fail-open: a convenience knob must not silently stop the lane."""
        with patch(
            "baldur.settings.scheduler.get_scheduler_settings",
            side_effect=RuntimeError("settings unavailable"),
        ):
            assert _disabled_beat_entry_keys() == set()

    def test_settings_fault_is_reported_at_warning(self):
        """The fail-open path is not silent — the operator's knob went unread."""
        import baldur.tasks.canary_watchdog as watchdog_module

        with patch(
            "baldur.settings.scheduler.get_scheduler_settings",
            side_effect=RuntimeError("settings unavailable"),
        ):
            with patch.object(watchdog_module, "logger") as mock_logger:
                _disabled_beat_entry_keys()

        assert [call.args[0] for call in mock_logger.warning.call_args_list] == [
            "canary_watchdog.scheduler_settings_unavailable"
        ]


# =============================================================================
# C. Composed entry shape
# =============================================================================


class TestCanaryWatchdogBeatScheduleContract:
    """The three composed entries carry the exact names, queues and expiries."""

    @pytest.fixture(autouse=True)
    def _composed(self, entitled_pro_install, no_disabled_jobs):
        """Every case here needs the lane actually composed."""

    def test_entries_name_the_registered_dotted_tasks(self):
        """Beat publishes these strings; a worker resolves them or the lane is dead."""
        schedule = get_canary_watchdog_beat_schedule()

        assert {key: entry["task"] for key, entry in schedule.items()} == {
            "canary-scan-zombie-rollouts": (
                "baldur.tasks.canary_watchdog.scan_zombie_rollouts"
            ),
            "canary-auto-promote-eligible": (
                "baldur.tasks.canary_watchdog.auto_promote_eligible"
            ),
            "canary-collect-metrics": (
                "baldur.tasks.canary_watchdog.collect_canary_metrics"
            ),
        }

    def test_entries_route_to_their_named_queues(self):
        """Queue choice is an operator-visible contract, not an implementation
        detail: the promotion check is realtime, the scan is maintenance."""
        schedule = get_canary_watchdog_beat_schedule()

        assert {key: entry["options"]["queue"] for key, entry in schedule.items()} == {
            "canary-scan-zombie-rollouts": "maintenance",
            "canary-auto-promote-eligible": "realtime",
            "canary-collect-metrics": "metrics",
        }

    def test_entries_carry_their_documented_cadences(self):
        """5 / 1 / 2 minutes — the same cadences the in-process twin registers."""
        from celery.schedules import crontab

        schedule = get_canary_watchdog_beat_schedule()

        assert {key: entry["schedule"] for key, entry in schedule.items()} == {
            "canary-scan-zombie-rollouts": crontab(minute="*/5"),
            "canary-auto-promote-eligible": crontab(minute="*/1"),
            "canary-collect-metrics": crontab(minute="*/2"),
        }

    @pytest.mark.parametrize(
        ("entry_key", "expires"),
        [
            ("canary-scan-zombie-rollouts", 240),
            ("canary-auto-promote-eligible", 50),
            ("canary-collect-metrics", 90),
        ],
    )
    def test_expires_stays_below_its_own_cadence(self, entry_key, expires):
        """Boundary: a task that keeps failing must expire before the next tick.

        At or above the cadence the retries pile up instead of being handed to
        the next publication, which is what the retry policy exists to avoid.
        """
        schedule = get_canary_watchdog_beat_schedule()

        assert schedule[entry_key]["options"]["expires"] == expires
        assert expires < _ENTRY_CADENCE_SECONDS[entry_key]
