"""DLQ outbox fork repair — re-owning inherited state in a forked worker.

The writer is a daemon thread started by ``init()``. Under a fork it does not
exist in the child, and the child inherits ``_is_running=True``, so ``start()``
no-ops on that flag. Every async DLQ store in that child then landed in a
RingBuffer nothing consumed, while ``store_failure`` reported success — and the
buffer has no WAL behind it, so the entries were lost at exit rather than
deferred.

Three rules here look arbitrary and are not:

- The repair sits at the module singleton's **entry points**, not in
  ``start()``. Both ``get_outbox()`` and ``setup_dlq_outbox()`` early-return on
  the inherited singleton before any ``start()`` call, so a ``start()``-sited
  repair is unreachable in a child.
- The inherited buffer contents are **abandoned**, not drained. The forking
  process's own drainer is still delivering them; keeping them would write each
  entry once per process, with nothing downstream to dedup.
- ``_spawn_thread`` guards on thread **aliveness** under a lock, with the handle
  rebind inside it. The repair path and the ``DaemonWorkerProbe`` respawn
  coordinator — a second writer through the inherited handle's
  ``restart_callback`` — must not both pass the check between ``Thread.start()``
  and the rebind and leave two drainers on one buffer.

``os.fork`` is absent on the Windows dev box, so these tests simulate the
inherited state (a PID stamp from another process, a dead writer thread) rather
than forking. Real fork-child thread liveness is owed from the Linux lane.
"""

from __future__ import annotations

import os
import threading

import pytest

from baldur.services.dlq_outbox import outbox as outbox_module
from baldur.services.dlq_outbox.outbox import (
    _repair_if_forked,
    get_outbox,
    setup_dlq_outbox,
)

# A PID this process cannot be. ``os.getpid()`` is positive on every supported
# platform, so a negative stamp is unambiguously "some other process".
_FOREIGN_PID = -1


@pytest.fixture
def install_module_outbox(build_outbox, make_sync_writer, collected_writes):
    """Install a started Outbox as the module singleton, stamped by a foreign PID.

    This is the shape a fork child inherits: a live-looking singleton whose
    writer thread does not run here. The dead thread is produced by stopping the
    real one, which is what ``fork()`` leaves behind — a thread object Python has
    already marked stopped.
    """

    def _install(*, origin_pid: int = _FOREIGN_PID, entries: int = 0):
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(writer, flush_interval_seconds=0.01)
        outbox.start()

        # Simulate the fork: the writer thread is gone, the running flag is not.
        worker._stop_event.set()
        worker._thread.join(timeout=2.0)
        assert not worker._thread.is_alive()
        worker._stop_event.clear()

        for index in range(entries):
            buffer.put((0.0, {"entry": index}))

        outbox_module._outbox = outbox
        outbox_module._outbox_origin_pid = origin_pid
        return outbox, buffer, worker

    return _install


class TestOutboxForkRepairContract:
    """The repair has to be reachable from both module entry points."""

    def test_get_outbox_carries_the_fork_repair_marker(self):
        """The producer's lazy path reaches the singleton without the starter."""
        assert get_outbox.__fork_repaired__ is True

    def test_setup_dlq_outbox_carries_the_fork_repair_marker(self):
        """The starter's path — the one a forked worker actually takes."""
        assert setup_dlq_outbox.__fork_repaired__ is True


