"""
Beat Schedule Chaos Integration Tests (385).

Tests for:
A. _SCHEDULE_MODULES — chaos_scheduler entry exists
B. register_all_tasks_with_celery — chaos tasks registered
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# A. _SCHEDULE_MODULES — chaos_scheduler entry
# =============================================================================


class TestScheduleModulesChaosContract:
    """Contract verification for chaos_scheduler in _SCHEDULE_MODULES (Section 4 Fix 3)."""

    def test_chaos_scheduler_in_schedule_modules(self):
        """_SCHEDULE_MODULES contains chaos_scheduler entry."""
        from baldur.adapters.celery.beat_schedule import _SCHEDULE_MODULES

        module_names = [entry[0] for entry in _SCHEDULE_MODULES]
        assert "chaos_scheduler" in module_names

    def test_chaos_scheduler_module_path(self):
        """chaos_scheduler entry points to correct module and getter function."""
        from baldur.adapters.celery.beat_schedule import _SCHEDULE_MODULES

        chaos_entry = [e for e in _SCHEDULE_MODULES if e[0] == "chaos_scheduler"][0]
        assert chaos_entry[1] == "baldur.tasks.chaos_scheduler"
        assert chaos_entry[2] == "get_beat_schedule_for_celery"


# =============================================================================
# B. register_all_tasks_with_celery — chaos tasks
# =============================================================================


class TestRegisterAllTasksChaosSchedulerBehavior:
    """Behavior verification for register_all_tasks_with_celery chaos registration."""

    def test_calls_register_chaos_tasks(self):
        """register_all_tasks_with_celery invokes chaos scheduler task registration."""
        pytest.importorskip("baldur_pro")
        from baldur.adapters.celery.beat_schedule import (
            register_all_tasks_with_celery,
        )

        mock_app = MagicMock()

        with (
            patch(
                "baldur.tasks.chaos_scheduler.register_celery_tasks",
                autospec=True,
            ) as mock_register_chaos,
            patch(
                "baldur.tasks.intelligence_tasks.register_intelligence_tasks_with_celery",
                autospec=True,
            ),
            patch(
                "baldur.tasks.compliance_tasks.register_compliance_tasks_with_celery",
                autospec=True,
            ),
            patch(
                "baldur.tasks.traffic_aware_replay.register_traffic_aware_tasks_with_celery",
                autospec=True,
            ),
            # 599 D10 - private lanes are imported inside the function;
            # patch them so the mock app never reaches the real class wrapping.
            patch(
                "baldur_pro.services.finops.tasks.register_finops_tasks_with_celery",
                autospec=True,
            ),
            patch(
                "baldur_dormant.services.compliance.tasks.register_compliance_check_tasks_with_celery",
                autospec=True,
            ),
            patch(
                "baldur_dormant.services.learning.tasks.register_learning_tasks_with_celery",
                autospec=True,
            ),
        ):
            register_all_tasks_with_celery(mock_app)

        mock_register_chaos.assert_called_once_with(mock_app)
