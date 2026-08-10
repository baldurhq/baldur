"""The audit lifecycle's PID guard, startup honesty, and metric honesty.

``startup_async_audit_system()`` is the pipeline's single re-entry point, and
before this change it early-returned on state a ``fork()`` had copied: a
preload worker read "already started" over a pipeline none of whose threads
survived, so every non-CRITICAL audit event it emitted was enqueued to a
consumer that did not exist.

Three surfaces:

- the PID-aware "already started" guard;
- completion gated on the logger actually having started, so a swallowed init
  failure stays retryable instead of being recorded as a successful startup;
- ``get_async_audit_metrics()``, which composes thread aliveness rather than
  publishing an inherited control flag.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- State transition: inherited flags discarded, own-PID flags honoured
- Error injection: a logger init that fails leaves the flags unset
- Negative side effect: a failed startup registers no completion
- Delegation: the published queue depth comes from the repaired stats read
"""

from __future__ import annotations

import os
import threading
from unittest.mock import patch

import pytest

from baldur.audit import async_audit_lifecycle
from baldur.audit.async_audit_lifecycle import (
    _lifecycle_state,
    get_async_audit_metrics,
    startup_async_audit_system,
)
from baldur.utils.async_logger import AsyncHealingLogger

FOREIGN_PID = os.getpid() + 1


@pytest.fixture
def lifecycle_state():
    """Runtime-scoped lifecycle flags, reset either side of the test."""
    state = _lifecycle_state()
    state.startup_completed = False
    state.origin_pid = None
    state.audit_shutdown_done = False
    yield state
    state.startup_completed = False
    state.origin_pid = None
    state.audit_shutdown_done = False


@pytest.fixture
def stubbed_startup_steps():
    """Neutralise the four startup steps so only the guard is under test."""
    with (
        patch.object(async_audit_lifecycle, "_load_checkpoint", return_value=0),
        patch.object(
            async_audit_lifecycle, "_check_unprocessed_wal_entries", return_value=0
        ),
        patch.object(
            async_audit_lifecycle, "_initialize_async_logger", return_value=True
        ) as init_logger,
        patch.object(async_audit_lifecycle, "_start_sync_worker") as start_worker,
    ):
        yield init_logger, start_worker


def _dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    return thread


# =============================================================================
# The PID-aware "already started" guard
# =============================================================================


class TestAuditLifecyclePidGuardBehavior:
    """Inherited flags describe the parent's pipeline, not this process's."""

    def test_a_cold_process_starts_the_pipeline(
        self, lifecycle_state, stubbed_startup_steps
    ):
        init_logger, start_worker = stubbed_startup_steps

        assert startup_async_audit_system() is True

        init_logger.assert_called_once_with()
        start_worker.assert_called_once_with()
        assert lifecycle_state.startup_completed is True
        assert lifecycle_state.origin_pid == os.getpid()

    def test_a_second_call_in_the_same_process_early_returns(
        self, lifecycle_state, stubbed_startup_steps
    ):
        """The flags belong to this process, so the guard is honoured: two
        consumers on one queue is exactly what it prevents.
        """
        init_logger, _ = stubbed_startup_steps
        lifecycle_state.startup_completed = True
        lifecycle_state.origin_pid = os.getpid()

        assert startup_async_audit_system() is False

        init_logger.assert_not_called()

    def test_inherited_completion_from_another_pid_re_runs_startup(
        self, lifecycle_state, stubbed_startup_steps
    ):
        """The fork-child shape. Without the PID discriminator this returns
        False and the worker serves requests with no consumer at all.
        """
        init_logger, start_worker = stubbed_startup_steps
        lifecycle_state.startup_completed = True
        lifecycle_state.origin_pid = FOREIGN_PID

        assert startup_async_audit_system() is True

        init_logger.assert_called_once_with()
        start_worker.assert_called_once_with()
        assert lifecycle_state.origin_pid == os.getpid()

    def test_inherited_shutdown_latch_is_discarded_with_the_completion(
        self, lifecycle_state, stubbed_startup_steps
    ):
        """A parent that already ran its shutdown hands the child a latch that
        would make the child's own shutdown a no-op — its buffered events would
        never be flushed.
        """
        lifecycle_state.startup_completed = True
        lifecycle_state.origin_pid = FOREIGN_PID
        lifecycle_state.audit_shutdown_done = True

        startup_async_audit_system()

        assert lifecycle_state.audit_shutdown_done is False

    def test_a_never_started_process_with_a_stale_pid_still_starts(
        self, lifecycle_state, stubbed_startup_steps
    ):
        """Boundary: the guard keys on the completion flag first, so an origin
        PID without a completion is not a reason to skip.
        """
        init_logger, _ = stubbed_startup_steps
        lifecycle_state.startup_completed = False
        lifecycle_state.origin_pid = FOREIGN_PID

        assert startup_async_audit_system() is True

        init_logger.assert_called_once_with()


# =============================================================================
# Startup honesty
# =============================================================================