class TestOutboxForkRepairBehavior:
    """``_repair_if_forked()`` — re-owning the module singleton after a fork."""

    def test_pid_mismatch_renews_the_module_locks(self, install_module_outbox):
        """The forking process holds ``_outbox_lock`` across the whole build.

        A fork taken at that instant hands the child a lock no thread here will
        ever release, so both module locks are replaced before anything
        acquires one.
        """
        install_module_outbox()
        inherited_outbox_lock = outbox_module._outbox_lock
        inherited_worker_dead_lock = outbox_module._worker_dead_lock

        _repair_if_forked()

        assert outbox_module._outbox_lock is not inherited_outbox_lock
        assert outbox_module._worker_dead_lock is not inherited_worker_dead_lock

    def test_pid_mismatch_clears_the_inherited_worker_dead_flag(
        self, install_module_outbox
    ):
        """The flag describes the *parent's* drainer.

        Coercing every store to the sync writer on that basis would cost the
        child the async path it is about to get back.
        """
        install_module_outbox()
        outbox_module._worker_dead = True
        outbox_module._worker_dead_coercions = 7

        _repair_if_forked()

        assert outbox_module._worker_dead is False
        assert outbox_module._worker_dead_coercions == 0

    def test_pid_mismatch_abandons_the_inherited_buffer_contents(
        self, install_module_outbox
    ):
        """The parent's live drainer owns those entries.

        Draining the copies here would write one duplicate downstream per child
        — up to N+1 rows for one failure, with nothing to dedup them.
        """
        _, buffer, _ = install_module_outbox(entries=5)
        assert buffer.size == 5

        _repair_if_forked()

        assert buffer.size == 0
        # Counters restart with the contents, so the drop-rate alert reflects
        # this process's own traffic rather than a pre-fork rate.
        assert buffer.get_stats().total_enqueued == 0

    def test_pid_mismatch_respawns_a_live_writer(self, install_module_outbox):
        """The whole point: the child drains its own outbox."""
        _, _, worker = install_module_outbox()
        assert worker.is_alive is False

        _repair_if_forked()

        assert worker.is_alive is True

    def test_pid_mismatch_restamps_the_singleton_with_this_process(
        self, install_module_outbox
    ):
        """A repaired singleton must not repair again on the next entry-point call."""
        install_module_outbox()

        _repair_if_forked()

        assert outbox_module._outbox_origin_pid == os.getpid()

    def test_repeated_repair_does_not_respawn_a_second_writer(
        self, install_module_outbox
    ):
        """Both entry points run the repair; only the first one has work to do."""
        _, _, worker = install_module_outbox()
        _repair_if_forked()
        first_thread = worker._thread

        _repair_if_forked()

        assert worker._thread is first_thread
        assert worker.is_alive is True

    def test_matching_pid_leaves_everything_alone(self, install_module_outbox):
        """The ordinary in-process call — the repair must cost it nothing."""
        _, buffer, worker = install_module_outbox(origin_pid=os.getpid(), entries=3)
        inherited_lock = outbox_module._outbox_lock

        _repair_if_forked()

        assert outbox_module._outbox_lock is inherited_lock
        assert buffer.size == 3
        assert worker.is_alive is False

    def test_unstamped_singleton_is_left_alone(self, install_module_outbox):
        """No stamp means nothing was ever built here — there is nothing to re-own."""
        _, buffer, _ = install_module_outbox(origin_pid=os.getpid(), entries=2)
        outbox_module._outbox_origin_pid = None
        inherited_lock = outbox_module._outbox_lock

        _repair_if_forked()

        assert outbox_module._outbox_lock is inherited_lock
        assert buffer.size == 2

    def test_setup_returns_false_but_leaves_a_live_writer_in_a_fork_child(
        self, install_module_outbox
    ):
        """The starter's path, end to end.

        ``False`` here means "this process did not build one", not "nothing was
        started" — the decorator already re-owned and respawned the inherited
        singleton by the time the idempotence guard is reached. Before the
        repair moved to the entry points, this returned ``False`` having started
        nothing at all.
        """
        _, buffer, worker = install_module_outbox(entries=4)

        started_here = setup_dlq_outbox()

        assert started_here is False
        assert worker.is_alive is True
        assert buffer.size == 0
        assert outbox_module._outbox_origin_pid == os.getpid()

    def test_producer_lazy_path_also_repairs_in_a_fork_child(
        self, install_module_outbox
    ):
        """Defense in depth: the producer reaches the singleton without the starter."""
        outbox, _, worker = install_module_outbox()

        returned = get_outbox()

        assert returned is outbox
        assert worker.is_alive is True


