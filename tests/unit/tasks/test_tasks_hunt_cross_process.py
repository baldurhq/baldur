"""
Cross-Process Zombie Hunter Tests (390 DD-3).

Tests for:
A. _hunt_cross_process_zombies() — Phase 2 standalone behavior
B. hunt_zombie_experiments() Phase 2 integration — feature flag gating
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_TASK_MODULE = "baldur.tasks.chaos_scheduler"


def _make_mock_idempotency():
    """Create a mock IdempotencyService with lock support."""
    mock = MagicMock()
    mock.acquire_lock.return_value = True
    mock.release_lock.return_value = True
    return mock


def _make_mock_scheduler(worker_id="my-pod:1"):
    """Create a mock ChaosSchedulerService."""
    mock = MagicMock()
    mock._worker_id = worker_id
    mock.cleanup_cross_process_zombie.return_value = True
    return mock


def _make_mock_settings(enabled=True, lock_ttl=120):
    """Create a mock ChaosSettings."""
    mock = MagicMock()
    mock.cross_process_detection_enabled = enabled
    mock.experiment_lock_ttl = lock_ttl
    return mock


# =============================================================================
# A. _hunt_cross_process_zombies() — Phase 2 standalone
# =============================================================================


class TestHuntCrossProcessZombiesBehavior:
    """_hunt_cross_process_zombies() Phase 2 behavior verification."""

    def test_skips_own_worker_records(self):
        """Records from own worker_id are skipped (Phase 1 handles them)."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        own_record = {
            "experiment_id": "chaos-own123",
            "worker_id": "my-pod:1",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-own123": own_record,
            }
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result["hunted"] == 0
        scheduler.cleanup_cross_process_zombie.assert_not_called()

    def test_skips_alive_worker_records(self):
        """Records from workers with active heartbeat are skipped."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        other_record = {
            "experiment_id": "chaos-other123",
            "worker_id": "alive-pod:2",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-other123": other_record,
            }
            # Worker is alive (heartbeat exists)
            mock_be.exists.return_value = True
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result["hunted"] == 0
        mock_be.exists.assert_called_once_with("worker:heartbeat:alive-pod:2")

    def test_cleans_dead_worker_zombie(self):
        """Records from dead workers (no heartbeat) are cleaned."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        dead_record = {
            "experiment_id": "chaos-dead123",
            "worker_id": "dead-pod:3",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-dead123": dead_record,
            }
            mock_be.exists.return_value = False  # No heartbeat
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result["hunted"] == 1
        scheduler.cleanup_cross_process_zombie.assert_called_once_with(
            dead_record, "zombie_hunter"
        )
        idempotency.acquire_lock.assert_called_once()
        idempotency.release_lock.assert_called_once()

    def test_skips_when_lock_not_acquired(self):
        """Records where distributed lock fails are skipped."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        idempotency = _make_mock_idempotency()
        idempotency.acquire_lock.return_value = False  # Lock contention
        settings = _make_mock_settings()

        dead_record = {
            "experiment_id": "chaos-contested123",
            "worker_id": "dead-pod:4",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-contested123": dead_record,
            }
            mock_be.exists.return_value = False
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result["skipped"] == 1
        assert result["hunted"] == 0
        scheduler.cleanup_cross_process_zombie.assert_not_called()

    def test_graceful_degradation_on_backend_failure(self):
        """Returns empty result on StateBackend query failure."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler()
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_get.side_effect = ConnectionError("Redis down")

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result == {"hunted": 0, "skipped": 0, "errors": []}

    def test_cleanup_error_recorded_in_errors_list(self):
        """Per-experiment errors are captured in errors list, not raised."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        scheduler.cleanup_cross_process_zombie.side_effect = RuntimeError("Boom")
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        dead_record = {
            "experiment_id": "chaos-error123",
            "worker_id": "dead-pod:5",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-error123": dead_record,
            }
            mock_be.exists.return_value = False
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert len(result["errors"]) == 1
        assert result["errors"][0]["experiment_id"] == "chaos-error123"

    def test_propagates_soft_time_limit_exceeded(self):
        """SoftTimeLimitExceeded is re-raised for outer handler."""
        from celery.exceptions import SoftTimeLimitExceeded

        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        scheduler.cleanup_cross_process_zombie.side_effect = SoftTimeLimitExceeded()
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        dead_record = {
            "experiment_id": "chaos-timeout123",
            "worker_id": "dead-pod:6",
        }

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-timeout123": dead_record,
            }
            mock_be.exists.return_value = False
            mock_get.return_value = mock_be

            with pytest.raises(SoftTimeLimitExceeded):
                _hunt_cross_process_zombies(scheduler, idempotency, settings)

    def test_records_without_worker_id_are_skipped(self):
        """Records missing worker_id field are skipped."""
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        scheduler = _make_mock_scheduler(worker_id="my-pod:1")
        idempotency = _make_mock_idempotency()
        settings = _make_mock_settings()

        no_worker_record = {"experiment_id": "chaos-orphan123"}

        with patch("baldur.core.state_backend.get_state_backend") as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-orphan123": no_worker_record,
            }
            mock_get.return_value = mock_be

            result = _hunt_cross_process_zombies(scheduler, idempotency, settings)

        assert result["hunted"] == 0
        scheduler.cleanup_cross_process_zombie.assert_not_called()


# =============================================================================
# B. hunt_zombie_experiments() Phase 2 integration
# =============================================================================


class TestHuntZombieExperimentsPhase2Behavior:
    """Phase 2 integration in hunt_zombie_experiments() feature flag gating."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def test_phase2_skipped_when_disabled(self):
        """Phase 2 is not called when cross_process_detection_enabled=False."""
        from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

        with (
            patch("baldur_pro.services.chaos.get_chaos_scheduler") as mock_get_sched,
            patch("baldur.services.idempotency.IdempotencyService"),
            patch(f"{_TASK_MODULE}._hunt_cross_process_zombies") as mock_phase2,
        ):
            mock_scheduler = MagicMock()
            mock_scheduler.get_experiments_by_status.return_value = []
            mock_get_sched.return_value = mock_scheduler

            result = hunt_zombie_experiments()

            mock_phase2.assert_not_called()
            assert result["success"] is True

    def test_phase2_called_when_enabled(self, monkeypatch):
        """Phase 2 is called when cross_process_detection_enabled=True."""
        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()

        # Need to reload the module-level _chaos_settings
        import baldur.tasks.chaos_scheduler as task_mod

        original_settings = task_mod._chaos_settings
        try:
            from baldur.settings.chaos import get_chaos_settings

            task_mod._chaos_settings = get_chaos_settings()

            with (
                patch(
                    "baldur_pro.services.chaos.get_chaos_scheduler"
                ) as mock_get_sched,
                patch("baldur.services.idempotency.IdempotencyService"),
                patch(
                    f"{_TASK_MODULE}._hunt_cross_process_zombies",
                    return_value={"hunted": 2, "skipped": 0, "errors": []},
                ) as mock_phase2,
            ):
                mock_scheduler = MagicMock()
                mock_scheduler.get_experiments_by_status.return_value = []
                mock_get_sched.return_value = mock_scheduler

                from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

                result = hunt_zombie_experiments()

                mock_phase2.assert_called_once()
                assert result["hunted"] == 2
                assert result["success"] is True
        finally:
            task_mod._chaos_settings = original_settings
            reset_chaos_settings()

    def test_phase2_results_merged_with_phase1(self, monkeypatch):
        """Phase 2 results are merged into Phase 1 result counters."""
        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()

        import baldur.tasks.chaos_scheduler as task_mod

        original_settings = task_mod._chaos_settings
        try:
            from baldur.settings.chaos import get_chaos_settings

            task_mod._chaos_settings = get_chaos_settings()

            with (
                patch(
                    "baldur_pro.services.chaos.get_chaos_scheduler"
                ) as mock_get_sched,
                patch("baldur.services.idempotency.IdempotencyService"),
                patch(
                    f"{_TASK_MODULE}._hunt_cross_process_zombies",
                    return_value={
                        "hunted": 3,
                        "skipped": 1,
                        "errors": [{"experiment_id": "e1", "error": "err"}],
                    },
                ),
            ):
                mock_scheduler = MagicMock()
                mock_scheduler.get_experiments_by_status.return_value = []
                mock_get_sched.return_value = mock_scheduler

                from baldur.tasks.chaos_scheduler import hunt_zombie_experiments

                result = hunt_zombie_experiments()

                # Phase 1 hunted 0 + Phase 2 hunted 3
                assert result["hunted"] == 3
                assert result["skipped"] == 1
                assert len(result["errors"]) == 1
        finally:
            task_mod._chaos_settings = original_settings
            reset_chaos_settings()
