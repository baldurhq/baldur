"""
Cross-Process Zombie Detection Tests (390).

Tests for:
A. Worker identity and heartbeat class variables — contract values
B. _beat_once() — StateBackend heartbeat write
C. _start_heartbeat() / _stop_heartbeat() — lifecycle, idempotency
D. _cleanup_stale_own_records() — startup self-cleanup (DD-1)
E. cleanup_cross_process_zombie() — cross-process zombie cleanup (DD-3)
F. _execute_experiment() dual-write — StateBackend record creation
G. _execute_experiment() dual-delete — StateBackend record deletion in finally
H. cleanup_zombie_experiment() dual-delete — StateBackend record deletion (step 3.5)
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import time
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.chaos.scheduler.service import (
    ChaosSchedulerService,
    RunningExperimentInfo,
)

_SCHEDULER_MODULE = "baldur_pro.services.chaos.scheduler.service"
_STATE_BACKEND = "baldur.core.state_backend.get_state_backend"


@pytest.fixture
def scheduler():
    """ChaosSchedulerService with cross_process_detection_enabled=False (default)."""
    return ChaosSchedulerService()


@pytest.fixture
def cross_process_scheduler(monkeypatch):
    """ChaosSchedulerService with cross_process_detection_enabled=True."""
    monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
    from baldur.settings.chaos import reset_chaos_settings

    reset_chaos_settings()
    try:
        with patch(_STATE_BACKEND) as mock_backend:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_backend.return_value = mock_be
            svc = ChaosSchedulerService()
        # Replace the backend for tests
        svc._mock_backend = mock_be
        yield svc
    finally:
        reset_chaos_settings()


def _inject_running(scheduler, schedule_id, experiment_id, mono_offset=0.0):
    """Helper: inject a RunningExperimentInfo into _running_experiments."""
    info = RunningExperimentInfo(
        experiment_id=experiment_id,
        started_at_monotonic=time.monotonic() - mono_offset,
    )
    scheduler._running_experiments[schedule_id] = info
    return info


# =============================================================================
# A. Worker Identity and Heartbeat Contract
# =============================================================================


class TestWorkerIdentityContract:
    """Worker identity and heartbeat class variable contract verification."""

    def test_heartbeat_key_prefix(self):
        """_HEARTBEAT_KEY_PREFIX follows the multi-region heartbeat key convention."""
        assert ChaosSchedulerService._HEARTBEAT_KEY_PREFIX == "worker:heartbeat:"

    def test_worker_id_format_hostname_colon_pid(self, scheduler):
        """_worker_id format is hostname:pid (DD-1)."""
        import os
        import socket

        expected = f"{socket.gethostname()}:{os.getpid()}"
        assert scheduler._worker_id == expected

    def test_heartbeat_not_running_by_default(self, scheduler):
        """Heartbeat not started when cross_process_detection_enabled=False."""
        assert scheduler._heartbeat_running is False
        assert scheduler._heartbeat_thread is None


# =============================================================================
# B. _beat_once() — StateBackend heartbeat write
# =============================================================================


class TestBeatOnceBehavior:
    """_beat_once() heartbeat write behavior verification."""

    def test_beat_once_writes_heartbeat_to_state_backend(self, scheduler):
        """_beat_once() calls backend.set() with correct key and TTL."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_get.return_value = mock_be

            scheduler._beat_once()

            mock_be.set.assert_called_once()
            args, kwargs = mock_be.set.call_args
            assert args[0] == f"worker:heartbeat:{scheduler._worker_id}"
            record = args[1]
            assert record["worker_id"] == scheduler._worker_id
            assert "ts" in record
            assert (
                kwargs["ttl_seconds"]
                == scheduler._chaos_settings.worker_heartbeat_ttl_seconds
            )

    def test_beat_once_swallows_backend_exception(self, scheduler):
        """_beat_once() logs warning but does not raise on backend failure."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_get.return_value.set.side_effect = ConnectionError("Redis down")

            # Should not raise
            scheduler._beat_once()


# =============================================================================
# C. _start_heartbeat() / _stop_heartbeat() — lifecycle, idempotency
# =============================================================================


class TestHeartbeatLifecycleBehavior:
    """Heartbeat start/stop lifecycle and idempotency verification."""

    def test_start_heartbeat_sets_running_flag(self, scheduler):
        """_start_heartbeat() sets _heartbeat_running=True and creates thread."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._start_heartbeat()

            assert scheduler._heartbeat_running is True
            assert scheduler._heartbeat_thread is not None
            assert scheduler._heartbeat_thread.daemon is True
            assert scheduler._heartbeat_thread.name == "ChaosWorkerHeartbeat"

            # Cleanup
            scheduler._stop_heartbeat()

    def test_start_heartbeat_idempotent_when_already_running(self, scheduler):
        """_start_heartbeat() is a no-op when already running."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._start_heartbeat()
            first_thread = scheduler._heartbeat_thread

            scheduler._start_heartbeat()  # Second call
            assert scheduler._heartbeat_thread is first_thread  # Same thread

            scheduler._stop_heartbeat()

    def test_stop_heartbeat_idempotent_when_not_running(self, scheduler):
        """_stop_heartbeat() is a no-op when heartbeat is not running."""
        assert scheduler._heartbeat_running is False
        scheduler._stop_heartbeat()  # Should not raise

    def test_stop_heartbeat_deletes_heartbeat_key(self, scheduler):
        """_stop_heartbeat() deletes heartbeat key from StateBackend."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._start_heartbeat()
            scheduler._stop_heartbeat()

            expected_key = f"worker:heartbeat:{scheduler._worker_id}"
            mock_be.delete.assert_any_call(expected_key)
            assert scheduler._heartbeat_running is False