class TestAuditLifecycleStartupHonestyBehavior:
    """A startup that did not complete must stay retryable."""

    def test_a_failed_logger_init_leaves_the_flags_unset(self, lifecycle_state):
        """Error injection: ``_initialize_async_logger`` swallows its own
        exception and reports False. Recording that as a completed startup
        would make the failure permanent for the life of the process.
        """
        with (
            patch.object(async_audit_lifecycle, "_load_checkpoint", return_value=0),
            patch.object(
                async_audit_lifecycle, "_check_unprocessed_wal_entries", return_value=0
            ),
            patch.object(
                async_audit_lifecycle, "_initialize_async_logger", return_value=False
            ),
            patch.object(async_audit_lifecycle, "_start_sync_worker"),
        ):
            result = startup_async_audit_system()

        assert result is False
        assert lifecycle_state.startup_completed is False
        assert lifecycle_state.origin_pid is None

    def test_a_failed_startup_is_retried_by_the_next_caller(self, lifecycle_state):
        """The per-worker post-fork starter is that next caller: the first
        attempt failed, the second one must actually run the steps again.
        """
        with (
            patch.object(async_audit_lifecycle, "_load_checkpoint", return_value=0),
            patch.object(
                async_audit_lifecycle, "_check_unprocessed_wal_entries", return_value=0
            ),
            patch.object(
                async_audit_lifecycle,
                "_initialize_async_logger",
                side_effect=[False, True],
            ) as init_logger,
            patch.object(async_audit_lifecycle, "_start_sync_worker"),
        ):
            first = startup_async_audit_system()
            second = startup_async_audit_system()

        assert (first, second) == (False, True)
        assert init_logger.call_count == 2
        assert lifecycle_state.startup_completed is True

    def test_the_sync_worker_still_starts_when_the_logger_init_fails(
        self, lifecycle_state
    ):
        """The two components are independent: a WAL drain that can run must
        not be suppressed by the batch consumer failing to come up.
        """
        with (
            patch.object(async_audit_lifecycle, "_load_checkpoint", return_value=0),
            patch.object(
                async_audit_lifecycle, "_check_unprocessed_wal_entries", return_value=0
            ),
            patch.object(
                async_audit_lifecycle, "_initialize_async_logger", return_value=False
            ),
            patch.object(async_audit_lifecycle, "_start_sync_worker") as start_worker,
        ):
            startup_async_audit_system()

        start_worker.assert_called_once_with()

    def test_initialize_async_logger_reports_false_when_start_raises(self):
        """The honesty is produced here: the helper swallows the exception (so
        boot survives) but must not report success.
        """
        with patch.object(
            AsyncHealingLogger, "start", side_effect=RuntimeError("no threads left")
        ):
            assert async_audit_lifecycle._initialize_async_logger() is False

    def test_initialize_async_logger_reports_true_on_a_real_start(self):
        try:
            assert async_audit_lifecycle._initialize_async_logger() is True
        finally:
            AsyncHealingLogger.reset()


# =============================================================================
# Metric honesty
# =============================================================================


class TestAuditMetricsHonestyBehavior:
    """``worker_running`` is a published signal; a fork made it a lie."""

    @pytest.fixture(autouse=True)
    def _reset_logger(self):
        AsyncHealingLogger.reset()
        yield
        AsyncHealingLogger.reset()

    def test_inherited_flag_over_a_dead_thread_publishes_not_running(
        self, lifecycle_state
    ):
        """The fork-child shape: ``_running`` inherited True with a thread
        object Python has already marked stopped.
        """
        AsyncHealingLogger._running = True
        AsyncHealingLogger._worker_thread = _dead_thread()

        assert get_async_audit_metrics()["worker_running"] is False

    def test_flag_without_any_thread_publishes_not_running(self, lifecycle_state):
        AsyncHealingLogger._running = True
        AsyncHealingLogger._worker_thread = None

        assert get_async_audit_metrics()["worker_running"] is False

    def test_a_live_consumer_publishes_running(self, lifecycle_state):
        AsyncHealingLogger.start()

        assert get_async_audit_metrics()["worker_running"] is True

    def test_queue_depth_comes_from_the_repaired_statistics_read(self, lifecycle_state):
        """Delegation: reaching into the queue object directly would read
        whatever a fork left behind, so the published depth is the counter the
        repaired ``get_stats()`` returns.
        """
        with patch.object(
            AsyncHealingLogger,
            "get_stats",
            return_value={"events_logged": 3, "current_queue_size": 17},
        ) as get_stats:
            metrics = get_async_audit_metrics()

        get_stats.assert_called_once_with()
        assert metrics["queue_size"] == 17
        assert metrics["events_logged"] == 3

    def test_missing_queue_depth_publishes_zero_rather_than_raising(
        self, lifecycle_state
    ):
        """Boundary: the metrics endpoint is a status surface — an absent key
        must not turn a scrape into an error payload.
        """
        with patch.object(
            AsyncHealingLogger, "get_stats", return_value={"events_logged": 1}
        ):
            metrics = get_async_audit_metrics()

        assert metrics["queue_size"] == 0
        assert "error" not in metrics

    def test_lifecycle_flags_are_published_alongside(self, lifecycle_state):
        lifecycle_state.startup_completed = True
        lifecycle_state.origin_pid = os.getpid()
        lifecycle_state.shutdown_registered = True

        metrics = get_async_audit_metrics()

        assert metrics["lifecycle_startup_completed"] is True
        assert metrics["lifecycle_shutdown_registered"] is True
