"""Behavioural coverage for the WAL's fork revival and its reclamation scope.

Two related contracts, both first reachable once every gunicorn worker runs its
own audit pipeline:

- ``_repair_if_forked()`` — an inherited WAL must stop writing through the
  parent's file description without *flushing* into it, keep its sequence, drop
  an inherited operating-mode latch, and reset the per-process counters.
- **Nobody reclaims a living peer's file.** Three sites can unlink: retention
  (``_cleanup_old_files``), the disk-full purge (``_purge_by_priority``) and
  ``cleanup_processed(mode="startup")``. Retention additionally counts only
  this process's own files, so a worker can no longer meet a directory-wide cap
  by deleting its own undrained records.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Resource release: the parent's file is byte-identical across the repair
- State transition: mode latches cleared, ``CLOSED`` preserved and still raising
- Boundary: the retention cap counted over own-PID files only
- Ordering: own files reclaimed before dead peers', live peers never
- Set membership: orphan liveness partitioning
- Equivalence classes: own / dead-PID / live-PID at the same ``max_seq``

Testbed: a real ``WriteAheadLog`` over ``tmp_path``. Peer files are stamped by
raw on-disk writes — the only way to fabricate another PID's file inside one
test process — and ``pid_alive`` is patched per test so liveness is a property
of the test rather than of the host's PID allocation.
"""

from __future__ import annotations

import json
import os
import struct
import threading
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig, WALError, WALState
from baldur.audit.wal._reader import is_live_peer_wal_file, wal_file_owner_pid

WAL_PREFIX = "fork_wal"
FOREIGN_PID = os.getpid() + 1

# Two fabricated peer PIDs, distinguishable by the liveness stub below.
DEAD_PEER_PID = os.getpid() + 90001
LIVE_PEER_PID = os.getpid() + 90002


@pytest.fixture
def wal_config(tmp_path):
    return WALConfig(
        wal_dir=str(tmp_path),
        sync_on_write=False,
        max_files=10,
        file_prefix=WAL_PREFIX,
    )


@pytest.fixture
def wal(wal_config):
    instance = WriteAheadLog(config=wal_config)
    yield instance
    instance.close()


@pytest.fixture
def wal_dir(wal_config) -> Path:
    return Path(wal_config.wal_dir)


@pytest.fixture
def only_the_live_peer_is_alive():
    """Pin liveness by PID so no test depends on the host's PID allocation.

    ``DEAD_PEER_PID`` and ``LIVE_PEER_PID`` are only *presumed* unused on the
    host; without this stub a CI agent that happens to own one of them would
    flip the whole suite's verdict.
    """
    with patch(
        "baldur.audit.wal._reader.pid_alive",
        side_effect=lambda pid: pid == LIVE_PEER_PID,
    ):
        yield


def _write_raw_wal_file(filepath: Path, entries: list[dict]) -> None:
    """Write a file in the on-disk format the reader understands.

    Bypasses ``WriteAheadLog`` so the filename can carry an arbitrary PID.
    """
    with open(filepath, "wb") as handle:
        handle.write(b"AWAL")
        handle.write(struct.pack(">I", 1))
        for entry in entries:
            data = json.dumps(entry).encode("utf-8")
            checksum = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            handle.write(struct.pack(">I", len(data)))
            handle.write(checksum.encode("ascii"))
            handle.write(data)


def _filename(pid: int, suffix: str = "001") -> str:
    return f"{WAL_PREFIX}_{suffix}_{pid}.wal"


def _purgeable_filename(pid: int) -> str:
    """A filename the priority purge recognises, still owned by ``pid``.

    ``_purge_by_priority`` globs ``{prefix}_{priority}_*.wal``; the owner is
    still the last segment, so one name satisfies both readers.
    """
    return f"{WAL_PREFIX}_debug_001_{pid}.wal"