# =============================================================================
# D. _cleanup_stale_own_records() — startup self-cleanup (DD-1)
# =============================================================================


class TestCleanupStaleOwnRecordsBehavior:
    """Startup self-cleanup behavior for PID reuse safety (DD-1)."""

    def test_cleans_own_worker_records(self, scheduler):
        """_cleanup_stale_own_records() cleans records matching own worker_id."""
        own_record = {
            "experiment_id": "chaos-aaa111bbb222",
            "schedule_id": "sched-001",
            "worker_id": scheduler._worker_id,
            "experiment_type": "latency",
            "target_service": "payment",
        }
        other_record = {
            "experiment_id": "chaos-ccc333ddd444",
            "schedule_id": "sched-002",
            "worker_id": "other-pod:99",
            "experiment_type": "error",
            "target_service": "order",
        }

        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {
                "chaos:running:chaos-aaa111bbb222": own_record,
                "chaos:running:chaos-ccc333ddd444": other_record,
            }
            mock_get.return_value = mock_be

            with patch.object(
                scheduler, "cleanup_cross_process_zombie"
            ) as mock_cleanup:
                scheduler._cleanup_stale_own_records()

                # Only own record cleaned
                mock_cleanup.assert_called_once_with(own_record, "startup_self_cleanup")

    def test_handles_empty_state_backend(self, scheduler):
        """_cleanup_stale_own_records() handles empty StateBackend gracefully."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._cleanup_stale_own_records()  # Should not raise

    def test_swallows_backend_exception(self, scheduler):
        """_cleanup_stale_own_records() logs warning on backend failure."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_get.side_effect = ConnectionError("Redis down")

            scheduler._cleanup_stale_own_records()  # Should not raise


# =============================================================================
# E. cleanup_cross_process_zombie() — cross-process cleanup (DD-3)
# =============================================================================


