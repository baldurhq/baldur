"""Behavioural coverage for ``AuditSyncWorker``'s fork revival.

The WAL drain loop is one of the three components the audit pipeline revives
per forked worker. Four surfaces are covered here:

- ``_repair_if_forked()`` — instance-preserving renewal of the state a
  ``fork()`` makes unusable, and the one field that is deliberately carried
  (``_last_processed_seq``: the child's WAL sequence continues above it, so
  resetting it is the mutation that silently swallows entries).
- ``_absorb_orphans_once()`` / ``_absorb_orphans_pass()`` — the one-shot is
  consumed only by a pass that achieved something against a *real*
  destination, and a pass aborts as soon as an unreachable store is proven.
- ``_resolve_central_destination()`` — the no-op adapter is not a destination;
  treating it as one makes "delivered" satisfiable by a method whose body is
  ``pass``.
- ``is_running`` and the spawn helper — status honesty and spawn convergence
  over inherited state.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- State transition: origin-PID stamp, latch clearing, thread reference drop
- Object identity: the instance survives, the lock and the ``Event`` do not
- Decision table: the one-shot's four outcomes; the destination's four classes
- Call counting: the absorb's retry budget is per pass, not per entry
- Concurrency: a watchdog respawn racing the starter leaves one thread
- Negative side effect: a status read never mutates
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.adapters.audit.null_adapter import NullAuditLogAdapter
from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALEntry
from baldur.interfaces.audit_adapter import AuditLogAdapter

FOREIGN_PID = os.getpid() + 1


@pytest.fixture
def worker():
    """A directly-constructed (non-singleton) worker — never pollutes
    ``AuditSyncWorker._instance``.
    """
    instance = AuditSyncWorker(
        wal=None, central_adapter=MagicMock(spec=AuditLogAdapter)
    )
    yield instance
    instance.stop(timeout=1.0)


def _simulate_fork(instance: AuditSyncWorker) -> None:
    instance._origin_pid = FOREIGN_PID
    instance._repair_if_forked()


def _dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    return thread


def _orphan_entry(sequence: int) -> WALEntry:
    """A real WAL entry — the type ``recover_orphans()`` actually returns."""
    return WALEntry(
        sequence=sequence,
        timestamp=float(sequence),
        data={"event": "orphan"},
        checksum="abcdef12",
    )


# =============================================================================
# _repair_if_forked
# =============================================================================


class TestSyncWorkerForkRepairBehavior:
    """Renew what the fork broke; carry the cursor; keep the instance."""

    def test_repair_is_a_noop_in_the_constructing_process(self, worker):
        """An instance constructed here is born owned, so the repair no-ops
        until the object is actually inherited.
        """
        lock_before = worker._lock
        stop_event_before = worker._stop_event

        worker._repair_if_forked()

        assert worker._lock is lock_before
        assert worker._stop_event is stop_event_before

    def test_repair_claims_ownership_for_this_process(self, worker):
        _simulate_fork(worker)

        assert worker._origin_pid == os.getpid()

    def test_repair_replaces_the_lock_and_the_stop_event(self, worker):
        """The lock's recorded owner is a thread that no longer exists, and an
        ``Event``'s own internal lock can be inherited held — so both are
        replaced rather than reset in place.
        """
        lock_before = worker._lock
        stop_event_before = worker._stop_event

        _simulate_fork(worker)

        assert worker._lock is not lock_before
        assert worker._stop_event is not stop_event_before
        assert worker._stop_event.is_set() is False

    def test_repair_drops_the_reference_to_the_parents_thread(self, worker):
        worker._thread = _dead_thread()

        _simulate_fork(worker)

        assert worker._thread is None

    def test_repair_preserves_instance_identity(self, worker):
        """The registry handle and the respawn callback keep referencing this
        object, so every existing reference must stay valid.
        """
        before = id(worker)

        _simulate_fork(worker)

        assert id(worker) == before

    def test_repair_carries_the_sync_cursor_exactly(self, worker):
        """Negative: ``_last_processed_seq`` is NOT reset. The child's WAL
        carries the inherited sequence forward, so its first new entry is
        strictly above this cursor and gets drained; resetting it re-reads a
        foreign span and re-delivers it.
        """
        worker._last_processed_seq = 4711

        _simulate_fork(worker)

        assert worker._last_processed_seq == 4711

    def test_repair_drops_the_parents_statistics(self, worker):
        """They report on the parent's run; inherited, a fresh child publishes
        the parent's totals.
        """
        worker._stats.total_synced = 900
        worker._stats.total_failed = 12
        stats_before = worker._stats

        _simulate_fork(worker)

        assert worker._stats is not stats_before
        assert worker.get_stats()["total_synced"] == 0
        assert worker.get_stats()["total_failed"] == 0

    def test_repair_clears_every_process_local_latch(self, worker):
        """The edge-triggered warning latches and the stall counters are
        per-episode state; inherited, the child never warns about its own first
        unwired episode or its own stall.
        """
        worker._no_adapter_warned = True
        worker._cursor_stall_alerted = True
        worker._stall_cycles = 9
        worker._batches_since_checkpoint = 5

        _simulate_fork(worker)

        assert worker._no_adapter_warned is False
        assert worker._cursor_stall_alerted is False
        assert worker._stall_cycles == 0
        assert worker._batches_since_checkpoint == 0

    def test_repair_re_arms_the_one_shot_absorb(self, worker):
        """The child has its own dead peers to absorb — including, after a
        crash, the ones its parent never got to.
        """
        worker._orphans_absorbed = True

        _simulate_fork(worker)

        assert worker._orphans_absorbed is False

    def test_repair_leaves_the_running_flag_to_start_and_stop(self, worker):
        """Negative: the honest guards compose thread aliveness instead."""
        worker._running = True

        _simulate_fork(worker)

        assert worker._running is True

    def test_repair_keeps_the_handle_and_resets_its_observations(self, worker):
        from baldur.meta.daemon_worker import DaemonWorkerHandle

        handle = DaemonWorkerHandle(thread=_dead_thread(), tick_interval_seconds=1.0)
        handle.restart_count = 6
        worker._handle = handle

        _simulate_fork(worker)

        assert worker._handle is handle
        assert handle.restart_count == 0

    def test_second_repair_in_the_same_process_is_a_noop(self, worker):
        _simulate_fork(worker)
        lock_after_first = worker._lock

        worker._repair_if_forked()

        assert worker._lock is lock_after_first

    def test_a_public_entry_point_triggers_the_repair(self, worker):
        worker._origin_pid = FOREIGN_PID

        worker.get_stats()

        assert worker._origin_pid == os.getpid()


# =============================================================================
# The one-shot orphan absorb
# =============================================================================


class TestAbsorbOneShotBehavior:
    """A one-shot consumed on a pass that achieved nothing strands a dead
    peer's backlog for the whole life of this process, even though the central
    store returns a minute later.
    """

    @pytest.mark.parametrize(
        ("pass_result", "consumed"),
        [
            (None, False),
            ((0, 0), True),
            ((0, 3), False),
            ((2, 5), True),
            ((4, 4), True),
        ],
        ids=[
            "could_not_run",
            "no_orphans_found",
            "attempted_but_delivered_none",
            "partial_delivery",
            "full_delivery",
        ],
    )
    def test_one_shot_is_consumed_only_by_a_pass_that_achieved_something(
        self, worker, pass_result, consumed
    ):
        with patch.object(worker, "_absorb_orphans_pass", return_value=pass_result):
            worker._absorb_orphans_once()

        assert worker._orphans_absorbed is consumed

    def test_an_unreachable_store_is_retried_on_the_next_cycle(self, worker):
        """The decision table's point: "a destination object exists" is not
        "the destination is reachable". The pass that failed everything must
        run again, and the pass that finally lands consumes the one-shot.
        """
        with patch.object(
            worker, "_absorb_orphans_pass", side_effect=[(0, 3), (0, 3), (3, 3)]
        ) as absorb_pass:
            worker._absorb_orphans_once()
            assert worker._orphans_absorbed is False
            worker._absorb_orphans_once()
            assert worker._orphans_absorbed is False
            worker._absorb_orphans_once()

        assert worker._orphans_absorbed is True
        assert absorb_pass.call_count == 3

    def test_pass_aborts_once_an_unreachable_store_is_proven(self, worker):
        """Call counting: an unreachable store is a property of the
        destination, not of the entry, so there is nothing to learn from
        attempting the other N-1. With three orphans the adapter is called
        ``max_retries + 1`` times in total, not once per entry.
        """
        adapter = MagicMock(spec=AuditLogAdapter)
        adapter.log.side_effect = OSError("central store is down")
        wal = MagicMock(spec=WriteAheadLog)
        wal.recover_orphans.return_value = [_orphan_entry(i) for i in (1, 2, 3)]
        instance = AuditSyncWorker(
            wal=wal,
            central_adapter=adapter,
            config=SyncWorkerConfig(max_retries=2, retry_delay_seconds=0.0),
        )

        result = instance._absorb_orphans_pass()

        assert result == (0, 1)
        assert adapter.log.call_count == 3  # max_retries + 1, for one entry

    def test_pass_keeps_going_after_a_failure_once_something_landed(self, worker):
        """A per-entry failure with deliveries already made is a property of
        the entry, so the remaining orphans are still attempted.
        """
        adapter = MagicMock(spec=AuditLogAdapter)
        adapter.log.side_effect = [None, OSError("bad entry"), None]
        wal = MagicMock(spec=WriteAheadLog)
        wal.recover_orphans.return_value = [_orphan_entry(i) for i in (1, 2, 3)]
        instance = AuditSyncWorker(
            wal=wal,
            central_adapter=adapter,
            config=SyncWorkerConfig(max_retries=0, retry_delay_seconds=0.0),
        )

        absorbed, attempted = instance._absorb_orphans_pass()

        assert (absorbed, attempted) == (2, 3)

    def test_a_pass_with_no_wal_at_all_does_not_consume_the_one_shot(self, worker):
        """Regression (`/verify` Stage 6.7): "no WAL" is *absent*, not "no
        orphans". The WAL check runs before the destination is resolved, so a
        pass that returned a tuple here would consume the one-shot having
        looked at nothing — and a WAL that is disabled, still failing its init,
        or PRO-only on an install that later gains PRO can appear afterwards.
        """
        instance = AuditSyncWorker(
            wal=None, central_adapter=MagicMock(spec=AuditLogAdapter)
        )

        assert instance._absorb_orphans_pass() is None

        instance._absorb_orphans_once()
        assert instance._orphans_absorbed is False

    def test_a_failed_orphan_read_does_not_consume_the_one_shot(self, worker):
        """Regression (`/verify` Stage 6.7): a raising ``recover_orphans()``
        yields the same empty result as a genuinely empty orphan set, and
        reading the failure as "there were none" strands a dead peer's whole
        backlog for the life of this process. One malformed record in a
        crashed peer's torn file is enough to raise here.
        """
        wal = MagicMock(spec=WriteAheadLog)
        wal.recover_orphans.side_effect = TypeError("torn record: seq is a str")
        instance = AuditSyncWorker(
            wal=wal, central_adapter=MagicMock(spec=AuditLogAdapter)
        )

        assert instance._absorb_orphans_pass() is None

        instance._absorb_orphans_once()
        assert instance._orphans_absorbed is False

    def test_a_later_cycle_absorbs_once_the_orphan_read_recovers(self, worker):
        """The other half: the retry the failed read preserved actually lands."""
        wal = MagicMock(spec=WriteAheadLog)
        wal.recover_orphans.side_effect = [OSError("read failed"), [_orphan_entry(1)]]
        instance = AuditSyncWorker(
            wal=wal, central_adapter=MagicMock(spec=AuditLogAdapter)
        )

        instance._absorb_orphans_once()
        assert instance._orphans_absorbed is False
        instance._absorb_orphans_once()

        assert instance._orphans_absorbed is True

    def test_pass_resolves_the_destination_before_reading_any_orphan_file(self, worker):
        """With nothing real to deliver to, the pass costs one registry lookup
        instead of a full read of every orphan file on disk.
        """
        wal = MagicMock(spec=WriteAheadLog)
        instance = AuditSyncWorker(wal=wal, central_adapter=None)

        with patch.object(instance, "_get_adapter", return_value=None):
            result = instance._absorb_orphans_pass()

        assert result is None
        wal.recover_orphans.assert_not_called()


# =============================================================================
# _resolve_central_destination
# =============================================================================


class TestCentralDestinationContract:
    """The four equivalence classes of "is there somewhere to deliver to"."""

    def test_a_real_adapter_is_a_destination(self, worker):
        adapter = MagicMock(spec=AuditLogAdapter)

        with patch.object(worker, "_get_adapter", return_value=adapter):
            assert worker._resolve_central_destination() is adapter

    def test_the_no_op_adapter_is_not_a_destination(self, worker):
        """The provider registry falls back to it, so a booted process always
        gets an object back — and ``NullAuditLogAdapter.log()`` is ``pass``,
        which would report a crashed peer's whole backlog absorbed having
        reached nowhere.
        """
        with patch.object(worker, "_get_adapter", return_value=NullAuditLogAdapter()):
            assert worker._resolve_central_destination() is None

    def test_an_injected_adapter_passes_unchanged(self):
        """Detection is by type, not by the registry's configured default
        name — which is wrong whenever the adapter was resolved by an explicit
        name. Custom wiring and tests must pass through.
        """
        injected = MagicMock(spec=AuditLogAdapter)
        instance = AuditSyncWorker(wal=None, central_adapter=injected)

        assert instance._resolve_central_destination() is injected

    def test_no_adapter_at_all_is_not_a_destination(self, worker):
        with patch.object(worker, "_get_adapter", return_value=None):
            assert worker._resolve_central_destination() is None


# =============================================================================
# Status honesty
# =============================================================================


class TestSyncWorkerStatusHonestyBehavior:
    """``is_running`` is read by the audit health probe."""

    def test_inherited_flag_over_a_dead_thread_reports_not_running(self, worker):
        """The fork-child shape. A flag read would report a healthy pipeline in
        a worker that has no sync thread at all.
        """
        worker._running = True
        worker._thread = _dead_thread()

        assert worker.is_running is False

    def test_flag_without_any_thread_reports_not_running(self, worker):
        worker._running = True
        worker._thread = None

        assert worker.is_running is False

    def test_live_thread_with_the_flag_set_reports_running(self, worker):
        blocker = threading.Event()
        live = threading.Thread(target=blocker.wait, daemon=True)
        live.start()
        worker._running = True
        worker._thread = live
        try:
            assert worker.is_running is True
        finally:
            blocker.set()
            live.join(timeout=5.0)

    def test_a_stopped_worker_with_a_live_thread_reports_not_running(self, worker):
        """Both halves are required: a thread still winding down after
        ``stop()`` is not a running worker.
        """
        blocker = threading.Event()
        live = threading.Thread(target=blocker.wait, daemon=True)
        live.start()
        worker._running = False
        worker._thread = live
        try:
            assert worker.is_running is False
        finally:
            blocker.set()
            live.join(timeout=5.0)

    def test_reading_the_status_never_repairs(self, worker):
        """Negative side effect: a status read must not mutate. Honesty comes
        from composing thread aliveness, not from repairing on the read path.
        """
        worker._origin_pid = FOREIGN_PID
        lock_before = worker._lock

        assert worker.is_running is False

        assert worker._origin_pid == FOREIGN_PID
        assert worker._lock is lock_before


# =============================================================================
# Spawn helper convergence
# =============================================================================


class TestSpawnHelperConvergenceBehavior:
    """Both revival triggers — ``start()`` and the watchdog respawn callback —
    converge on the spawn helper, which is therefore where the fork repair and
    the spawn idempotence live.
    """

    def test_spawn_is_skipped_while_the_thread_is_alive(self, worker):
        worker.start()
        first = worker._thread

        worker._spawn_thread()

        assert worker._thread is first

    def test_spawn_replaces_a_dead_thread(self, worker):
        """Guarded on aliveness, never on ``_running`` — the respawn contract
        forbids consulting the flag.
        """
        dead = _dead_thread()
        worker._thread = dead

        worker._spawn_thread()

        assert worker._thread is not dead
        assert worker._thread.is_alive()

    def test_concurrent_spawns_leave_exactly_one_thread(self, worker):
        """Concurrency: a watchdog respawn racing the starter would otherwise
        leave two loops sharing one cursor.
        """
        spawned: list[threading.Thread] = []
        start_together = threading.Barrier(4)

        def racer():
            start_together.wait(timeout=5.0)
            worker._spawn_thread()
            spawned.append(worker._thread)

        racers = [threading.Thread(target=racer, daemon=True) for _ in range(4)]
        for racer_thread in racers:
            racer_thread.start()
        for racer_thread in racers:
            racer_thread.join(timeout=5.0)

        assert len({id(t) for t in spawned}) == 1
        assert worker._thread.is_alive()

    def test_spawn_rebinds_the_handles_thread_so_a_respawn_converges(self, worker):
        """The registry keeps pointing at the same handle, so the new thread
        has to be published on it or the probe watches a corpse.
        """
        worker.start()
        handle = worker._handle
        worker._thread.join(timeout=0)
        worker._stop_event.set()
        worker._thread.join(timeout=5.0)

        worker._stop_event.clear()
        worker._spawn_thread()

        assert handle.thread is worker._thread
        assert handle.thread.is_alive()

    def test_repaired_child_spawn_starts_a_real_thread(self, worker):
        """Composed: the respawn callback reaches the helper without passing
        any public entry point, so the repair has to fire here too.
        """
        worker._thread = _dead_thread()
        worker._origin_pid = FOREIGN_PID

        worker._spawn_thread()

        assert worker._origin_pid == os.getpid()
        assert worker._thread is not None
        assert worker._thread.is_alive()
        # The loop is running, so the stop Event it waits on must be this
        # process's replacement rather than the inherited one — otherwise
        # setting the replacement would leave the thread parked forever.
        worker._stop_event.set()
        worker._thread.join(timeout=5.0)
        assert worker._thread.is_alive() is False
