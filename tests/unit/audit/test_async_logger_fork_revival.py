"""Behavioural coverage for ``AsyncHealingLogger``'s fork revival.

Under ``gunicorn --preload`` the master builds this pipeline and none of it
survives ``fork()``: the consumer thread does not exist in the child, the class
locks may record an owner that never releases them, and the queue holds copies
of events the parent is still delivering itself. Three surfaces are covered:

- ``_repair_if_forked()`` — what it renews, what it deliberately carries, and
  the events it must NOT re-deliver.
- ``flush()`` — the aliveness branch, without which a child signals a consumer
  that does not exist and waits out the full timeout having flushed nothing.
- ``start()`` — honest failure reporting, and the executor ensured on the
  already-running path.

The structural gates that derive *which* members must repair live in
``test_audit_fork_lifecycle.py``; this module asserts what the repair does.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- State transition: origin-PID stamp, latch clearing
- Idempotency: origin None / same PID / repeated repair
- Object identity: locks, Events, queues and rate-limiting primitives replaced
- Negative side effect: inherited queue never delivered, ``_running`` untouched
- Branch selection: flush delegation vs synchronous drain
- Error injection: a spawn that raises leaves no phantom running state
"""

from __future__ import annotations

import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from baldur.utils.async_logger import (
    AsyncHealingLogger,
    EventSeverity,
    FlushErrorAlertConfig,
)

# The PID stamp a fork child would find: a value no live process here owns.
FOREIGN_PID = os.getpid() + 1


@pytest.fixture
def logger_class():
    """``AsyncHealingLogger`` with class state reset either side of the test.

    Every attribute this suite touches is class-level, so a leaked
    ``_running`` or a leaked origin stamp would change the next test's branch.
    """
    AsyncHealingLogger.reset()
    yield AsyncHealingLogger
    AsyncHealingLogger.reset()


def _simulate_fork(cls) -> None:
    """Stamp the class as owned by another process, then repair."""
    cls._origin_pid = FOREIGN_PID
    cls._repair_if_forked()


def _dead_thread() -> threading.Thread:
    """A started-and-finished thread — ``is_alive()`` False, not None."""
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    return thread


# =============================================================================
# _repair_if_forked
# =============================================================================


