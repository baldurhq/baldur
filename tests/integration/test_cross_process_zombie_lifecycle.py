"""
Cross-Process Zombie Detection Lifecycle Integration Tests (390).

Tests the interaction between ChaosSchedulerService, StateBackend (Memory),
and IdempotencyService for cross-process zombie detection.

Test Categories:
    A. Heartbeat Lifecycle:
        - Start → sync beat → daemon thread → stop → key deleted
    B. Cross-Process Zombie Detection E2E:
        - Dead worker record → heartbeat absent → Phase 2 cleanup
    C. Startup Self-Cleanup + Zombie Detection Coordination:
        - PID reuse scenario: stale records cleaned before heartbeat starts

Note: All tests use MemoryStateBackend — no infra dependency.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


from unittest.mock import MagicMock, patch

import pytest

from baldur.core.state_backend import MemoryStateBackend
from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.chaos.scheduler.service import ChaosSchedulerService

_STATE_BACKEND = "baldur.core.state_backend.get_state_backend"
_TASK_MODULE = "baldur.tasks.chaos_scheduler"


@pytest.fixture(autouse=True)
def _reset_settings():
    """Reset chaos settings before/after each test."""
    from baldur.settings.chaos import reset_chaos_settings

    reset_chaos_settings()
    yield
    reset_chaos_settings()


# =============================================================================
# A. Heartbeat Lifecycle
# =============================================================================


class TestHeartbeatLifecycle:
    """Heartbeat start/stop lifecycle with MemoryStateBackend.

    Validates:
    - Sync first beat writes heartbeat key before thread starts
    - _stop_heartbeat() deletes heartbeat key
    - Full cycle: start → beat → stop → key absent
    """

    def setup_method(self):
        self.backend = MemoryStateBackend()

    def test_heartbeat_start_writes_key_then_stop_deletes(self, monkeypatch):
        """
        Purpose:
            Heartbeat lifecycle: start writes key, stop deletes it.
        Expected:
            - After _start_heartbeat(): heartbeat key exists in backend
            - After _stop_heartbeat(): heartbeat key is gone
        """
        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()

        with patch(_STATE_BACKEND, lambda: self.backend):
            scheduler = ChaosSchedulerService()

            # After init (cross_process=True), heartbeat should be running
            assert scheduler._heartbeat_running is True

            # Heartbeat key should exist
            heartbeat_key = f"worker:heartbeat:{scheduler._worker_id}"
            result = self.backend.get(heartbeat_key)
            assert result is not None
            assert result["worker_id"] == scheduler._worker_id

            # Stop heartbeat
            scheduler._stop_heartbeat()
            assert scheduler._heartbeat_running is False

            # Key should be deleted
            result = self.backend.get(heartbeat_key)
            assert result is None


# =============================================================================
# B. Cross-Process Zombie Detection E2E
# =============================================================================


class TestCrossProcessZombieDetectionE2E:
    """End-to-end: dead worker → Phase 2 detects → cleanup.

    Validates:
    - _hunt_cross_process_zombies() detects records from dead workers
    - cleanup_cross_process_zombie() deletes the record
    - Alive workers are skipped
    """

    def setup_method(self):
        self.backend = MemoryStateBackend()

    def test_dead_worker_zombie_detected_and_cleaned(self, monkeypatch):
        """
        Purpose:
            Phase 2 detects a zombie experiment from a dead worker
            (no heartbeat) and cleans it up.
        Expected:
            - Dead worker's experiment record is deleted from StateBackend
            - STOPPED event is emitted
            - Alive worker's experiment is untouched
        """
        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()

        with patch(_STATE_BACKEND, lambda: self.backend):
            scheduler = ChaosSchedulerService()

        # Simulate dead worker's stale experiment record
        dead_record = {
            "experiment_id": "chaos-dead111222",
            "schedule_id": "sched-dead",
            "worker_id": "dead-pod:99",
            "experiment_type": "latency",
            "target_service": "payment",
        }
        self.backend.set("chaos:running:chaos-dead111222", dead_record, ttl_seconds=600)

        # Simulate alive worker's experiment record + heartbeat
        alive_record = {
            "experiment_id": "chaos-alive333444",
            "schedule_id": "sched-alive",
            "worker_id": "alive-pod:88",
            "experiment_type": "error",
            "target_service": "order",
        }
        self.backend.set(
            "chaos:running:chaos-alive333444", alive_record, ttl_seconds=600
        )
        self.backend.set(
            "worker:heartbeat:alive-pod:88",
            {"worker_id": "alive-pod:88", "ts": 12345},
            ttl_seconds=120,
        )

        # Run Phase 2
        from baldur.tasks.chaos_scheduler import _hunt_cross_process_zombies

        mock_idempotency = MagicMock()
        mock_idempotency.acquire_lock.return_value = True

        from baldur.settings.chaos import get_chaos_settings

        settings = get_chaos_settings()

        with patch(_STATE_BACKEND, lambda: self.backend):
            with patch.object(scheduler, "_emit_chaos_event") as mock_emit:
                result = _hunt_cross_process_zombies(
                    scheduler, mock_idempotency, settings
                )

        # Dead worker zombie cleaned
        assert result["hunted"] == 1

        # Dead record deleted from backend
        assert self.backend.get("chaos:running:chaos-dead111222") is None

        # Alive record still exists
        assert self.backend.get("chaos:running:chaos-alive333444") is not None

        # STOPPED event emitted for dead worker
        mock_emit.assert_called_once()
        event_type, data = mock_emit.call_args[0]
        assert event_type == EventType.CHAOS_EXPERIMENT_STOPPED
        assert data["dead_worker_id"] == "dead-pod:99"

        # Cleanup
        scheduler._stop_heartbeat()


# =============================================================================
# C. Startup Self-Cleanup + Zombie Detection Coordination
# =============================================================================


class TestStartupSelfCleanupCoordination:
    """PID reuse scenario: startup self-cleanup before heartbeat.

    Validates:
    - On startup with same worker_id, stale records are cleaned
    - New heartbeat is established after cleanup
    """

    def setup_method(self):
        self.backend = MemoryStateBackend()

    def test_stale_records_cleaned_on_startup(self, monkeypatch):
        """
        Purpose:
            When a process restarts with the same worker_id (PID reuse),
            stale experiment records from the dead incarnation are cleaned.
        Expected:
            - Stale records with own worker_id are deleted on startup
            - Fresh heartbeat is written
        """
        monkeypatch.setenv("BALDUR_CHAOS_CROSS_PROCESS_DETECTION_ENABLED", "true")
        from baldur.settings.chaos import reset_chaos_settings

        reset_chaos_settings()

        import os
        import socket

        expected_worker_id = f"{socket.gethostname()}:{os.getpid()}"

        # Pre-populate stale records from "previous incarnation"
        stale_record = {
            "experiment_id": "chaos-stale999888",
            "schedule_id": "sched-stale",
            "worker_id": expected_worker_id,
            "experiment_type": "error",
            "target_service": "auth",
        }
        self.backend.set(
            "chaos:running:chaos-stale999888", stale_record, ttl_seconds=600
        )

        with patch(_STATE_BACKEND, lambda: self.backend):
            with patch.object(ChaosSchedulerService, "_emit_chaos_event"):
                scheduler = ChaosSchedulerService()

        # Stale record should be cleaned
        assert self.backend.get("chaos:running:chaos-stale999888") is None

        # Fresh heartbeat should exist
        heartbeat_key = f"worker:heartbeat:{expected_worker_id}"
        assert self.backend.get(heartbeat_key) is not None

        # Cleanup
        scheduler._stop_heartbeat()