class TestCleanupCrossProcessZombieBehavior:
    """cleanup_cross_process_zombie() behavior verification."""

    def test_deletes_state_backend_record(self, scheduler):
        """cleanup_cross_process_zombie() deletes the experiment record."""
        record = {
            "experiment_id": "chaos-aaa111bbb222",
            "schedule_id": "sched-001",
            "worker_id": "dead-pod:1",
            "experiment_type": "latency",
            "target_service": "payment",
        }

        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_get.return_value = mock_be

            with patch.object(scheduler, "_emit_chaos_event"):
                result = scheduler.cleanup_cross_process_zombie(record, "zombie_hunter")

        assert result is True
        mock_be.delete.assert_called_once_with("chaos:running:chaos-aaa111bbb222")

    def test_emits_stopped_event_with_record_data(self, scheduler):
        """cleanup_cross_process_zombie() emits CHAOS_EXPERIMENT_STOPPED event."""
        record = {
            "experiment_id": "chaos-aaa111bbb222",
            "schedule_id": "sched-001",
            "worker_id": "dead-pod:1",
            "experiment_type": "latency",
            "target_service": "payment",
        }

        with patch(_STATE_BACKEND) as mock_get:
            mock_get.return_value = MagicMock()

            with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
                scheduler.cleanup_cross_process_zombie(record, "zombie_hunter")

                mock_emit.assert_called_once()
                event_type, data = mock_emit.call_args[0]
                assert event_type == EventType.CHAOS_EXPERIMENT_STOPPED
                assert data["experiment_id"] == "chaos-aaa111bbb222"
                assert data["dead_worker_id"] == "dead-pod:1"
                assert data["cleanup_source"] == "zombie_hunter"
                assert data["status"] == "aborted"
                assert data["success"] is False

    def test_returns_true_even_on_backend_delete_failure(self, scheduler):
        """cleanup_cross_process_zombie() returns True even if delete fails."""
        record = {
            "experiment_id": "chaos-aaa111bbb222",
            "schedule_id": "sched-001",
            "worker_id": "dead-pod:1",
        }

        with patch(_STATE_BACKEND) as mock_get:
            mock_get.return_value.delete.side_effect = ConnectionError("Redis")

            with patch.object(scheduler, "_emit_chaos_event"):
                result = scheduler.cleanup_cross_process_zombie(record, "test")

        assert result is True


# =============================================================================
# F. _execute_experiment() dual-write
# =============================================================================


def _run_execute_experiment(sched, mock_be, experiment_id="chaos-test123456"):
    """Helper: run _execute_experiment() with standard mocks, return mock_be."""
    mock_schedule = MagicMock()
    mock_schedule.experiment_type = "latency_injection"
    mock_schedule.target_service = "payment"
    mock_schedule.target_domain = ""
    mock_schedule.experiment_config = {}

    with (
        patch("baldur_pro.services.chaos.experiments.create_experiment") as mock_create,
        patch("baldur_pro.services.chaos.safety_guard.get_safety_guard"),
        patch("baldur_pro.services.chaos.blast_radius.get_blast_radius_manager"),
        patch(_STATE_BACKEND, return_value=mock_be),
    ):
        mock_exp = MagicMock()
        mock_result = MagicMock()
        mock_result.status = "completed"
        mock_result.dry_run = False
        mock_exp.execute.return_value = mock_result
        mock_create.return_value = mock_exp

        from baldur.utils.time import utc_now

        sched._execute_experiment(
            schedule=mock_schedule,
            schedule_id="sched-001",
            experiment_id=experiment_id,
            started_at=utc_now(),
            force=True,
        )


