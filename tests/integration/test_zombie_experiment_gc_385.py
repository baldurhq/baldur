"""
Zombie Experiment GC Integration Tests (385).

Tests the end-to-end zombie detection → cleanup → STOPPED event lifecycle
across ChaosSchedulerService components.

Test Categories:
    A. Full Zombie Cleanup Lifecycle:
        - register → expire → cleanup → STOPPED event → unregister
    B. Dual Detection Idempotency:
        - zombie_hunter and watchdog both detect same zombie → only one STOPPED
    C. Concurrent Cleanup Race:
        - Multiple threads calling cleanup_zombie_experiment → no double cleanup

Note: All tests use in-memory mock repositories - no DB dependency.
      This enables parallel test execution with pytest-xdist.
"""

from __future__ import annotations

import pytest

pytest.importorskip("baldur_pro")

pytestmark = pytest.mark.requires_pro


import threading
import time
from unittest.mock import MagicMock, patch

from baldur.services.event_bus.bus.event_types import EventType
from baldur_pro.services.chaos.scheduler.service import (
    ChaosSchedulerService,
    RunningExperimentInfo,
)


def _make_mock_schedule(schedule_id="sched-001"):
    """Create a mock ScheduledExperiment."""
    schedule = MagicMock()
    schedule.id = schedule_id
    schedule.experiment_type = "latency_injection"
    schedule.target_service = "payment"
    schedule.target_domain = ""
    schedule.description = "Test latency"
    schedule.enabled = True
    schedule.experiment_config = {}
    return schedule


def _make_mock_experiment(experiment_id, expired=True):
    """Create a mock ChaosExperiment instance."""
    exp = MagicMock()
    exp.experiment_id = experiment_id
    exp.status = "running"
    exp._is_expired_monotonic = MagicMock(return_value=expired)
    exp.is_expired = MagicMock(return_value=expired)
    return exp


# =============================================================================
# A. Full Zombie Cleanup Lifecycle
# =============================================================================


