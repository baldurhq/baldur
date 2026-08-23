"""The drain flushes the writer's group-commit buffer before reading.

With group commit enabled the writer returns a sequence for an entry that so
far lives only in process memory. "The write returned a sequence" therefore
does not imply "a reader can find it" — and the sync worker reads the WAL file
through its own handle. Without an explicit flush the entry stays invisible
until some unrelated later write happens to trip the flush condition, which in
a quiet process can be a long time; the event the operator is waiting for sits
in RAM while the drain reports an empty cycle.

This matters more since the single-deliverer rule: the drain is now the *only*
thing that delivers a WAL-backed event, so an entry it cannot see is an entry
nobody delivers until the next write.

The flush is a no-op when group commit is disabled, and a WAL-like object that
cannot flush is left alone rather than breaking the cycle.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.audit.sync_worker import AuditSyncWorker, SyncWorkerConfig
from baldur.audit.wal import WriteAheadLog
from baldur.audit.wal._models import WALConfig
from baldur.interfaces.audit_adapter import AuditEntry
from baldur.services.idempotency import IdempotencyService

# Far above any single test's runtime, so one write can never trip the
# time-based flush condition and mask the explicit flush under test.
NEVER_ELAPSES_MS = 600_000

# Far above the number of entries any test writes, so the size-based flush
# condition cannot trip either.
NEVER_FILLS = 1_000


class RecordingAdapter:
    """The central destination — remembers the entries it was handed."""

    def __init__(self):
        self.entries: list[AuditEntry] = []

    def log(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class WALWithoutFlush:
    """A WAL-like object that has no group-commit buffer to flush."""

    def __init__(self):
        self.reads = 0

    def recover_unprocessed(self, last_seq, mode="runtime", limit=None):
        self.reads += 1
        return []


@pytest.fixture
def fresh_idempotency_cache():
    """Per-test dedup cache so the worker's real dedup gate cannot leak."""
    cache = InMemoryCacheAdapter(key_prefix="test_sync_worker_group_commit:")
    with patch.object(IdempotencyService, "_get_cache", return_value=cache):
        yield cache


def _buffering_wal(tmp_path) -> WriteAheadLog:
    """A real WAL whose group-commit buffer never self-flushes on its own."""
    return WriteAheadLog(
        config=WALConfig(
            wal_dir=str(tmp_path),
            file_prefix="sw_group_commit",
            sync_on_write=True,
            group_commit_enabled=True,
            group_commit_max_entries=NEVER_FILLS,
            group_commit_max_wait_ms=NEVER_ELAPSES_MS,
        )
    )


def _direct_wal(tmp_path) -> WriteAheadLog:
    """A real WAL with group commit off — every write lands immediately."""
    return WriteAheadLog(
        config=WALConfig(
            wal_dir=str(tmp_path),
            file_prefix="sw_direct",
            sync_on_write=True,
            group_commit_enabled=False,
        )
    )


def _worker(wal, adapter) -> AuditSyncWorker:
    return AuditSyncWorker(
        wal=wal, central_adapter=adapter, config=SyncWorkerConfig(max_retries=0)
    )


class TestGroupCommitFlushedBeforeRead:
    """One cycle sees an entry the writer only buffered."""

    def test_a_buffered_entry_is_delivered_in_the_same_cycle(
        self, tmp_path, fresh_idempotency_cache
    ):
        """The property: write, then drain, and the row is delivered — no
        second unrelated write needed to shake it loose."""
        wal = _buffering_wal(tmp_path)
        adapter = RecordingAdapter()
        try:
            wal.write({"event_type": "CB_STATE_CHANGE", "record_id": "audit-1"})

            synced, failed = _worker(wal, adapter)._sync_batch()
        finally:
            wal.close()

        assert (synced, failed) == (1, 0)
        assert adapter.entries[0].details["record_id"] == "audit-1"

    def test_without_the_flush_the_entry_is_invisible_to_a_reader(self, tmp_path):
        """What the flush is for, stated directly: the write returned a
        sequence, yet a reader over the same WAL finds nothing until the
        buffer is forced out. Without this the test above could pass for a
        writer that never buffered at all."""
        wal = _buffering_wal(tmp_path)
        try:
            seq = wal.write({"event_type": "CB_STATE_CHANGE", "record_id": "audit-1"})
            before = wal.recover_unprocessed(0, mode="runtime")

            wal.flush_group_commit()
            after = wal.recover_unprocessed(0, mode="runtime")
        finally:
            wal.close()

        assert seq >= 0, "the writer reported a sequence for the buffered entry"
        assert before == []
        assert len(after) == 1

    def test_every_buffered_entry_of_the_batch_is_delivered(
        self, tmp_path, fresh_idempotency_cache
    ):
        """The flush drains the whole buffer, not just its head."""
        wal = _buffering_wal(tmp_path)
        adapter = RecordingAdapter()
        try:
            for index in range(3):
                wal.write(
                    {"event_type": "CB_STATE_CHANGE", "record_id": f"audit-{index}"}
                )

            synced, _ = _worker(wal, adapter)._sync_batch()
        finally:
            wal.close()

        assert synced == 3
        assert [entry.details["record_id"] for entry in adapter.entries] == [
            "audit-0",
            "audit-1",
            "audit-2",
        ]

    def test_group_commit_disabled_still_delivers(
        self, tmp_path, fresh_idempotency_cache
    ):
        """The no-op half: with group commit off the flush must change
        nothing, and the ordinary drain still works."""
        wal = _direct_wal(tmp_path)
        adapter = RecordingAdapter()
        try:
            wal.write({"event_type": "CB_STATE_CHANGE", "record_id": "audit-1"})

            synced, failed = _worker(wal, adapter)._sync_batch()
        finally:
            wal.close()

        assert (synced, failed) == (1, 0)

    def test_flushing_a_disabled_buffer_writes_nothing(self, tmp_path):
        """Called directly: a disabled group commit has no buffer, so the
        flush neither appends nor raises."""
        wal = _direct_wal(tmp_path)
        worker = _worker(wal, RecordingAdapter())
        try:
            worker._flush_writer_buffer(wal)
            entries = wal.recover_unprocessed(0, mode="runtime")
        finally:
            wal.close()

        assert entries == []

    def test_a_wal_without_the_method_is_tolerated(self):
        """A host-injected WAL-like object need not implement the writer's
        buffering at all — the drain must not break on it."""
        worker = _worker(WALWithoutFlush(), RecordingAdapter())

        worker._flush_writer_buffer(WALWithoutFlush())  # must not raise

    def test_a_wal_that_cannot_flush_still_gets_read(self):
        """Fail-open placement: a flush failure must not skip the cycle's
        read, or one unflushable object would stall the whole drain."""
        wal = WALWithoutFlush()
        worker = _worker(wal, RecordingAdapter())

        synced, failed = worker._sync_batch()

        assert wal.reads > 0, "the cycle read the WAL despite the unflushable object"
        assert (synced, failed) == (0, 0)