def _simulate_fork(instance: WriteAheadLog) -> None:
    instance._origin_pid = FOREIGN_PID
    instance._repair_if_forked()


# =============================================================================
# _repair_if_forked
# =============================================================================


class TestWalForkRepairBehavior:
    """The child re-owns the WAL without touching the parent's file."""

    def test_repair_is_a_noop_in_the_constructing_process(self, wal):
        lock_before = wal._lock

        wal._repair_if_forked()

        assert wal._lock is lock_before

    def test_repair_claims_ownership_for_this_process(self, wal):
        _simulate_fork(wal)

        assert wal._origin_pid == os.getpid()

    def test_repair_replaces_the_lock(self, wal):
        """A writer holds this lock on a recurring cadence, so a fork while it
        is held is a real window — and the owner recorded in the inherited
        ``RLock`` is a thread that will never release it.
        """
        lock_before = wal._lock

        _simulate_fork(wal)

        assert wal._lock is not lock_before

    def test_repair_leaves_the_parents_file_byte_identical(self, wal):
        """Resource release, and the reason it is done at the raw layer:
        dropping the last reference to a *buffered* file object runs a
        finalizer that flushes, writing the parent's buffered bytes into the
        parent's file through the inherited file description — the parent then
        writes them again itself.
        """
        wal.write({"event": "buffered-in-the-parent"})
        parent_file = wal._current_file
        assert parent_file is not None
        bytes_before = parent_file.read_bytes()

        _simulate_fork(wal)

        assert parent_file.read_bytes() == bytes_before

    def test_repair_drops_the_inherited_handle(self, wal):
        wal.write({"event": "before-fork"})
        inherited = wal._current_handle
        assert inherited is not None

        _simulate_fork(wal)

        assert wal._current_handle is None
        assert wal._current_file is None
        assert inherited.raw.closed is True

    def test_child_opens_a_fresh_handle_on_a_pid_stamped_file(self, wal):
        """The whole point of the handle drop: the child lazily opens its own
        PID-stamped file, which is what preserves the PID isolation the
        multi-worker drain depends on. (In a real fork the child's PID also
        makes the *name* differ; simulating the fork in one process cannot show
        that, so the assertion is on the stamp and on the handle identity.)
        """
        wal.write({"event": "parent"})
        parent_handle = wal._current_handle

        _simulate_fork(wal)
        wal.write({"event": "child"})

        assert wal._current_file is not None
        assert wal_file_owner_pid(wal._current_file) == os.getpid()
        assert wal._current_handle is not parent_handle

    def test_repair_carries_the_sequence_forward(self, wal):
        """Cursors over this sequence space live on other objects the repair
        cannot reach. Restarting at 0 would put the child's own new entries
        below an inherited cursor, where nothing ever replays them.
        """
        wal.write({"event": "one"})
        wal.write({"event": "two"})
        inherited_sequence = wal._sequence
        assert inherited_sequence == 2

        _simulate_fork(wal)

        assert wal._sequence == inherited_sequence

    def test_first_child_entry_is_strictly_above_an_inherited_cursor(self, wal):
        wal.write({"event": "one"})
        wal.write({"event": "two"})
        inherited_cursor = wal._sequence

        _simulate_fork(wal)
        child_sequence = wal.write({"event": "first-in-the-child"})

        assert child_sequence > inherited_cursor

    def test_repair_zeroes_the_per_process_counters(self, wal):
        """They are this-process statistics published through ``get_stats()``,
        so an inherited value makes a worker that wrote three entries report
        the parent's thousands. The sequence is durability state, not
        statistics, and is excluded above.
        """
        wal.write({"event": "parent"})
        assert wal._total_entries > 0

        _simulate_fork(wal)

        assert wal._total_entries == 0
        assert wal._corrupted_entries == 0
        assert wal._recovered_entries == 0
        assert wal._last_write_time is None

    @pytest.mark.parametrize(
        "inherited_state",
        [WALState.DISK_FULL_FAILOPEN, WALState.ROTATING],
        ids=["disk_full_failopen", "rotating"],
    )
    def test_repair_clears_an_inherited_operating_mode_latch(
        self, wal, inherited_state
    ):
        """A parent that once ran out of disk hands the child a WAL whose every
        write returns ``-1`` for the child's whole life — nothing on the audit
        path calls the recovery check, so nothing would ever clear it.
        """
        wal._state = inherited_state

        _simulate_fork(wal)

        assert wal._state == WALState.ACTIVE

    def test_a_child_of_a_disk_full_parent_can_write(self, wal):
        """The user-visible half of the latch clear."""
        wal._state = WALState.DISK_FULL_FAILOPEN
        assert wal.write({"event": "dropped"}) == -1

        _simulate_fork(wal)

        assert wal.write({"event": "delivered"}) > 0

    def test_repair_preserves_an_inherited_closed_state(self, wal):
        """``CLOSED`` is the owner's explicit intent, not a mode the disk put
        the WAL into: a child that writes gets a loud error rather than a
        silent drop.
        """
        wal.close()
        assert wal._state == WALState.CLOSED

        _simulate_fork(wal)

        assert wal._state == WALState.CLOSED
        with pytest.raises(WALError, match="closed"):
            wal.write({"event": "after-close"})

    def test_repair_discards_an_inherited_group_commit_buffer(self, tmp_path):
        """Those entries belong to the parent's buffer and the parent flushes
        them itself; keeping the copy writes one duplicate per worker.
        """
        instance = WriteAheadLog(
            config=WALConfig(
                wal_dir=str(tmp_path),
                sync_on_write=False,
                group_commit_enabled=True,
                file_prefix=WAL_PREFIX,
            )
        )
        try:
            instance.write({"event": "buffered"})
            assert instance._group_buffer

            _simulate_fork(instance)

            assert instance._group_buffer == []
        finally:
            instance.close()

    def test_second_repair_in_the_same_process_is_a_noop(self, wal):
        _simulate_fork(wal)
        lock_after_first = wal._lock

        wal._repair_if_forked()

        assert wal._lock is lock_after_first

    def test_a_mixin_entry_point_triggers_the_repair(self, wal):
        """``write`` is contributed by a mixin — the surface a ``vars(cls)``
        scan would not see, and the child's most likely first touch.
        """
        wal._origin_pid = FOREIGN_PID

        wal.write({"event": "first-touch"})

        assert wal._origin_pid == os.getpid()