class TestZombieCleanupLifecycle:
    """Integration test: complete zombie lifecycle from registration to STOPPED event.

    Validates:
    - Experiment registration in both _running_experiments and _experiment_instances
    - Zombie detection via expired TTL
    - Full cleanup sequence: rollback → abort → unregister → STOPPED event
    - Post-cleanup state consistency
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.scheduler = ChaosSchedulerService()
        self.schedule = _make_mock_schedule()
        self.scheduler._schedules[self.schedule.id] = self.schedule

    def test_full_lifecycle_register_expire_cleanup_stopped(self):
        """
        Purpose:
            Simulate experiment registration → zombie detection → cleanup → STOPPED event.
        Expected:
            - rollback() called
            - _experiment_instances cleared
            - _running_experiments cleared
            - STOPPED event emitted with correct fields
        """
        # Given — experiment registered in both tracking dicts
        exp = _make_mock_experiment("exp-001")
        self.scheduler._experiment_instances["exp-001"] = exp
        self.scheduler._running_experiments[self.schedule.id] = RunningExperimentInfo(
            experiment_id="exp-001",
            started_at_monotonic=time.monotonic() - 600,
        )

        # When — zombie cleanup
        emitted_events = []

        def capture_emit(event_type, data):
            emitted_events.append((event_type, data))

        with patch.object(
            self.scheduler, "_emit_chaos_event", side_effect=capture_emit
        ):
            result = self.scheduler.cleanup_zombie_experiment(
                "exp-001", "zombie_hunter"
            )

        # Then — full cleanup
        assert result is True

        # Rollback called
        exp.rollback.assert_called_once()

        # Both tracking dicts cleaned
        assert "exp-001" not in self.scheduler._experiment_instances
        assert self.schedule.id not in self.scheduler._running_experiments

        # STOPPED event emitted with schedule-based fields
        assert len(emitted_events) == 1
        event_type, data = emitted_events[0]
        assert event_type == EventType.CHAOS_EXPERIMENT_STOPPED
        assert data["status"] == "aborted"
        assert data["experiment_type"] == "latency_injection"
        assert data["target_service"] == "payment"
        assert data["cleanup_source"] == "zombie_hunter"
        assert data["success"] is False

    def test_cleanup_after_normal_completion_is_noop(self):
        """
        Purpose:
            If _execute_experiment's finally block already cleaned up,
            cleanup_zombie_experiment should be idempotent.
        Expected:
            - Returns False (nothing to clean)
            - No STOPPED event emitted
        """
        # Given — both dicts already empty (normal finally cleanup)

        # When
        with patch.object(self.scheduler, "_emit_chaos_event") as mock_emit:
            result = self.scheduler.cleanup_zombie_experiment(
                "exp-already-done", "watchdog"
            )

        # Then
        assert result is False
        mock_emit.assert_not_called()


# =============================================================================
# B. Dual Detection Idempotency
# =============================================================================


class TestDualDetectionIdempotency:
    """Integration test: zombie_hunter and watchdog both detect same zombie (D-8).

    Validates:
    - Only one STOPPED event emitted despite two cleanup attempts
    - Second cleanup returns False
    """

    def setup_method(self):
        """Set up test fixtures."""
        self.scheduler = ChaosSchedulerService()
        self.schedule = _make_mock_schedule()
        self.scheduler._schedules[self.schedule.id] = self.schedule

    def test_second_cleanup_after_first_returns_false_no_duplicate_event(self):
        """
        Purpose:
            Simulate zombie_hunter cleaning first, then watchdog attempting cleanup.
        Expected:
            - First cleanup returns True, emits STOPPED
            - Second cleanup returns False, no STOPPED emitted
        """
        # Given
        exp = _make_mock_experiment("exp-dup")
        self.scheduler._experiment_instances["exp-dup"] = exp
        self.scheduler._running_experiments[self.schedule.id] = RunningExperimentInfo(
            experiment_id="exp-dup",
            started_at_monotonic=time.monotonic() - 600,
        )

        emitted_events = []

        def capture_emit(event_type, data):
            emitted_events.append((event_type, data))

        with patch.object(
            self.scheduler, "_emit_chaos_event", side_effect=capture_emit
        ):
            # When — zombie hunter cleans first
            result1 = self.scheduler.cleanup_zombie_experiment(
                "exp-dup", "zombie_hunter"
            )
            # When — watchdog tries to clean same experiment
            result2 = self.scheduler.cleanup_zombie_experiment("exp-dup", "watchdog")

        # Then
        assert result1 is True
        assert result2 is False
        assert len(emitted_events) == 1  # only one STOPPED
        assert emitted_events[0][1]["cleanup_source"] == "zombie_hunter"


# =============================================================================
# C. Concurrent Cleanup Race
# =============================================================================


class TestConcurrentCleanupRace:
    """Integration test: multi-threaded cleanup_zombie_experiment calls (D-9).

    Validates:
    - Thread-safe cleanup under concurrent access
    - Exactly one True result, rest are False
    - Exactly one STOPPED event emitted
    """

    def test_concurrent_cleanup_emits_single_stopped_event(self):
        """
        Purpose:
            Multiple threads call cleanup_zombie_experiment simultaneously.
        Expected:
            - Exactly one thread succeeds (returns True)
            - Exactly one STOPPED event emitted
        """
        scheduler = ChaosSchedulerService()
        schedule = _make_mock_schedule()
        scheduler._schedules[schedule.id] = schedule

        exp = _make_mock_experiment("exp-race")
        scheduler._experiment_instances["exp-race"] = exp
        scheduler._running_experiments[schedule.id] = RunningExperimentInfo(
            experiment_id="exp-race",
            started_at_monotonic=time.monotonic() - 600,
        )

        emitted_events = []
        emit_lock = threading.Lock()

        def capture_emit(event_type, data):
            with emit_lock:
                emitted_events.append((event_type, data))

        results = []
        result_lock = threading.Lock()

        def cleanup_worker(source):
            r = scheduler.cleanup_zombie_experiment("exp-race", source)
            with result_lock:
                results.append(r)

        with patch.object(scheduler, "_emit_chaos_event", side_effect=capture_emit):
            threads = [
                threading.Thread(target=cleanup_worker, args=(f"worker-{i}",))
                for i in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        # Exactly one True, rest False
        true_count = sum(1 for r in results if r is True)
        assert true_count == 1

        # Exactly one STOPPED event
        stopped_events = [
            e for e in emitted_events if e[0] == EventType.CHAOS_EXPERIMENT_STOPPED
        ]
        assert len(stopped_events) == 1