class TestAsyncLoggerForkRepairBehavior:
    """The repair re-owns inherited state; anything else is left alone."""

    def test_repair_is_a_noop_when_no_ancestor_ever_started(self, logger_class):
        """``_origin_pid is None`` is the un-started stamp. Every unit-test
        process sits here, so the repair must not fire over state nobody owns.
        """
        logger_class._origin_pid = None
        lock_before = logger_class._lock

        logger_class._repair_if_forked()

        assert logger_class._origin_pid is None
        assert logger_class._lock is lock_before

    def test_repair_is_a_noop_in_the_owning_process(self, logger_class):
        """The owner never mismatches, which is why the repair is affordable
        on ``log()``.
        """
        logger_class._origin_pid = os.getpid()
        lock_before = logger_class._lock
        queue_before = logger_class._priority_queue

        logger_class._repair_if_forked()

        assert logger_class._lock is lock_before
        assert logger_class._priority_queue is queue_before

    def test_repair_claims_ownership_for_this_process(self, logger_class):
        _simulate_fork(logger_class)

        assert logger_class._origin_pid == os.getpid()

    def test_repair_replaces_both_class_locks(self, logger_class):
        """An inherited ``RLock`` records an owner thread that does not exist
        here and will never release it — the child's first ``configure()``
        would block forever.
        """
        lock_before = logger_class._lock
        count_lock_before = logger_class._queue_count_lock

        _simulate_fork(logger_class)

        assert logger_class._lock is not lock_before
        assert logger_class._queue_count_lock is not count_lock_before

    def test_repair_replaces_the_flush_events(self, logger_class):
        """``Event`` objects are replaced rather than ``clear()``-ed: an
        Event's own internal lock can be inherited held.
        """
        requested_before = logger_class._flush_requested
        done_before = logger_class._flush_done

        _simulate_fork(logger_class)

        assert logger_class._flush_requested is not requested_before
        assert logger_class._flush_done is not done_before
        assert logger_class._flush_requested.is_set() is False
        assert logger_class._flush_done.is_set() is False

    def test_repair_replaces_the_queues_rather_than_nulling_them(self, logger_class):
        """A ``None`` queue makes the consumer loop busy-spin and makes
        enqueue silently drop, so the repair must hand back real objects.
        """
        logger_class.start()
        priority_before = logger_class._priority_queue
        queue_before = logger_class._queue

        _simulate_fork(logger_class)

        assert isinstance(logger_class._priority_queue, queue.PriorityQueue)
        assert isinstance(logger_class._queue, queue.Queue)
        assert logger_class._priority_queue is not priority_before
        assert logger_class._queue is not queue_before
        assert logger_class._queue_count == 0

    def test_inherited_queue_contents_are_never_delivered(self, logger_class):
        """The parent's live pipeline owns those events and delivers them
        exactly once. Flushing the child's copies would write one duplicate per
        worker into the audit trail with nothing downstream to dedup them.
        """
        delivered: list[dict] = []
        logger_class.configure(delivered.append)
        logger_class._priority_queue = queue.PriorityQueue()
        logger_class.log({"type": "inherited_from_parent"}, EventSeverity.INFO)
        assert logger_class._priority_queue.qsize() == 1

        _simulate_fork(logger_class)
        logger_class.flush()

        assert delivered == []

    def test_repair_zeroes_the_inherited_statistics(self, logger_class):
        """They are this-process counters published through ``get_stats()``;
        inherited, a worker that logged three events reports the parent's
        thousands.
        """
        logger_class._origin_pid = os.getpid()
        logger_class.log({"type": "parent_event"}, EventSeverity.INFO)
        assert logger_class.get_stats()["events_logged"] > 0

        _simulate_fork(logger_class)

        assert logger_class.get_stats()["events_logged"] == 0

    def test_repair_drops_the_inherited_thread_and_executor(self, logger_class):
        """Neither exists in the child. The executor is dropped *without*
        ``shutdown()`` — its worker threads are the parent's.
        """
        logger_class._worker_thread = _dead_thread()
        logger_class._critical_executor = ThreadPoolExecutor(max_workers=1)
        executor = logger_class._critical_executor

        with patch.object(executor, "shutdown") as shutdown:
            _simulate_fork(logger_class)

        assert logger_class._worker_thread is None
        assert logger_class._critical_executor is None
        shutdown.assert_not_called()
        executor.shutdown(wait=False)

    def test_repair_leaves_the_running_flag_to_start_and_stop(self, logger_class):
        """Negative: ``_running`` stays owned by start/stop. The honest guards
        compose thread aliveness instead of clearing the flag here.
        """
        logger_class._running = True

        _simulate_fork(logger_class)

        assert logger_class._running is True

    def test_repair_keeps_the_handle_object_and_resets_its_observations(
        self, logger_class
    ):
        """Identity is kept so a respawn converges on this handle; the
        observations on it belong to the parent's run.
        """
        logger_class.start()
        handle = logger_class._handle
        handle.restart_count = 3
        handle.last_crash_reason = "parent crash"

        _simulate_fork(logger_class)

        assert logger_class._handle is handle
        assert handle.restart_count == 0
        assert handle.last_crash_reason is None

    def test_repair_renews_the_error_window_and_alert_gate(self, logger_class):
        """Each owns a lock — an inherited held one deadlocks the child's first
        flush-error path.
        """
        window_before = logger_class._error_window
        gate_before = logger_class._alert_gate

        _simulate_fork(logger_class)

        assert logger_class._error_window is not window_before
        assert logger_class._alert_gate is not gate_before

    def test_child_can_page_on_its_first_breach_after_a_parent_page(self, logger_class):
        """Negative half of the renewal: the cooldown gate carries a live
        *reservation*, and an inherited one suppresses the child's first page
        for the remainder of the parent's cooldown — a missed alert, not a
        slow one.
        """
        logger_class._origin_pid = os.getpid()
        logger_class._alert_config = FlushErrorAlertConfig(
            threshold_count=1, window_seconds=60.0, cooldown_seconds=3600.0
        )
        with patch.object(
            logger_class, "_send_flush_error_alert", return_value=True
        ) as send:
            logger_class._error_window.record_and_count("flush_error", 60.0)
            logger_class._check_and_send_alert()
            assert send.call_count == 1

            # Same process, still inside the cooldown: suppressed.
            logger_class._error_window.record_and_count("flush_error", 60.0)
            logger_class._check_and_send_alert()
            assert send.call_count == 1

            # The child inherits that reservation, and must not honour it.
            _simulate_fork(logger_class)
            logger_class._error_window.record_and_count("flush_error", 60.0)
            logger_class._check_and_send_alert()

        assert send.call_count == 2

    def test_second_repair_in_the_same_process_is_a_noop(self, logger_class):
        """Idempotency: after the first repair the stamp matches, so a later
        entry point does one attribute load and returns.
        """
        _simulate_fork(logger_class)
        lock_after_first = logger_class._lock

        logger_class._repair_if_forked()

        assert logger_class._lock is lock_after_first

    def test_a_public_entry_point_triggers_the_repair(self, logger_class):
        """The decorator is what makes the repair lazy: the child's first
        class-lock acquisition on the revival path is ``configure()``, not
        ``start()``.
        """
        logger_class._origin_pid = FOREIGN_PID

        logger_class.configure(lambda events: None)

        assert logger_class._origin_pid == os.getpid()