# =============================================================================
# Retention scope
# =============================================================================


class TestWalRetentionScopeBehavior:
    """``max_files`` is a per-process retention budget."""

    def test_retention_keeps_the_cap_over_own_pid_files(self, wal, wal_dir):
        """Boundary: at exactly ``max_files`` nothing is reclaimed; the file
        that pushes the count over it is.
        """
        wal._config.max_files = 2
        own = [wal_dir / _filename(os.getpid(), f"{i:03d}") for i in range(1, 4)]
        for path in own:
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal._cleanup_old_files()

        assert [p.exists() for p in own] == [False, True, True]

    def test_retention_ignores_peer_files_when_counting(self, wal, wal_dir):
        """The lossy shape this replaces: with one file per worker the
        directory passes a directory-wide cap with nobody misbehaving, and each
        rotating worker then deletes its **own** oldest file — whose entries
        the size-triggered rotation gives the drain no chance to deliver.
        """
        wal._config.max_files = 2
        own = [wal_dir / _filename(os.getpid(), f"{i:03d}") for i in (1, 2)]
        peers = [
            wal_dir / _filename(DEAD_PEER_PID, "001"),
            wal_dir / _filename(LIVE_PEER_PID, "002"),
        ]
        for path in own + peers:
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal._cleanup_old_files()

        assert all(p.exists() for p in own)
        assert all(p.exists() for p in peers)

    def test_retention_never_unlinks_a_peer_file_even_over_the_cap(self, wal, wal_dir):
        """Negative side effect: the own-PID glob is what makes a peer file
        unreachable by this site, not an ordering accident.
        """
        wal._config.max_files = 1
        own = [wal_dir / _filename(os.getpid(), f"{i:03d}") for i in (1, 2, 3)]
        peer = wal_dir / _filename(DEAD_PEER_PID, "000")
        for path in [*own, peer]:
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal._cleanup_old_files()

        assert peer.exists()
        assert [p.exists() for p in own] == [False, False, True]


