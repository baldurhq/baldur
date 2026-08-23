"""Integration tests for the bounded audit drain cycle (#763 D3).

Composition, not delegation: ``AuditSyncWorker._sync_batch`` and
``WriteAheadLog`` share one piece of state across a transaction boundary.
``_last_processed_seq`` gates the bounded read, the delivery loop advances it
over the contiguous leading run of successes, and ``_post_sync_cleanup`` ->
``cleanup_processed`` then makes *deletion* decisions from the same cursor with
a **different** reader — ``_get_file_max_sequence``, which skips the checksum
entirely. The corrupted-checksum guard is precisely a claim about those two
readers disagreeing, so it cannot be asserted inside either unit. The lag's
lifecycle (backlog -> partially drained -> idle) is likewise a multi-cycle
property, not a single-call one.

Test categories:
    A. Multi-cycle drain: every entry delivered once, ascending, cursor and lag
       tracking the backlog down to zero.
    B. Cursor vs deletion: a file is unlinked only once every entry it holds is
       below the cursor — including when the read had to skip a corrupt record
       the deletion predicate cannot even see.
    C. Unwired destination: nothing advances, nothing is deleted, and the
       per-cycle read stays at the budget while the backlog grows.

Infrastructure: none. A real ``WriteAheadLog`` over ``tmp_path`` plus an
in-memory capturing adapter — no Redis, database or Celery — so this runs
under ``pytest-xdist`` like the rest of the mock-based integration tree.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from tests.factories.audit_adapters import CapturingAuditAdapter
from tests.factories.wal_records import RawRecord, own_pid_wal_name, write_raw_wal_file

WAL_PREFIX = "drain_cycle_wal"

BATCH_SIZE = 100

# A checksum field carrying a byte outside ASCII: the strict reader ends the
# file silently here, while ``_get_file_max_sequence`` never reads the field at
# all and still sees every sequence behind it.
UNDECODABLE_CHECKSUM = b"\xffeadbeef"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def wal_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def wal(wal_dir: Path):
    """A real WAL over an empty directory."""
    config = WALConfig(
        wal_dir=str(wal_dir),
        sync_on_write=False,
        max_files=1000,
        file_prefix=WAL_PREFIX,
    )
    instance = WriteAheadLog(config=config)
    yield instance
    instance.close()


@pytest.fixture
def adapter() -> CapturingAuditAdapter:
    return CapturingAuditAdapter()


@pytest.fixture
def worker(wal, adapter) -> AuditSyncWorker:
    """A directly-constructed worker, so no test pollutes the singleton."""
    return AuditSyncWorker(
        wal=wal,
        central_adapter=adapter,
        config=SyncWorkerConfig(batch_size=BATCH_SIZE),
    )


def _fill(wal: WriteAheadLog, count: int) -> None:
    for i in range(count):
        wal.write({"action": "TEST_EVENT", "details": {"n": i}})
    wal.flush()


def _fill_into(wal: WriteAheadLog, count: int, stamp: str) -> Path:
    """Write ``count`` entries into a file named by ``stamp``, and return it.

    ``_get_current_wal_filename`` stamps the clock to the second, so two
    rotations inside the same second reopen the same path in append mode and a
    test that needs two distinct files never gets them.
    """
    with patch.object(
        wal,
        "_get_current_wal_filename",
        return_value=own_pid_wal_name(WAL_PREFIX, stamp),
    ):
        _fill(wal, count)
    return wal._current_file


def _wal_files(wal_dir: Path) -> list[Path]:
    return sorted(wal_dir.glob(f"{WAL_PREFIX}_*.wal"))


def _count_consumed(wal: WriteAheadLog, consumed: list[int]):
    """Record every record the WAL actually pulls off disk — the bound under
    test is on the read, which the return value alone cannot show.
    """
    original = wal._read_wal_file_best_effort

    def counting(filepath):
        for entry in original(filepath):
            consumed.append(entry.sequence)
            yield entry

    return patch.object(wal, "_read_wal_file_best_effort", counting)


# =============================================================================
# A. Multi-cycle drain
# =============================================================================


class TestBoundedDrainDeliversTheWholeBacklog:
    """A budgeted read must not turn into a budgeted *delivery*: the backlog
    still drains completely, one budget per cycle.
    """

    def test_every_entry_is_delivered_exactly_once_in_ascending_order(
        self, wal, worker, adapter
    ):
        backlog = BATCH_SIZE * 2 + BATCH_SIZE // 2
        _fill(wal, backlog)

        cycles = 0
        while worker._sync_batch()[0] > 0:
            cycles += 1

        assert cycles == 3, "two full budgets and a partial one"
        assert len(adapter.entries) == backlog
        delivered = [e.details["n"] for e in adapter.entries]
        assert delivered == list(range(backlog)), "ascending, no gaps, no repeats"
        assert worker._last_processed_seq == backlog

    def test_lag_falls_to_zero_as_the_backlog_drains(self, wal, worker):
        backlog = BATCH_SIZE * 2
        _fill(wal, backlog)

        observed = []
        for _ in range(3):
            worker._sync_batch()
            observed.append(worker.get_stats()["current_lag_entries"])

        assert observed == [backlog, backlog - BATCH_SIZE, 0]

    def test_entries_written_mid_drain_are_picked_up_by_a_later_cycle(
        self, wal, worker, adapter
    ):
        """The bounded read draws a fresh window each cycle, so it must not
        pin itself to the backlog it saw first.
        """
        _fill(wal, BATCH_SIZE)
        worker._sync_batch()
        assert len(adapter.entries) == BATCH_SIZE

        _fill(wal, 10)
        synced, failed = worker._sync_batch()

        assert (synced, failed) == (10, 0)
        assert worker._last_processed_seq == BATCH_SIZE + 10


# =============================================================================
# B. Cursor vs deletion — the two readers
# =============================================================================


class TestDrainCursorGovernsDeletion:
    """``cleanup_processed`` decides with a reader that skips the checksum, so
    a file must survive until the cursor is past everything it holds.
    """

    def test_a_partially_drained_file_survives_its_cycle(self, wal, wal_dir, worker):
        # Given: one retired file holding more than a budget, and a live one.
        retired = _fill_into(wal, BATCH_SIZE + 50, "20260101_000000")
        wal._rotate_file()
        current = _fill_into(wal, 50, "20260101_000001")
        assert retired != current, "precondition: two distinct files on disk"

        # When: one cycle drains the first budget only.
        worker._sync_batch()

        # Then: the retired file still holds undrained entries, so it stays.
        assert worker._last_processed_seq == BATCH_SIZE
        assert retired.exists()
        assert current.exists()

    def test_a_fully_drained_file_is_unlinked_and_the_live_one_is_not(
        self, wal, wal_dir, worker
    ):
        retired = _fill_into(wal, BATCH_SIZE + 50, "20260101_000000")
        wal._rotate_file()
        current = _fill_into(wal, 50, "20260101_000001")

        worker._sync_batch()
        worker._sync_batch()

        assert worker._last_processed_seq == BATCH_SIZE + 100
        assert not retired.exists(), "every entry it held is below the cursor"
        assert current.exists(), "the file being appended to is never unlinked"

    def test_a_corrupt_record_does_not_let_the_cursor_or_the_cleanup_skip_a_file(
        self, wal, wal_dir, adapter
    ):
        """The C8 guard. The drain's reader skips a record whose checksum field
        is not even decodable; the deletion predicate never reads that field
        and reports the file's true maximum sequence. If the cursor jumped to a
        later file's entries, the deletion predicate would agree the earlier
        file was fully processed and unlink entries that never reached the
        destination.
        """
        # Given: an earlier file with a corrupt record in the middle, and a
        # later file behind it. Both are stamped after the WAL is constructed.
        earlier = write_raw_wal_file(
            wal_dir / own_pid_wal_name(WAL_PREFIX, "20260101_000000"),
            [
                RawRecord(sequence=1),
                RawRecord(sequence=2),
                RawRecord(sequence=3, checksum=UNDECODABLE_CHECKSUM),
                RawRecord(sequence=4),
                RawRecord(sequence=5),
                RawRecord(sequence=6),
            ],
        )
        later = write_raw_wal_file(
            wal_dir / own_pid_wal_name(WAL_PREFIX, "20260101_000001"),
            [RawRecord(sequence=seq) for seq in range(7, 11)],
        )
        worker = AuditSyncWorker(
            wal=wal, central_adapter=adapter, config=SyncWorkerConfig(batch_size=3)
        )

        # When: the first cycle reads across the corruption.
        worker._sync_batch()

        # Then: the cursor stopped inside the earlier file, and the file that
        # still holds sequences 5 and 6 was not unlinked.
        assert worker._last_processed_seq == 4
        assert earlier.exists(), (
            "the deletion predicate sees max_seq=6 > cursor=4 and must keep it"
        )
        assert later.exists()

        # And: the remaining cycles drain both files and only then reclaim them.
        while worker._sync_batch()[0] > 0:
            pass

        delivered = [e.details["n"] for e in adapter.entries]
        assert delivered == [1, 2, 4, 5, 6, 7, 8, 9, 10], (
            "only the unreadable record is lost — nothing behind it"
        )
        assert not earlier.exists()
        assert not later.exists()


# =============================================================================
# C. Unwired destination
# =============================================================================


class TestUnwiredDrainStaysBounded:
    """With no real destination the cursor is frozen by design, so the read is
    the only thing that can be bounded — and it must be.
    """

    def test_per_cycle_read_stays_at_the_budget_while_the_backlog_grows(
        self, wal, worker, adapter
    ):
        """The measured defect: with the cursor frozen, each cycle re-read the
        whole retained backlog to use the first hundred entries of it, so
        per-cycle work grew linearly and cumulative cost quadratically.
        """
        _fill(wal, BATCH_SIZE * 3)
        per_cycle: list[int] = []

        with patch.object(worker, "_get_adapter", return_value=None):
            for _ in range(5):
                consumed: list[int] = []
                with _count_consumed(wal, consumed):
                    worker._sync_batch()
                per_cycle.append(len(consumed))
                _fill(wal, 20)  # the backlog keeps growing underneath

        assert per_cycle == [BATCH_SIZE] * 5, (
            "the read is the budget every cycle, not the backlog"
        )

    def test_nothing_is_delivered_advanced_or_deleted_while_unwired(
        self, wal, wal_dir, worker, adapter
    ):
        _fill(wal, BATCH_SIZE + 50)
        files_before = _wal_files(wal_dir)
        assert files_before, "precondition: the backlog is on disk"

        with patch.object(worker, "_get_adapter", return_value=None):
            for _ in range(3):
                assert worker._sync_batch() == (0, 0)

        assert adapter.entries == []
        assert worker._last_processed_seq == 0
        assert _wal_files(wal_dir) == files_before
        assert worker.get_stats()["current_lag_entries"] == BATCH_SIZE + 50

    def test_a_wired_adapter_afterwards_drains_the_retained_backlog(
        self, wal, worker, adapter
    ):
        """Retention has to be worth something: the entries held back while
        unwired are delivered once a destination appears.
        """
        backlog = BATCH_SIZE + 50
        _fill(wal, backlog)

        with patch.object(worker, "_get_adapter", return_value=None):
            worker._sync_batch()
        assert adapter.entries == []

        while worker._sync_batch()[0] > 0:
            pass

        assert len(adapter.entries) == backlog
        assert worker._last_processed_seq == backlog
