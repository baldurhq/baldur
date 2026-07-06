"""
Hunt Zombie Experiments Task Tests (385).

Tests for:
A. hunt_zombie_experiments() — cleanup delegation, SoftTimeLimitExceeded guard (H-9)
B. register_celery_tasks() — hunt_zombie_experiments_task registration
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_CHAOS_SVC = "baldur_pro.services.chaos"
_IDEMPOTENCY = "baldur.services.idempotency"


# =============================================================================
# A. hunt_zombie_experiments — cleanup delegation + SoftTimeLimitExceeded
# =============================================================================


class TestHuntZombieExperimentsCleanupDelegationBehavior:
    """Behavior verification for hunt_zombie_experiments cleanup delegation (385)."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_delegates_to_cleanup_zombie_experiment(self):
        """Expired experiment → calls scheduler.cleanup_zombie_experiment()."""
        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        mock_scheduler = MagicMock()
        mock_experiment = MagicMock()
        mock_experiment.experiment_id = "exp-zombie"
        mock_experiment._is_expired_monotonic.return_value = True
        mock_scheduler.get_experiments_by_status.return_value = [mock_experiment]
        mock_scheduler.cleanup_zombie_experiment.return_value = True

        mock_idempotency = MagicMock()
        mock_idempotency.acquire_lock.return_value = True

        with (
            patch(f"{_CHAOS_SVC}.get_chaos_scheduler", return_value=mock_scheduler),
            patch(f"{_IDEMPOTENCY}.IdempotencyService", return_value=mock_idempotency),
        ):
            result = hunt_zombie_experiments()

        assert result["success"] is True
        assert result["hunted"] == 1
        mock_scheduler.cleanup_zombie_experiment.assert_called_once_with(
            "exp-zombie",
            "zombie_hunter",
        )

    def test_releases_lock_after_cleanup(self):
        """Distributed lock is always released after cleanup attempt."""
        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        mock_scheduler = MagicMock()
        mock_experiment = MagicMock()
        mock_experiment.experiment_id = "exp-1"
        mock_experiment._is_expired_monotonic.return_value = True
        mock_scheduler.get_experiments_by_status.return_value = [mock_experiment]
        mock_scheduler.cleanup_zombie_experiment.side_effect = RuntimeError("boom")

        mock_idempotency = MagicMock()
        mock_idempotency.acquire_lock.return_value = True

        with (
            patch(f"{_CHAOS_SVC}.get_chaos_scheduler", return_value=mock_scheduler),
            patch(f"{_IDEMPOTENCY}.IdempotencyService", return_value=mock_idempotency),
        ):
            result = hunt_zombie_experiments()

        # Lock released despite exception
        mock_idempotency.release_lock.assert_called_once()
        # Error recorded
        assert len(result["errors"]) == 1

    def test_soft_time_limit_breaks_loop_skipping_remaining(self):
        """SoftTimeLimitExceeded mid-loop breaks loop, skipping remaining experiments (H-9).

        Uses 3 experiments: exp1 processed, exp2 raises SoftTimeLimitExceeded,
        exp3 must NOT be processed — proving the loop actually breaks.
        """
        from celery.exceptions import SoftTimeLimitExceeded

        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        mock_scheduler = MagicMock()
        exp1 = MagicMock()
        exp1.experiment_id = "exp-1"
        exp1._is_expired_monotonic.return_value = True

        exp2 = MagicMock()
        exp2.experiment_id = "exp-2"
        exp2._is_expired_monotonic.side_effect = SoftTimeLimitExceeded()

        exp3 = MagicMock()
        exp3.experiment_id = "exp-3"
        exp3._is_expired_monotonic.return_value = True

        mock_scheduler.get_experiments_by_status.return_value = [exp1, exp2, exp3]
        mock_scheduler.cleanup_zombie_experiment.return_value = True

        mock_idempotency = MagicMock()
        mock_idempotency.acquire_lock.return_value = True

        with (
            patch(f"{_CHAOS_SVC}.get_chaos_scheduler", return_value=mock_scheduler),
            patch(f"{_IDEMPOTENCY}.IdempotencyService", return_value=mock_idempotency),
        ):
            result = hunt_zombie_experiments()

        # Given — exp1 processed, exp2 interrupted, exp3 never reached
        assert result["success"] is True
        assert result["hunted"] == 1
        # exp3 TTL check must NOT have been called — loop broke at exp2
        exp3._is_expired_monotonic.assert_not_called()
        # Only exp-1 was cleaned up
        mock_scheduler.cleanup_zombie_experiment.assert_called_once_with(
            "exp-1", "zombie_hunter"
        )

    def test_soft_time_limit_during_cleanup_releases_lock(self):
        """SoftTimeLimitExceeded during cleanup still releases distributed lock (finally clause)."""
        from celery.exceptions import SoftTimeLimitExceeded

        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        mock_scheduler = MagicMock()
        exp1 = MagicMock()
        exp1.experiment_id = "exp-1"
        exp1._is_expired_monotonic.return_value = True
        mock_scheduler.get_experiments_by_status.return_value = [exp1]
        mock_scheduler.cleanup_zombie_experiment.side_effect = SoftTimeLimitExceeded()

        mock_idempotency = MagicMock()
        mock_idempotency.acquire_lock.return_value = True

        with (
            patch(f"{_CHAOS_SVC}.get_chaos_scheduler", return_value=mock_scheduler),
            patch(f"{_IDEMPOTENCY}.IdempotencyService", return_value=mock_idempotency),
        ):
            result = hunt_zombie_experiments()

        # Lock released via finally despite SoftTimeLimitExceeded
        mock_idempotency.release_lock.assert_called_once()
        # Partial result returned (hunted=0 since cleanup was interrupted)
        assert result["success"] is True
        assert result["hunted"] == 0

    def test_skips_non_expired_experiments(self):
        """Non-expired experiments are not cleaned up."""
        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        mock_scheduler = MagicMock()
        mock_experiment = MagicMock()
        mock_experiment.experiment_id = "exp-healthy"
        mock_experiment._is_expired_monotonic.return_value = False
        mock_scheduler.get_experiments_by_status.return_value = [mock_experiment]

        with (
            patch(f"{_CHAOS_SVC}.get_chaos_scheduler", return_value=mock_scheduler),
            patch(f"{_IDEMPOTENCY}.IdempotencyService"),
        ):
            result = hunt_zombie_experiments()

        assert result["hunted"] == 0
        mock_scheduler.cleanup_zombie_experiment.assert_not_called()


# =============================================================================
# B. register_celery_tasks — hunt_zombie_experiments_task
# =============================================================================


class TestRegisterCeleryTasksContract:
    """Contract verification for register_celery_tasks() task registration (Section 4)."""

    def test_hunt_zombie_experiments_task_registered(self):
        """register_celery_tasks returns dict with hunt_zombie_experiments key."""
        from baldur.tasks.chaos_scheduler import register_celery_tasks

        mock_app = MagicMock()
        mock_app.task = MagicMock(side_effect=lambda **kwargs: lambda fn: fn)

        tasks = register_celery_tasks(mock_app)
        assert "hunt_zombie_experiments" in tasks