# =============================================================================
# Disk-full reclamation order
# =============================================================================


class TestWalReclamationOrderBehavior:
    """The purge may free space, but never a living peer's."""

    def test_live_peer_files_are_dropped_from_the_candidate_list(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Freeing space by unlinking a live peer's file sends the owner's
        subsequent writes to an unlinked inode.
        """
        live = wal_dir / _filename(LIVE_PEER_PID, "001")
        dead = wal_dir / _filename(DEAD_PEER_PID, "002")
        for path in (live, dead):
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        ordered = wal._reclaimable_in_order([live, dead])

        assert ordered == [dead]

    def test_own_files_are_reclaimed_before_a_dead_peers(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Dead-PID files are precisely the un-absorbed orphan backlog:
        deleting them ahead of this process's own rotated history would discard
        a crashed peer's undelivered entries first.
        """
        dead = wal_dir / _filename(DEAD_PEER_PID, "001")
        own = wal_dir / _filename(os.getpid(), "002")
        for path in (dead, own):
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        ordered = wal._reclaimable_in_order([dead, own])

        assert ordered == [own, dead]

    def test_pid_less_files_are_ordered_with_the_dead(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """A filename with no parseable PID cannot be proven to have a living
        owner, so it is reclaimable — but only after this process's own.
        """
        nameless = wal_dir / f"{WAL_PREFIX}_legacy_format.wal"
        own = wal_dir / _filename(os.getpid(), "002")
        for path in (nameless, own):
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        ordered = wal._reclaimable_in_order([nameless, own])

        assert ordered == [own, nameless]

    def test_purge_frees_space_without_latching_fail_open(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Negative: with a reclaimable own file present the purge succeeds, so
        the disk-full handler returns before latching ``DISK_FULL_FAILOPEN`` —
        the mode that makes every subsequent write return ``-1``.
        """
        wal._config.max_file_size_mb = 1
        own = wal_dir / _purgeable_filename(os.getpid())
        own.write_bytes(b"\0" * (2 * 1024 * 1024))

        wal._handle_disk_full()

        assert wal._state == WALState.ACTIVE
        assert not own.exists()

    def test_the_currently_open_file_is_never_a_purge_candidate(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Regression (`/verify` Stage 6.7): the open file is the one file
        guaranteed to have an active writer — this process. Unlinking it is the
        live-peer mistake committed against ourselves: the handle stays open,
        every later record goes to an unlinked inode, and no drain reads them.
        Ordering by owner put it *ahead* of a dead peer's orphan file, where
        the previous oldest-first ordering had put it last.
        """
        wal.write({"event": "open-and-undrained"})
        current = wal._current_file
        dead = wal_dir / _filename(DEAD_PEER_PID, "000")
        _write_raw_wal_file(dead, [{"seq": 1, "ts": 1.0, "data": {}}])

        ordered = wal._reclaimable_in_order([current, dead])

        assert current not in ordered
        assert ordered == [dead]

    def test_retention_floor_counts_files_this_process_may_not_reclaim(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Regression (`/verify` Stage 6.7): ``critical_retention_min_mb`` asks
        how much WAL data is left **on disk**, so it counts every file in the
        directory. Measuring it over the reclaimable subset alone lets a live
        peer's untouchable file shrink the denominator until this process
        refuses to free its own rotated history — and then latches Fail-Open,
        in exactly the multi-worker topology per-process WAL files create.
        """
        wal._config.max_file_size_mb = 1
        wal._config.critical_retention_min_mb = 3
        own = wal_dir / _filename(os.getpid(), "001")
        own.write_bytes(b"\0" * (2 * 1024 * 1024))
        live_peer = wal_dir / _filename(LIVE_PEER_PID, "002")
        live_peer.write_bytes(b"\0" * (4 * 1024 * 1024))

        wal._handle_disk_full()

        # 6 MB on disk, floor 3 MB: freeing the own 2 MB file is allowed.
        assert not own.exists()
        assert live_peer.exists()
        assert wal._state == WALState.ACTIVE

    def test_purge_latches_fail_open_rather_than_unlink_a_live_peer(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """The other half: a purge that may reclaim nothing must fall through
        to Fail-Open rather than free the space it can see. The peer's file is
        big enough to satisfy the target on its own, so only the liveness guard
        stands between it and deletion.
        """
        wal._config.max_file_size_mb = 1
        live = wal_dir / _purgeable_filename(LIVE_PEER_PID)
        live.write_bytes(b"\0" * (2 * 1024 * 1024))

        wal._handle_disk_full()

        assert wal._state == WALState.DISK_FULL_FAILOPEN
        assert live.exists()


# =============================================================================
# Orphan liveness filter
# =============================================================================


class TestOrphanLivenessFilterBehavior:
    """Orphan-ness is liveness-scoped, not merely "not my PID"."""

    def test_owner_pid_is_the_last_filename_segment(self, tmp_path):
        """Both the prefix and the timestamp contain underscores, so the owner
        is the last underscore-separated segment of the stem.
        """
        assert wal_file_owner_pid(tmp_path / "audit_wal_20260810_120000_431.wal") == 431

    @pytest.mark.parametrize(
        "name",
        ["audit_wal.wal", "audit_wal_notapid.wal"],
        ids=["no_separator", "non_numeric"],
    )
    def test_unparseable_filenames_have_no_owner(self, tmp_path, name):
        assert wal_file_owner_pid(tmp_path / name) is None

    def test_own_file_is_not_a_live_peer(self, tmp_path, only_the_live_peer_is_alive):
        """ "Peer" means *another* process — this process's own file is handled
        by the caller, not excluded as somebody else's.
        """
        assert is_live_peer_wal_file(tmp_path / _filename(os.getpid())) is False

    def test_pid_less_file_is_not_a_live_peer(
        self, tmp_path, only_the_live_peer_is_alive
    ):
        assert is_live_peer_wal_file(tmp_path / "audit_wal.wal") is False

    @pytest.mark.parametrize(
        ("pid", "expected"),
        [(LIVE_PEER_PID, True), (DEAD_PEER_PID, False)],
        ids=["live_peer", "dead_peer"],
    )
    def test_peer_liveness_follows_the_probe(
        self, tmp_path, only_the_live_peer_is_alive, pid, expected
    ):
        assert is_live_peer_wal_file(tmp_path / _filename(pid)) is expected

    def test_orphan_set_is_dead_peers_only(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Set membership: own excluded (this worker drains them itself), live
        peer excluded (its own worker is delivering them), dead peer included.

        Without the liveness filter a forked child reads its *living* parent's
        file and re-delivers every entry the parent already delivered — the
        consumer's idempotency guard is a TTL cache, not a durable
        acknowledgement.
        """
        own = wal_dir / _filename(os.getpid(), "001")
        live = wal_dir / _filename(LIVE_PEER_PID, "002")
        dead = wal_dir / _filename(DEAD_PEER_PID, "003")
        for path in (own, live, dead):
            _write_raw_wal_file(path, [{"seq": 1, "ts": 1.0, "data": {}}])

        orphans = wal._orphan_wal_files(WAL_PREFIX)

        assert orphans == [dead]

    def test_recover_orphans_skips_a_live_peers_entries(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        live = wal_dir / _filename(LIVE_PEER_PID, "001")
        dead = wal_dir / _filename(DEAD_PEER_PID, "002")
        _write_raw_wal_file(live, [{"seq": 7, "ts": 7.0, "data": {"e": "live"}}])
        _write_raw_wal_file(dead, [{"seq": 8, "ts": 8.0, "data": {"e": "dead"}}])

        entries = wal.recover_orphans()

        assert [e.sequence for e in entries] == [8]
        assert {e.data.get("e") for e in entries} == {"dead"}


# =============================================================================
# Startup cleanup
# =============================================================================


class TestStartupCleanupLivePeerGuard:
    """``cleanup_processed(mode="startup")`` globs every PID, so the liveness
    guard is what keeps it off a living peer's file.
    """

    @pytest.fixture
    def three_owners(self, wal_dir):
        """One file per owner class, all at the same ``max_seq``.

        Equal sequences are the point: ``last_processed_seq`` counts *this*
        worker's sequence space, so a peer's low ``max_seq`` is not evidence
        that its entries were delivered.
        """
        paths = {
            "own": wal_dir / _filename(os.getpid(), "001"),
            "dead": wal_dir / _filename(DEAD_PEER_PID, "002"),
            "live": wal_dir / _filename(LIVE_PEER_PID, "003"),
        }
        for path in paths.values():
            _write_raw_wal_file(path, [{"seq": 5, "ts": 5.0, "data": {}}])
        return paths

    def test_startup_cleanup_reclaims_own_and_dead_but_never_live(
        self, wal, three_owners, only_the_live_peer_is_alive
    ):
        deleted = wal.cleanup_processed(last_processed_seq=5, mode="startup")

        assert three_owners["own"].exists() is False
        assert three_owners["dead"].exists() is False
        assert three_owners["live"].exists() is True
        assert deleted == 2

    def test_runtime_mode_leaves_both_peers_out_of_scope(
        self, wal, three_owners, only_the_live_peer_is_alive
    ):
        """``mode="runtime"`` narrows the glob to this PID, so the guard is not
        even consulted — the two modes must not converge by accident.
        """
        deleted = wal.cleanup_processed(last_processed_seq=5, mode="runtime")

        assert three_owners["own"].exists() is False
        assert three_owners["dead"].exists() is True
        assert three_owners["live"].exists() is True
        assert deleted == 1

    def test_live_peer_survives_even_far_below_the_cursor(
        self, wal, wal_dir, only_the_live_peer_is_alive
    ):
        """Boundary in the wrong direction on purpose: the peer's whole file
        sits below the cursor, which is exactly when the pre-guard glob would
        have unlinked it while its owner kept appending.
        """
        live = wal_dir / _filename(LIVE_PEER_PID, "001")
        _write_raw_wal_file(live, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal.cleanup_processed(last_processed_seq=10_000, mode="startup")

        assert live.exists()


# =============================================================================
# Repair under concurrency
# =============================================================================


class TestWalForkRepairConcurrency:
    """Only one thread may perform the repair; the others see it finished."""

    def test_concurrent_entry_points_repair_exactly_once(self, wal):
        """The gate is entered only after an unlocked mismatch pre-check, and
        the mismatch is re-checked inside so a second thread returns instead of
        repairing twice — a second repair would replace the lock a first
        repaired writer is already holding.
        """
        wal._origin_pid = FOREIGN_PID
        locks_seen: list[object] = []
        start_together = threading.Barrier(6)

        def racer():
            start_together.wait(timeout=5.0)
            wal._repair_if_forked()
            locks_seen.append(wal._lock)

        racers = [threading.Thread(target=racer, daemon=True) for _ in range(6)]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join(timeout=5.0)

        assert len({id(lock) for lock in locks_seen}) == 1
        assert wal._origin_pid == os.getpid()
