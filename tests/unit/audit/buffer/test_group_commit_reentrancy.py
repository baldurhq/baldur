"""GroupCommitWriter.flush() re-entrancy guard tests.

Real-LMDB tests (tmp_path isolated) for the signal re-entrancy hazard:
a handler firing while the owning thread is inside flush()'s open write
txn re-enters flush() on the SAME thread (the buffer RLock re-entry
succeeds), and a nested env.begin(write=True) would deadlock on LMDB's
non-recursive writer mutex. The guard makes the re-entrant call return
immediately; the interrupted flush then resumes and commits the same
entries exactly once.

Signal delivery itself cannot be unit-driven mid-bytecode, so the
deterministic same-thread re-entry is simulated via a serialization
side-effect inside the open txn — the identical call shape. A missing
guard manifests as a hang killed by the suite-wide --timeout, so this
test cannot false-pass.
"""
# traceability: 598 D8 / SC8 (G5 LMDB writer-mutex deadlock)

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from unittest.mock import patch

import pytest

from baldur.utils.serialization import fast_dumps_str

try:
    import lmdb  # noqa: F401

    LMDB_AVAILABLE = True
except ImportError:
    LMDB_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not LMDB_AVAILABLE,
    reason="lmdb not installed",
)


@pytest.fixture
def temp_db_path() -> Generator[str, None, None]:
    """Temporary LMDB path (auto-removed after the test)."""
    temp_dir = tempfile.mkdtemp(prefix="group_commit_reentrancy_test_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def group_commit_buffer(temp_db_path: str) -> Generator:
    """Group-commit buffer whose puts stay pending until explicit flush."""
    from baldur.audit.persistence.config import DiskBufferSettings
    from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

    settings = DiskBufferSettings(
        data_dir=temp_db_path,
        lmdb_map_size_mb=50,
        max_entries=1000,
        sync_on_write=False,
        group_commit_enabled=True,
        group_commit_max_entries=100,
        group_commit_interval_ms=3_600_000,  # never auto-flush by time
        enable_dead_letter_db=True,
        enable_shutdown_handlers=False,
        include_hostname_in_db_name=False,
        include_pid_in_db_name=False,
        disk_full_threshold=0.0,
    )
    buffer = DiskPersistentBuffer(settings=settings, db_name="reentrancy_test")
    yield buffer
    buffer.close()


class TestGroupCommitFlushReentrancyBehavior:
    """Same-thread re-entrant flush returns via the guard; the outer
    flush commits every entry exactly once."""

    def test_reentrant_flush_during_open_txn_commits_exactly_once(
        self, group_commit_buffer
    ) -> None:
        """Re-entry inside the open write txn returns without a nested txn."""
        # Given — 3 entries pending in the group buffer
        keys = [group_commit_buffer.put({"seq": i}) for i in range(3)]
        assert group_commit_buffer._group_writer is not None
        assert group_commit_buffer._group_writer.pending_count == 3

        calls = {"serializations": 0, "reentry_returned": False}

        def reentering_serializer(entry, **kwargs):
            """First serialization re-enters flush_group_commit on the
            same thread — the exact shape of a signal-handler re-entry —
            then delegates to the real serializer."""
            calls["serializations"] += 1
            if calls["serializations"] == 1:
                group_commit_buffer.flush_group_commit()
                calls["reentry_returned"] = True
            return fast_dumps_str(entry, **kwargs)

        # When — flush with the re-entering side-effect installed
        with patch(
            "baldur.audit.persistence.group_commit.fast_dumps_str",
            autospec=True,
            side_effect=reentering_serializer,
        ):
            group_commit_buffer.flush_group_commit()

        # Then — the re-entrant call returned (no deadlock), and the
        # outer flush committed all entries exactly once
        assert calls["reentry_returned"] is True
        assert calls["serializations"] == 3
        assert group_commit_buffer.get_stats()["group_commit_flushes"] == 1
        for key in keys:
            assert group_commit_buffer.get(key) is not None
        assert group_commit_buffer._group_writer.pending_count == 0

    def test_guard_resets_after_flush_allowing_subsequent_flushes(
        self, group_commit_buffer
    ) -> None:
        """The in-progress flag resets in finally — later flushes work."""
        # Given — a completed first flush
        key_first = group_commit_buffer.put({"round": "first"})
        group_commit_buffer.flush_group_commit()

        # When — a second put + flush after the guard has reset
        key_second = group_commit_buffer.put({"round": "second"})
        group_commit_buffer.flush_group_commit()

        # Then — both rounds committed
        assert group_commit_buffer.get_stats()["group_commit_flushes"] == 2
        assert group_commit_buffer.get(key_first) is not None
        assert group_commit_buffer.get(key_second) is not None