# =============================================================================
# flush() aliveness branch
# =============================================================================


class TestAsyncLoggerFlushAlivenessBehavior:
    """``flush()`` picks its branch on a live consumer, not on ``_running``."""

    def test_flush_drains_synchronously_when_the_consumer_is_dead(self, logger_class):
        """The fork-child shape: ``_running`` inherited True over a thread
        Python has already marked stopped. A flag-only predicate would signal
        nobody and wait out the timeout having delivered nothing.
        """
        delivered: list[list[dict]] = []
        logger_class.configure(delivered.append)
        logger_class._priority_queue = queue.PriorityQueue()
        logger_class.log({"type": "pending"}, EventSeverity.INFO)
        logger_class._running = True
        logger_class._worker_thread = _dead_thread()

        logger_class.flush()

        assert [e["type"] for batch in delivered for e in batch] == ["pending"]
        # The dead-consumer branch signals nothing.
        assert logger_class._flush_requested.is_set() is False

    def test_flush_drains_synchronously_when_there_is_no_consumer_at_all(
        self, logger_class
    ):
        """A hookless deployment whose consumer never started: same branch."""
        delivered: list[list[dict]] = []
        logger_class.configure(delivered.append)
        logger_class._priority_queue = queue.PriorityQueue()
        logger_class.log({"type": "pending"}, EventSeverity.INFO)
        logger_class._running = True
        logger_class._worker_thread = None

        logger_class.flush()

        assert [e["type"] for batch in delivered for e in batch] == ["pending"]

    def test_flush_delegates_to_a_live_consumer(self, logger_class):
        """The delegating branch is the only way to include events already
        dequeued into the worker's local batch, so a live consumer must get the
        request rather than have the caller drain behind its back.
        """
        delivered: list[list[dict]] = []
        logger_class.configure(delivered.append)
        logger_class._priority_queue = queue.PriorityQueue()
        logger_class.log({"type": "pending"}, EventSeverity.INFO)
        logger_class._running = True

        blocker = threading.Event()
        live = threading.Thread(target=blocker.wait, daemon=True)
        live.start()
        logger_class._worker_thread = live
        try:
            with patch.object(logger_class._flush_done, "wait") as wait:
                logger_class.flush()
        finally:
            blocker.set()
            live.join(timeout=5.0)

        # Branch assertion, not a clock assertion: the request was raised and
        # the caller waited on the worker instead of draining the queue itself.
        assert logger_class._flush_requested.is_set() is True
        wait.assert_called_once()
        assert delivered == []
        assert logger_class._priority_queue.qsize() == 1

    def test_repaired_child_flush_reaches_the_synchronous_drain(self, logger_class):
        """Composed: after the repair the thread reference is ``None``, so the
        child's flush delivers its own events instead of waiting on the
        parent's consumer.
        """
        delivered: list[list[dict]] = []
        logger_class._origin_pid = os.getpid()
        logger_class._running = True
        logger_class._worker_thread = _dead_thread()

        _simulate_fork(logger_class)
        logger_class.configure(delivered.append)
        logger_class.log({"type": "child_event"}, EventSeverity.INFO)
        logger_class.flush()

        assert [e["type"] for batch in delivered for e in batch] == ["child_event"]


# =============================================================================
# start() honesty
# =============================================================================