class TestExecuteExperimentDualWriteBehavior:
    """Dual-write to StateBackend in _execute_experiment() behavior verification."""

    def test_dual_write_skipped_when_disabled(self, scheduler):
        """StateBackend not called when cross_process_detection_enabled=False."""
        # Given
        mock_be = MagicMock()

        # When
        _run_execute_experiment(scheduler, mock_be)

        # Then — StateBackend.set() should not be called for chaos:running:
        set_calls = [
            c for c in mock_be.set.call_args_list if "chaos:running:" in str(c)
        ]
        assert len(set_calls) == 0

    def test_dual_write_and_delete_when_enabled(self, cross_process_scheduler):
        """StateBackend.set() on start and delete() on completion when enabled."""
        sched = cross_process_scheduler
        mock_be = sched._mock_backend
        mock_be.reset_mock()

        # When
        _run_execute_experiment(sched, mock_be)

        # Then — dual-write: set() called with chaos:running: key
        set_calls = [
            c for c in mock_be.set.call_args_list if "chaos:running:" in str(c)
        ]
        assert len(set_calls) == 1
        write_call = set_calls[0]
        assert write_call[0][0] == "chaos:running:chaos-test123456"
        record = write_call[0][1]
        assert record["experiment_id"] == "chaos-test123456"
        assert record["schedule_id"] == "sched-001"
        assert record["worker_id"] == sched._worker_id

        # Then — dual-delete: delete() called in finally block
        mock_be.delete.assert_any_call("chaos:running:chaos-test123456")


# =============================================================================
# G. _execute_experiment() dual-delete in finally block
# =============================================================================


class TestExecuteExperimentFinallyDualDeleteBehavior:
    """Dual-delete in _execute_experiment() finally block (service.py:1018-1025)."""

    def test_finally_delete_runs_on_normal_completion(self, cross_process_scheduler):
        """Finally block deletes StateBackend record on successful execution."""
        sched = cross_process_scheduler
        mock_be = sched._mock_backend
        mock_be.reset_mock()

        _run_execute_experiment(sched, mock_be)

        delete_calls = [
            c for c in mock_be.delete.call_args_list if "chaos:running:" in str(c)
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][0] == "chaos:running:chaos-test123456"

    def test_finally_delete_runs_on_experiment_exception(self, cross_process_scheduler):
        """Finally block deletes StateBackend record even when experiment raises."""
        sched = cross_process_scheduler
        mock_be = sched._mock_backend
        mock_be.reset_mock()

        mock_schedule = MagicMock()
        mock_schedule.experiment_type = "latency_injection"
        mock_schedule.target_service = "payment"
        mock_schedule.target_domain = ""
        mock_schedule.experiment_config = {}

        with (
            patch(
                "baldur_pro.services.chaos.experiments.create_experiment"
            ) as mock_create,
            patch("baldur_pro.services.chaos.safety_guard.get_safety_guard"),
            patch("baldur_pro.services.chaos.blast_radius.get_blast_radius_manager"),
            patch(_STATE_BACKEND, return_value=mock_be),
        ):
            mock_exp = MagicMock()
            mock_exp.execute.side_effect = RuntimeError("Experiment failed")
            mock_create.return_value = mock_exp

            from baldur.utils.time import utc_now

            with pytest.raises(RuntimeError, match="Experiment failed"):
                sched._execute_experiment(
                    schedule=mock_schedule,
                    schedule_id="sched-001",
                    experiment_id="chaos-fail999888",
                    started_at=utc_now(),
                    force=True,
                )

        # Finally block must still clean up StateBackend record
        mock_be.delete.assert_any_call("chaos:running:chaos-fail999888")

    def test_finally_delete_skipped_when_disabled(self, scheduler):
        """Finally block does not call StateBackend when detection disabled."""
        mock_be = MagicMock()

        _run_execute_experiment(scheduler, mock_be)

        # No delete calls for chaos:running: keys
        delete_calls = [
            c for c in mock_be.delete.call_args_list if "chaos:running:" in str(c)
        ]
        assert len(delete_calls) == 0


# =============================================================================
# H. cleanup_zombie_experiment() dual-delete (step 3.5)
# =============================================================================