class TestOutboxWorkerForkRepairBehavior:
    """``DLQOutboxWorker.repair_after_fork()`` — the writer's own re-own step."""

    @pytest.fixture
    def forked_worker(self, build_outbox, make_sync_writer, collected_writes):
        """A started worker whose thread has died, as a fork child inherits it."""
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(writer, flush_interval_seconds=0.01)
        outbox.start()
        worker._stop_event.set()
        worker._thread.join(timeout=2.0)
        worker._stop_event.clear()
        try:
            yield outbox, buffer, worker
        finally:
            outbox.stop(timeout=1.0)

    def test_repair_restarts_the_statistics(self, forked_worker):
        """The parent's counts describe the parent's writes.

        Kept, they would leave the conservation invariant open in the child,
        whose buffer counters restart at zero with its contents.
        """
        _, _, worker = forked_worker
        worker._entries_written = 9
        worker._entries_failed = 4
        worker._entries_emergency_dumped = 2
        worker._in_flight = 3
        worker._consecutive_failures = 5

        worker.repair_after_fork()

        assert worker.entries_written == 0
        assert worker.entries_failed == 0
        assert worker.entries_emergency_dumped == 0
        assert worker.in_flight == 0
        assert worker.consecutive_failures == 0

    def test_repair_renews_the_inherited_lock_and_event(self, forked_worker):
        """Each may have been inherited held by a thread that does not exist here."""
        _, _, worker = forked_worker
        inherited_lock = worker._spawn_lock
        inherited_event = worker._stop_event

        worker.repair_after_fork()

        assert worker._spawn_lock is not inherited_lock
        assert worker._stop_event is not inherited_event

    def test_repair_preserves_the_handle_identity(self, forked_worker):
        """The registry entry and the inherited ``restart_callback`` point here.

        A probe tick that races the respawn converges on this object instead of
        chasing a stale entry.
        """
        _, _, worker = forked_worker
        inherited_handle = worker.handle

        worker.repair_after_fork()

        assert worker.handle is inherited_handle

    def test_repair_rebinds_the_handle_to_the_new_thread(self, forked_worker):
        """The probe observes the thread this process actually runs."""
        _, _, worker = forked_worker
        dead_thread = worker._thread

        worker.repair_after_fork()

        assert worker._thread is not dead_thread
        assert worker.handle.thread is worker._thread
        assert worker.is_alive is True

    def test_repair_keeps_the_worker_running_so_stop_stays_reachable(
        self, forked_worker
    ):
        """``_is_running`` describes this process the moment the spawn returns."""
        _, _, worker = forked_worker

        worker.repair_after_fork()

        assert worker.is_running is True

    def test_repair_leaves_exactly_one_live_writer_thread(self, forked_worker):
        """Two drainers on one buffer would write every entry twice."""
        _, _, worker = forked_worker
        dead_thread = worker._thread

        worker.repair_after_fork()

        assert dead_thread.is_alive() is False
        assert worker._thread.is_alive() is True

    def test_repair_keeps_a_writer_the_probe_already_respawned(self, forked_worker):
        """A probe respawn that beat the repair must not be stranded.

        The ``DaemonWorkerProbe`` reaches ``_spawn_thread`` through the
        inherited handle's ``restart_callback``; if its tick lands before the
        entry-point repair (the repair-first starter order makes this
        defense in depth, not the designed sequence), the repair used to null
        the reference to that live writer and spawn a second one — two
        drainers on one buffer, every entry written twice, and ``stop()``
        joining only the newer thread.
        """
        # Given — the probe respawned the dead inherited writer first
        _, _, worker = forked_worker
        worker._spawn_thread()
        probe_spawned = worker._thread
        assert probe_spawned.is_alive() is True

        # When — the fork repair runs afterwards
        worker.repair_after_fork()

        # Then — the probe's writer is kept; no second drainer exists
        assert worker._thread is probe_spawned
        assert worker.handle.thread is probe_spawned
        assert worker.is_running is True
        live_writers = [
            t
            for t in threading.enumerate()
            if t.name == "DLQOutboxWorker" and t.is_alive()
        ]
        assert live_writers == [probe_spawned]


class TestOutboxSpawnExclusionBehavior:
    """``_spawn_thread()`` is the single spawn seam.

    The thread body is replaced with a recorder so the number of spawns is
    observable: nothing else distinguishes "one thread started" from "two
    threads started and one already exited", and the real ``_writer_loop`` is
    not the subject here.
    """

    @pytest.fixture
    def spawn_recorder(self, build_outbox, make_sync_writer, collected_writes):
        """A worker whose thread body parks on a gate and records its own start."""
        writer = make_sync_writer(collected_writes)
        outbox, buffer, worker = build_outbox(writer, flush_interval_seconds=0.01)

        started: list[threading.Thread] = []
        gate = threading.Event()

        def _parked_loop() -> None:
            started.append(threading.current_thread())
            gate.wait(timeout=5.0)

        worker._writer_loop_with_crash_capture = _parked_loop
        try:
            yield worker, started, gate
        finally:
            gate.set()
            for thread in started:
                thread.join(timeout=2.0)

    def test_live_writer_is_not_replaced_by_another_spawn(self, spawn_recorder):
        """A genuinely dead writer respawns; a live one is deliberately left alone."""
        worker, started, _ = spawn_recorder
        worker._spawn_thread()
        original = worker._thread

        worker._spawn_thread()

        assert worker._thread is original
        assert len(started) == 1

    def test_concurrent_spawns_on_a_live_writer_start_nothing(self, spawn_recorder):
        """The aliveness guard holds under the probe's own concurrency."""
        # Given — one live writer
        worker, started, _ = spawn_recorder
        worker._spawn_thread()

        # When — eight callers race the seam, as the probe's restart_callback can
        threads = [threading.Thread(target=worker._spawn_thread) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        # Then
        assert len(started) == 1

    def test_concurrent_spawns_on_a_dead_writer_start_exactly_one(self, spawn_recorder):
        """The race this lock was added for: the start->rebind window.

        Without the lock the repair path and a probe tick can both observe the
        dead inherited thread and start one writer each.
        """
        # Given — the fork-child shape: no thread object at all
        worker, started, _ = spawn_recorder
        worker._thread = None

        # When
        threads = [threading.Thread(target=worker._spawn_thread) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        # Then
        assert len(started) == 1
        assert worker._thread is started[0]

    def test_spawn_resets_in_flight_so_the_conservation_invariant_reopens(
        self, spawn_recorder
    ):
        """A freshly spawned thread has nothing in flight by definition.

        A crash mid-flush that leaks a positive count would otherwise make
        ``flush_and_wait`` block to its timeout forever after recovery.
        """
        worker, _, _ = spawn_recorder
        worker._in_flight = 3

        worker._spawn_thread()

        assert worker.in_flight == 0
