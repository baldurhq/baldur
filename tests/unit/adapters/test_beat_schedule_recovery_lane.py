"""``include_recovery`` composes the recovery coordination lane.

793 D8. The recovery trigger check, the mid-recovery health monitor, the
stale-approval sweep and session cleanup are one lane of the beat composition,
composed by default wherever the PRO distribution is installed: a PRO
deployment with a beat starts and drives recoveries by itself.
``include_recovery=False`` keeps automatic staged recovery off the schedule;
every entry names a declared queue, so the schedule validator raises no
warning for the lane.

The lane's module lives in the PRO distribution, so this module skips where
that distribution is absent (the composition then logs the lane at DEBUG,
which the private-lane suite pins).

Verification techniques applied:
- Boundary: ``True`` composes the four entries, ``False`` drops exactly them,
  the other lanes are unaffected either way; the same through the Celery app
  wrapper
- Contract: the validator's warnings carry no recovery entry
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_pro

pytest.importorskip("baldur_pro.services.coordination.recovery_tasks")

from baldur.adapters.celery.beat_schedule import (
    _reset_celery_configured,
    configure_baldur_celery,
    get_baldur_beat_schedule,
    validate_schedule,
)

RECOVERY_ENTRIES = frozenset(
    {
        "check-recovery-trigger-every-minute",
        "monitor-recovery-health-every-30s",
        "check-stale-pending-every-10min",
        "cleanup-old-sessions-daily",
    }
)


class TestRecoveryLaneCompositionBehavior:
    """The lane's presence follows the flag, and only the lane's."""

    def test_default_composition_includes_the_four_recovery_entries(self):
        schedule = get_baldur_beat_schedule()

        assert RECOVERY_ENTRIES <= set(schedule)

    def test_include_recovery_false_drops_exactly_the_recovery_entries(self):
        """Boundary: the difference between the two compositions is the lane."""
        with_lane = get_baldur_beat_schedule(include_recovery=True)
        without_lane = get_baldur_beat_schedule(include_recovery=False)

        assert set(with_lane) - set(without_lane) == RECOVERY_ENTRIES
        assert RECOVERY_ENTRIES.isdisjoint(without_lane)
        # Every other lane composes identically either way.
        for name in without_lane:
            assert with_lane[name] == without_lane[name]

    def test_recovery_entries_come_from_the_lane_getter(self):
        """The composed entries are the lane's own, not a restatement."""
        from baldur_pro.services.coordination.recovery_tasks import (
            get_recovery_beat_schedule,
        )

        schedule = get_baldur_beat_schedule(include_recovery=True)

        for name, entry in get_recovery_beat_schedule().items():
            assert schedule[name] == entry

    def test_configure_baldur_celery_forwards_the_flag(self):
        """The Celery wrapper composes the lane, and drops it, by the same flag."""
        celery = pytest.importorskip("celery")
        current_before = celery.current_app._get_current_object()
        try:
            composed = celery.Celery("recovery_lane_on", set_as_current=False)
            _reset_celery_configured()
            configure_baldur_celery(composed, include_recovery=True)
            _reset_celery_configured()

            dropped = celery.Celery("recovery_lane_off", set_as_current=False)
            configure_baldur_celery(dropped, include_recovery=False)
        finally:
            _reset_celery_configured()

        assert celery.current_app._get_current_object() is current_before
        assert RECOVERY_ENTRIES <= set(composed.conf.beat_schedule)
        assert RECOVERY_ENTRIES.isdisjoint(dropped.conf.beat_schedule)
        # The lane's tasks are registered on the app that composes them.
        assert "baldur.check_recovery_trigger" in composed.tasks
        assert "baldur.execute_recovery_step" in composed.tasks

    def test_validator_raises_no_warning_for_the_lane(self):
        """Contract: every recovery entry names a declared queue."""
        report = validate_schedule()

        assert report["valid"] is True
        recovery_warnings = [
            warning
            for warning in report["warnings"]
            if warning.split(":")[0] in RECOVERY_ENTRIES
        ]
        assert recovery_warnings == []