class TestCleanupZombieDualDeleteBehavior:
    """Dual-delete in cleanup_zombie_experiment() step 3.5 verification."""

    def test_dual_delete_called_when_enabled(self, cross_process_scheduler):
        """cleanup_zombie_experiment() deletes from StateBackend when enabled."""
        sched = cross_process_scheduler
        experiment_id = "chaos-test123456"

        # Inject a running experiment
        _inject_running(sched, "sched-001", experiment_id)

        mock_be = MagicMock()
        with patch(_STATE_BACKEND, return_value=mock_be):
            with patch.object(sched, "_emit_chaos_event"):
                with patch.object(sched, "get_schedule", return_value=None):
                    sched.cleanup_zombie_experiment(experiment_id, "zombie_hunter")

        mock_be.delete.assert_any_call(f"chaos:running:{experiment_id}")

    def test_dual_delete_skipped_when_disabled(self, scheduler):
        """cleanup_zombie_experiment() skips StateBackend when disabled."""
        experiment_id = "chaos-test123456"
        _inject_running(scheduler, "sched-001", experiment_id)

        with patch(_STATE_BACKEND) as mock_get:
            with patch.object(scheduler, "_emit_chaos_event"):
                with patch.object(scheduler, "get_schedule", return_value=None):
                    scheduler.cleanup_zombie_experiment(experiment_id, "test")

            # StateBackend should NOT be called
            mock_get.assert_not_called()


# =============================================================================
# I. _execute_experiment() finally block — backend failure swallowed
# =============================================================================


class TestExecuteExperimentFinallyBackendFailureBehavior:
    """Finally block swallows get_state_backend() failure to prevent exception masking."""

    def test_finally_swallows_backend_failure_on_normal_completion(
        self, cross_process_scheduler
    ):
        """get_state_backend() failure in finally does not raise on normal execution."""
        sched = cross_process_scheduler
        mock_schedule = MagicMock()
        mock_schedule.experiment_type = "latency_injection"
        mock_schedule.target_service = "payment"
        mock_schedule.target_domain = ""
        mock_schedule.experiment_config = {}

        with (
            patch(
                "baldur_pro.services.chaos.experiments.create_experiment"
            ) as mock_create,
            patch("baldur_pro.services.chaos.safety_guard.get_safety_guard"),
            patch("baldur_pro.services.chaos.blast_radius.get_blast_radius_manager"),
            patch(
                _STATE_BACKEND,
                side_effect=[
                    MagicMock(),  # dual-write in try block
                    ConnectionError("Redis down"),  # dual-delete in finally
                ],
            ),
        ):
            mock_exp = MagicMock()
            mock_result = MagicMock()
            mock_result.status = "completed"
            mock_result.dry_run = False
            mock_exp.execute.return_value = mock_result
            mock_create.return_value = mock_exp

            from baldur.utils.time import utc_now

            # Should NOT raise — finally block swallows backend failure
            sched._execute_experiment(
                schedule=mock_schedule,
                schedule_id="sched-001",
                experiment_id="chaos-be-fail111",
                started_at=utc_now(),
                force=True,
            )

    def test_finally_preserves_original_exception_on_backend_failure(
        self, cross_process_scheduler
    ):
        """Original experiment exception is NOT masked by backend failure in finally."""
        sched = cross_process_scheduler
        mock_schedule = MagicMock()
        mock_schedule.experiment_type = "latency_injection"
        mock_schedule.target_service = "payment"
        mock_schedule.target_domain = ""
        mock_schedule.experiment_config = {}

        with (
            patch(
                "baldur_pro.services.chaos.experiments.create_experiment"
            ) as mock_create,
            patch("baldur_pro.services.chaos.safety_guard.get_safety_guard"),
            patch("baldur_pro.services.chaos.blast_radius.get_blast_radius_manager"),
            patch(
                _STATE_BACKEND,
                side_effect=[
                    MagicMock(),  # dual-write in try block
                    ConnectionError("Redis down"),  # dual-delete in finally
                ],
            ),
        ):
            mock_exp = MagicMock()
            mock_exp.execute.side_effect = RuntimeError("Experiment crashed")
            mock_create.return_value = mock_exp

            from baldur.utils.time import utc_now

            # Original RuntimeError must propagate, NOT ConnectionError from finally
            with pytest.raises(RuntimeError, match="Experiment crashed"):
                sched._execute_experiment(
                    schedule=mock_schedule,
                    schedule_id="sched-001",
                    experiment_id="chaos-be-fail222",
                    started_at=utc_now(),
                    force=True,
                )


