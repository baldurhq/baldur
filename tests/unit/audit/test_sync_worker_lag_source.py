"""Unit tests for the audit drain's lag source and bounded read (#763 D3).

The drain's per-cycle read is now capped at ``batch_size``, so the number of
entries a cycle read can no longer stand in for the backlog: derived from the
read, the lag would top out at the budget — far below the audit health probe's
DEGRADED threshold, making that verdict unreachable. ``_count_pending`` answers
from the WAL's own sequence instead, and the assignment happens above the
empty-read early return so an idle cycle still reports the current backlog
rather than leaving the previous one standing.

Verification techniques per UNIT_TEST_GUIDELINES §8:
- Dependency interaction: ``count_unprocessed`` is preferred over a read, and
  the read carries ``limit=batch_size``
- Exception & edge case: a WAL-like object without the method, and one that
  raises
- State transition: backlog -> partially drained -> idle, where the idle cycle
  must overwrite the previous number
- Side effects: the unwired WARNING carries the real backlog

Testbed: a real ``WriteAheadLog`` over ``tmp_path`` plus a capturing adapter.
Entries are written through ``wal.write()`` rather than stamped on disk — the
lag reads the WAL's in-memory sequence, which a fabricated file does not move.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from structlog.testing import capture_logs

from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from tests.factories.audit_adapters import CapturingAuditAdapter
from tests.factories.writable_dir import log_events

WAL_PREFIX = "lag_source_wal"

BATCH_SIZE = 100

# Deep enough that a lag capped at the read budget is unmistakable.
DEEP_BACKLOG = 1500


@pytest.fixture
def wal(tmp_path: Path):
    config = WALConfig(
        wal_dir=str(tmp_path),
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


def _make_worker(wal_instance, central_adapter=None) -> AuditSyncWorker:
    """A directly-constructed (non-singleton) worker, so no test pollutes
    ``AuditSyncWorker._instance``. The batch size is pinned rather than read
    from settings so the arithmetic below does not depend on the environment.
    """
    return AuditSyncWorker(
        wal=wal_instance,
        central_adapter=central_adapter or CapturingAuditAdapter(),
        config=SyncWorkerConfig(batch_size=BATCH_SIZE),
    )


def _fill(wal_instance: WriteAheadLog, count: int) -> None:
    for i in range(count):
        wal_instance.write({"action": "TEST_EVENT", "details": {"n": i}})
    wal_instance.flush()


class TestSyncWorkerLagSourceBehavior:
    """The lag is the backlog above the cursor, not what one cycle read."""

    def test_count_pending_prefers_count_unprocessed_over_a_read(self, wal):
        """The WAL answers from its in-memory sequence with no file reads —
        the substitution the async lifecycle check already documents as
        preferred.
        """
        wal_double = MagicMock(spec=WriteAheadLog)
        wal_double.count_unprocessed.return_value = 42
        worker = _make_worker(wal)
        worker._last_processed_seq = 7

        assert worker._count_pending(wal_double) == 42
        wal_double.count_unprocessed.assert_called_once_with(7)
        wal_double.recover_unprocessed.assert_not_called()

    def test_count_pending_falls_back_to_a_read_for_a_wal_without_the_method(self, wal):
        """A host-injected WAL-like object need not implement the fast path."""
        wal_double = Mock(spec=["recover_unprocessed"])
        wal_double.recover_unprocessed.return_value = [object(), object(), object()]
        worker = _make_worker(wal)
        worker._last_processed_seq = 5

        assert worker._count_pending(wal_double) == 3
        wal_double.recover_unprocessed.assert_called_once_with(5, mode="runtime")

    def test_count_pending_reports_zero_when_the_wal_raises(self, wal):
        """An unreadable WAL must not take the drain cycle down with it."""
        wal_double = MagicMock(spec=WriteAheadLog)
        wal_double.count_unprocessed.side_effect = OSError("disk gone")
        worker = _make_worker(wal)

        with capture_logs() as logs:
            pending = worker._count_pending(wal_double)

        assert pending == 0
        assert log_events(logs, "audit_sync_worker.pending_count_failed")

    def test_lag_reports_the_whole_backlog_while_the_cycle_reads_the_budget(
        self, wal, adapter
    ):
        """The headline: a 1500-entry backlog drained 100 at a time reports
        1500, not 100. Derived from the read, this number could never exceed
        the budget and the DEGRADED health verdict would be unreachable.
        """
        # Given
        _fill(wal, DEEP_BACKLOG)
        worker = _make_worker(wal, central_adapter=adapter)

        # When
        with patch.object(
            wal, "recover_unprocessed", wraps=wal.recover_unprocessed
        ) as spy:
            synced, failed = worker._sync_batch()

        # Then: the cycle read and delivered exactly one budget...
        assert (synced, failed) == (BATCH_SIZE, 0)
        assert len(adapter.entries) == BATCH_SIZE
        assert worker._last_processed_seq == BATCH_SIZE
        spy.assert_called_once_with(0, mode="runtime", limit=BATCH_SIZE)

        # ...while the reported lag is the backlog that still exists.
        assert worker.get_stats()["current_lag_entries"] == DEEP_BACKLOG

    def test_an_idle_cycle_overwrites_the_previous_backlog_number(self, wal, adapter):
        """Assignment position: the lag is written above the empty-read early
        return, because a cycle that reads nothing is the steady state of a
        wired, idle process — and it must not leave a stale backlog standing.
        """
        # Given: a backlog of one and a half budgets.
        backlog = BATCH_SIZE + BATCH_SIZE // 2
        _fill(wal, backlog)
        worker = _make_worker(wal, central_adapter=adapter)

        # When / Then: backlog -> partially drained -> idle.
        worker._sync_batch()
        assert worker.get_stats()["current_lag_entries"] == backlog

        worker._sync_batch()
        assert worker.get_stats()["current_lag_entries"] == backlog - BATCH_SIZE

        synced, failed = worker._sync_batch()
        assert (synced, failed) == (0, 0), "precondition: this cycle read nothing"
        assert worker.get_stats()["current_lag_entries"] == 0

    def test_unwired_warning_names_the_real_backlog_not_the_batch_budget(
        self, wal, adapter
    ):
        """With no real destination the cursor is deliberately frozen, so the
        WARNING is the only place an operator sees how deep the retained
        backlog is.
        """
        backlog = BATCH_SIZE + BATCH_SIZE // 2
        _fill(wal, backlog)
        worker = _make_worker(wal, central_adapter=adapter)

        with (
            patch.object(worker, "_get_adapter", return_value=None),
            capture_logs() as logs,
        ):
            synced, failed = worker._sync_batch()

        assert (synced, failed) == (0, 0)
        assert adapter.entries == [], "nothing was delivered anywhere"
        assert worker._last_processed_seq == 0, "the cursor stays frozen"

        unwired = log_events(logs, "audit_sync_worker.central_adapter_unwired")
        assert len(unwired) == 1
        assert unwired[0]["log_level"] == "warning"
        assert unwired[0]["pending_entries"] == backlog

    def test_lag_is_zero_when_there_is_no_wal_at_all(self, wal):
        """Edge case: ``_sync_batch`` returns before the lag assignment, so a
        worker with no WAL reports nothing rather than a stale number.
        """
        worker = _make_worker(wal)
        worker._stats.current_lag_entries = 99

        with patch.object(worker, "_get_wal", return_value=None):
            assert worker._sync_batch() == (0, 0)

        assert worker.get_stats()["current_lag_entries"] == 99, (
            "a worker that cannot read its WAL leaves the gauge untouched "
            "rather than reporting a fabricated zero"
        )
