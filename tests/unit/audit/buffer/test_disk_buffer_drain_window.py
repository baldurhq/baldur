"""Drain-window persistence sequence for the disk-buffer teardown split.

Real-LMDB tests (tmp_path isolated, xdist-safe) for the two-step
shutdown lifecycle: the signal-time flush is non-destructive so audit
writes arriving during the graceful-shutdown drain window keep
persisting, the drain-positioned teardown closes the buffer, and every
entry written before AND during the drain window survives a reopen.

Also pins the flush_group_commit() CLOSED no-op guard — symmetric with
put()'s CLOSED guard, so post-close callers never touch a closed env.
"""
# traceability: 598 SC3 (D1/D2 drain-window sequence) + D3 guard

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from unittest.mock import patch

import pytest

import baldur.audit.persistence.disk_buffer_shutdown as shutdown_module

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
    temp_dir = tempfile.mkdtemp(prefix="disk_buffer_drain_test_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def drain_settings(temp_db_path: str):
    """Group-commit settings with thresholds high enough that puts
    stay pending until an explicit flush."""
    from baldur.audit.persistence.config import DiskBufferSettings

    return DiskBufferSettings(
        data_dir=temp_db_path,
        lmdb_map_size_mb=50,
        max_entries=1000,
        sync_on_write=False,
        group_commit_enabled=True,
        group_commit_max_entries=100,
        group_commit_interval_ms=3_600_000,  # never auto-flush by time
        enable_dead_letter_db=True,
        enable_shutdown_handlers=False,  # module global injected manually
        include_hostname_in_db_name=False,
        include_pid_in_db_name=False,
        disk_full_threshold=0.0,
    )


@pytest.fixture
def drain_buffer(drain_settings) -> Generator:
    """Buffer injected as the shutdown module's tracked instance."""
    from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

    original_instance = shutdown_module._disk_buffer_instance

    buffer = DiskPersistentBuffer(settings=drain_settings, db_name="drain_test")
    shutdown_module._disk_buffer_instance = buffer
    yield buffer

    shutdown_module._disk_buffer_instance = original_instance
    buffer.close()


class TestDiskBufferDrainWindowBehavior:
    """End-to-end drain-window state sequence against real LMDB."""

    def test_signal_flush_keeps_buffer_writable_for_drain_window(
        self, drain_buffer
    ) -> None:
        """After the signal-time flush, put() still stores entries."""
        from baldur.audit.persistence.disk_buffer import BufferState

        # Given — one entry pending in the group buffer
        key_a = drain_buffer.put({"event": "pre_signal"})
        assert key_a is not None

        # When — signal-time non-destructive flush
        flushed = shutdown_module._flush_disk_buffer()

        # Then — flushed count observed, buffer alive, instance kept
        assert flushed == 1
        assert drain_buffer.state is not BufferState.CLOSED
        assert shutdown_module._disk_buffer_instance is drain_buffer

        key_b = drain_buffer.put({"event": "drain_window"})
        assert key_b is not None

    def test_full_teardown_closes_buffer_and_nulls_instance(self, drain_buffer) -> None:
        """The drain-positioned teardown is the destructive step."""
        from baldur.audit.persistence.disk_buffer import BufferState

        drain_buffer.put({"event": "before_teardown"})

        result = shutdown_module._shutdown_disk_buffer()

        assert result is True
        assert drain_buffer.state is BufferState.CLOSED
        assert shutdown_module._disk_buffer_instance is None

    def test_drain_window_entry_survives_teardown_and_reopen(
        self, drain_settings, drain_buffer
    ) -> None:
        """put A -> flush -> put B -> teardown -> reopen: A and B readable."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        # Given — A written before the signal, B during the drain window
        key_a = drain_buffer.put({"event": "pre_signal"})
        shutdown_module._flush_disk_buffer()
        key_b = drain_buffer.put({"event": "drain_window"})

        # When — drain-positioned full teardown
        assert shutdown_module._shutdown_disk_buffer() is True

        # Then — both entries survive a process-boundary reopen
        reopened = DiskPersistentBuffer(settings=drain_settings, db_name="drain_test")
        try:
            entry_a = reopened.get(key_a)
            entry_b = reopened.get(key_b)
            assert entry_a is not None
            assert entry_a.data["event"] == "pre_signal"
            assert entry_b is not None
            assert entry_b.data["event"] == "drain_window"
        finally:
            reopened.close()


class TestFlushGroupCommitClosedGuardBehavior:
    """flush_group_commit() CLOSED no-op guard (state check precedes
    dispatch), protecting every post-close caller."""

    def test_flush_after_close_returns_without_dispatch(self, drain_buffer) -> None:
        """Post-close flush is a silent no-op — flush_all never dispatched."""
        # Given — a closed buffer with the group writer still attached
        drain_buffer.close()

        # When / Then — no raise, no dispatch into the closed env
        with patch.object(
            drain_buffer._group_writer, "flush_all", autospec=True
        ) as mock_flush_all:
            drain_buffer.flush_group_commit()

        mock_flush_all.assert_not_called()

    def test_flush_before_close_dispatches_to_group_writer(self, drain_buffer) -> None:
        """ACTIVE-state flush still dispatches (guard is CLOSED-only)."""
        drain_buffer.put({"event": "pending"})

        with patch.object(
            drain_buffer._group_writer, "flush_all", autospec=True
        ) as mock_flush_all:
            drain_buffer.flush_group_commit()

        mock_flush_all.assert_called_once()