# =============================================================================
# J. cleanup_zombie_experiment() step 3.5 — backend failure swallowed
# =============================================================================


class TestCleanupZombieDualDeleteBackendFailureBehavior:
    """Step 3.5 swallows backend failure so step 4 STOPPED event still emits."""

    def test_step4_event_emitted_despite_backend_failure(self, cross_process_scheduler):
        """STOPPED event fires even when step 3.5 get_state_backend() raises."""
        sched = cross_process_scheduler
        experiment_id = "chaos-be-fail333"

        # Given — inject running experiment
        _inject_running(sched, "sched-001", experiment_id)

        # When — backend fails in step 3.5
        with patch(_STATE_BACKEND, side_effect=ConnectionError("Redis down")):
            with patch.object(sched, "_emit_chaos_event") as mock_emit:
                with patch.object(sched, "get_schedule", return_value=None):
                    result = sched.cleanup_zombie_experiment(
                        experiment_id, "zombie_hunter"
                    )

        # Then — step 4 STOPPED event still emitted
        assert result is True
        mock_emit.assert_called_once()
        event_type, data = mock_emit.call_args[0]
        from baldur.services.event_bus.bus.event_types import EventType

        assert event_type == EventType.CHAOS_EXPERIMENT_STOPPED
        assert data["experiment_id"] == experiment_id


# =============================================================================
# K. _heartbeat_loop() — sleep-first, no double-beat
# =============================================================================


class TestHeartbeatLoopSleepFirstBehavior:
    """_heartbeat_loop() uses sleep-first pattern to avoid double-beat on startup."""

    def test_no_double_beat_on_startup(self, scheduler):
        """After _start_heartbeat(), _beat_once is called exactly once (sync first beat).

        The loop sleeps first, so no immediate second beat occurs before the
        first interval elapses.
        """
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._start_heartbeat()

            # Allow thread to enter loop and hit sleep (but not complete it)
            import time

            time.sleep(0.05)

            # Count _beat_once calls via backend.set() for heartbeat key
            heartbeat_key = f"worker:heartbeat:{scheduler._worker_id}"
            set_calls = [
                c for c in mock_be.set.call_args_list if c[0][0] == heartbeat_key
            ]
            # Exactly 1 call: sync first beat only (loop sleeping, not yet beaten)
            assert len(set_calls) == 1

            scheduler._stop_heartbeat()

    def test_loop_checks_running_flag_after_sleep(self, scheduler):
        """_heartbeat_loop() checks _heartbeat_running after sleep before beating."""
        with patch(_STATE_BACKEND) as mock_get:
            mock_be = MagicMock()
            mock_be.get_all.return_value = {}
            mock_get.return_value = mock_be

            scheduler._start_heartbeat()
            heartbeat_key = f"worker:heartbeat:{scheduler._worker_id}"

            # Record beat count before stop
            pre_stop_count = len(
                [c for c in mock_be.set.call_args_list if c[0][0] == heartbeat_key]
            )

            # Stop heartbeat — flag goes False
            scheduler._stop_heartbeat()

            # After stop, no additional beats should occur
            import time

            time.sleep(0.05)
            post_stop_count = len(
                [c for c in mock_be.set.call_args_list if c[0][0] == heartbeat_key]
            )
            assert post_stop_count == pre_stop_count