class TestAsyncLoggerStartHonestyBehavior:
    """A start that did not happen must not be readable as one that did."""

    def test_failed_spawn_restores_the_running_flag_and_propagates(self, logger_class):
        """Error injection: a container thread cap makes ``Thread.start()``
        raise. Leaving ``_running`` True would make every later guard read a
        pipeline that never existed.
        """
        with (
            patch.object(
                logger_class,
                "_spawn_worker_thread",
                side_effect=RuntimeError("can't start new thread"),
            ),
            pytest.raises(RuntimeError, match="can't start new thread"),
        ):
            logger_class.start()

        assert logger_class._running is False

    def test_failed_spawn_registers_no_daemon_worker_handle(self, logger_class):
        """Negative side effect: the handle is built from the thread the spawn
        was supposed to produce, so a failed start must register nothing and
        build no handle over a thread that does not exist.
        """
        handle_before = logger_class._handle

        with (
            patch(
                "baldur.metrics.recorders.daemon_worker.register_daemon_worker"
            ) as register,
            patch.object(
                logger_class, "_spawn_worker_thread", side_effect=RuntimeError("nope")
            ),
            pytest.raises(RuntimeError),
        ):
            logger_class.start()

        register.assert_not_called()
        assert logger_class._handle is handle_before

    def test_start_over_a_live_worker_ensures_the_critical_executor(self, logger_class):
        """A revival that came in through the respawn callback rather than
        through ``start()`` leaves a live consumer and no pool. Without this,
        CRITICAL events stay pinned on the per-event-thread fallback for the
        rest of the process's life.
        """
        logger_class.start()
        worker_thread = logger_class._worker_thread
        assert worker_thread is not None
        assert worker_thread.is_alive()
        logger_class._critical_executor.shutdown(wait=True)
        logger_class._critical_executor = None

        logger_class.start()

        assert isinstance(logger_class._critical_executor, ThreadPoolExecutor)
        # State invariant: the already-running path did not spawn a second
        # consumer over the one queue.
        assert logger_class._worker_thread is worker_thread

    def test_start_over_a_live_worker_keeps_an_existing_executor(self, logger_class):
        """Idempotency: the ensure is conditional, not a rebuild — a rebuild
        would strand the futures already submitted to the live pool.
        """
        logger_class.start()
        executor = logger_class._critical_executor

        logger_class.start()

        assert logger_class._critical_executor is executor


# =============================================================================
# Spawn helper convergence
# =============================================================================


class TestAsyncLoggerSpawnConvergenceBehavior:
    """``_spawn_worker_thread`` is the single atomic spawn point.

    The meta-watchdog's respawn callback reaches it without passing any public
    entry point, and its contract forbids consulting ``_running`` — so the fork
    repair and the spawn idempotence both live here rather than in ``start()``.
    """

    def test_spawn_is_skipped_while_the_consumer_is_alive(self, logger_class):
        """A respawn racing ``start()`` would otherwise leave two consumers
        pulling from one queue.
        """
        logger_class.start()
        first = logger_class._worker_thread

        logger_class._spawn_worker_thread()

        assert logger_class._worker_thread is first

    def test_spawn_replaces_a_dead_consumer(self, logger_class):
        """Guarded on aliveness, never on ``_running``."""
        logger_class._running = True
        dead = _dead_thread()
        logger_class._worker_thread = dead

        logger_class._spawn_worker_thread()

        assert logger_class._worker_thread is not dead
        assert logger_class._worker_thread.is_alive()

    def test_concurrent_spawns_leave_exactly_one_consumer(self, logger_class):
        """Concurrency: the respawn callback and the starter can arrive
        together in a revived worker.
        """
        logger_class._running = True
        spawned: list[threading.Thread] = []
        start_together = threading.Barrier(4)

        def racer():
            start_together.wait(timeout=5.0)
            logger_class._spawn_worker_thread()
            spawned.append(logger_class._worker_thread)

        racers = [threading.Thread(target=racer, daemon=True) for _ in range(4)]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join(timeout=5.0)

        assert len({id(t) for t in spawned}) == 1

    def test_spawn_rebinds_the_handles_thread(self, logger_class):
        """The registry keeps pointing at the same handle, so a respawned
        thread has to be published on it or the probe watches a corpse.
        """
        logger_class.start()
        handle = logger_class._handle
        logger_class._running = False
        logger_class._worker_thread.join(timeout=5.0)

        logger_class._running = True
        logger_class._spawn_worker_thread()

        assert handle.thread is logger_class._worker_thread
        assert handle.thread.is_alive()

    def test_a_respawn_over_inherited_state_repairs_first(self, logger_class):
        """Composed: a watchdog respawn is a revival trigger that never passes
        a public entry point, so without the repair here it would consume the
        parent's queue copies.
        """
        logger_class._running = True
        logger_class._worker_thread = _dead_thread()
        inherited_queue = queue.PriorityQueue()
        logger_class._priority_queue = inherited_queue
        logger_class._origin_pid = FOREIGN_PID

        logger_class._spawn_worker_thread()

        assert logger_class._origin_pid == os.getpid()
        assert logger_class._priority_queue is not inherited_queue
        assert logger_class._worker_thread.is_alive()
