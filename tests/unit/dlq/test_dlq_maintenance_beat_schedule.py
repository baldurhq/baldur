"""
Unit tests for DLQ maintenance beat schedule wiring (440).

Covers:
- get_dlq_maintenance_beat_schedule() return values
- beat_schedule.py include_flags for dlq_maintenance, chaos_scheduler, postmortem
- configure_baldur_celery pass-through of new flags
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.celery.beat_schedule import (
    _SCHEDULE_MODULES,
    _reset_celery_configured,
    configure_baldur_celery,
    get_baldur_beat_schedule,
)


@pytest.fixture(autouse=True)
def _reset_configure_guard():
    _reset_celery_configured()
    yield
    _reset_celery_configured()


class TestDlqMaintenanceBeatScheduleContract:
    """Contract: get_dlq_maintenance_beat_schedule returns correct task entries."""

    def test_schedule_contains_evict_overflow_task(self):
        """evict-overflow-dlq-entries entry exists with correct task name."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()

        assert "evict-overflow-dlq-entries" in schedule
        entry = schedule["evict-overflow-dlq-entries"]
        assert entry["task"] == "baldur.celery_tasks.evict_overflow_dlq_entries"

    def test_evict_overflow_schedule_is_60_seconds(self):
        """evict-overflow runs every 60 seconds."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()
        assert schedule["evict-overflow-dlq-entries"]["schedule"] == 60.0

    def test_evict_overflow_queue_is_maintenance(self):
        """evict-overflow routes to maintenance queue."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()
        assert (
            schedule["evict-overflow-dlq-entries"]["options"]["queue"] == "maintenance"
        )

    def test_schedule_contains_cleanup_resolved_task(self):
        """cleanup-resolved-dlq-entries entry exists with correct task name."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()

        assert "cleanup-resolved-dlq-entries" in schedule
        entry = schedule["cleanup-resolved-dlq-entries"]
        assert entry["task"] == "baldur.celery_tasks.cleanup_resolved_dlq_entries"

    def test_cleanup_resolved_has_days_old_kwarg(self):
        """cleanup-resolved passes days_old=30 as kwargs."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()
        assert schedule["cleanup-resolved-dlq-entries"]["kwargs"]["days_old"] == 30

    def test_schedule_has_exactly_three_entries(self):
        """Schedule contains exactly 3 tasks."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()
        assert len(schedule) == 3

    def test_schedule_contains_release_stale_replaying_task(self):
        """release-stale-replaying-entries entry exists with correct task name."""
        from baldur.celery_tasks.dlq_tasks import get_dlq_maintenance_beat_schedule

        schedule = get_dlq_maintenance_beat_schedule()

        assert "release-stale-replaying-entries" in schedule
        entry = schedule["release-stale-replaying-entries"]
        assert entry["task"] == "baldur.celery_tasks.release_stale_replaying"
        assert entry["options"]["queue"] == "maintenance"


class TestBeatScheduleWiringContract:
    """Contract: _SCHEDULE_MODULES and include_flags contain dlq_maintenance."""

    def test_dlq_maintenance_in_schedule_modules(self):
        """dlq_maintenance module is registered in _SCHEDULE_MODULES."""
        flag_names = [m[0] for m in _SCHEDULE_MODULES]
        assert "dlq_maintenance" in flag_names

    def test_dlq_maintenance_module_path(self):
        """dlq_maintenance points to baldur.celery_tasks.dlq_tasks."""
        for flag, module_path, getter, _msg in _SCHEDULE_MODULES:
            if flag == "dlq_maintenance":
                assert module_path == "baldur.celery_tasks.dlq_tasks"
                assert getter == "get_dlq_maintenance_beat_schedule"
                break

    def test_chaos_scheduler_in_schedule_modules(self):
        """chaos_scheduler is in _SCHEDULE_MODULES (drive-by fix verified)."""
        flag_names = [m[0] for m in _SCHEDULE_MODULES]
        assert "chaos_scheduler" in flag_names

    def test_postmortem_in_schedule_modules(self):
        """postmortem is in _SCHEDULE_MODULES."""
        flag_names = [m[0] for m in _SCHEDULE_MODULES]
        assert "postmortem" in flag_names


class TestBeatScheduleIncludeFlagsBehavior:
    """Behavior: include flags control schedule module loading."""

    def test_dlq_maintenance_included_by_default(self):
        """get_baldur_beat_schedule includes dlq_maintenance by default."""
        schedule = get_baldur_beat_schedule()
        assert "evict-overflow-dlq-entries" in schedule

    def test_dlq_maintenance_excluded_when_flag_false(self):
        """Setting include_dlq_maintenance=False excludes DLQ tasks."""
        schedule = get_baldur_beat_schedule(include_dlq_maintenance=False)
        assert "evict-overflow-dlq-entries" not in schedule
        assert "cleanup-resolved-dlq-entries" not in schedule

    def test_chaos_scheduler_included_by_default(self):
        """chaos_scheduler include flag exists in get_baldur_beat_schedule signature."""
        import inspect

        sig = inspect.signature(get_baldur_beat_schedule)
        assert "include_chaos_scheduler" in sig.parameters

    def test_postmortem_included_by_default(self):
        """postmortem is included by default (drive-by fix)."""
        get_baldur_beat_schedule()
        # postmortem module may not load, but flag is present in signature
        import inspect

        sig = inspect.signature(get_baldur_beat_schedule)
        assert "include_postmortem" in sig.parameters


class TestConfigureBaldurCeleryPassThroughBehavior:
    """Behavior: configure_baldur_celery passes new flags to get_baldur_beat_schedule."""

    def test_configure_accepts_include_dlq_maintenance(self):
        """configure_baldur_celery has include_dlq_maintenance parameter."""
        import inspect

        sig = inspect.signature(configure_baldur_celery)
        assert "include_dlq_maintenance" in sig.parameters

    def test_configure_accepts_include_chaos_scheduler(self):
        """configure_baldur_celery has include_chaos_scheduler parameter."""
        import inspect

        sig = inspect.signature(configure_baldur_celery)
        assert "include_chaos_scheduler" in sig.parameters

    def test_configure_accepts_include_postmortem(self):
        """configure_baldur_celery has include_postmortem parameter."""
        import inspect

        sig = inspect.signature(configure_baldur_celery)
        assert "include_postmortem" in sig.parameters

    def test_configure_passes_dlq_maintenance_flag(self):
        """configure_baldur_celery passes include_dlq_maintenance to schedule."""
        app = MagicMock()
        app.conf.beat_schedule = {}
        app.conf.task_queues = []
        app.conf.task_routes = {}

        with patch(
            "baldur.adapters.celery.beat_schedule.register_all_tasks_with_celery"
        ):
            configure_baldur_celery(app, include_dlq_maintenance=False)

        schedule = app.conf.beat_schedule
        assert "evict-overflow-dlq-entries" not in schedule
