"""
Unit tests for disk_buffer.py fix(356) changes.

Tests:
G. assert → DiskBufferError explicit guards (4 locations)
H. health_check uses DeadLetterStore.count() O(1) instead of get_dead_letters()
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer
from baldur.audit.persistence.disk_buffer_models import (
    BufferState,
    DiskBufferError,
)


class TestDiskBufferExplicitErrorGuardsBehavior:
    """assert replaced with explicit DiskBufferError raises."""

    def _make_buffer_stub(self):
        """Create a minimal DiskPersistentBuffer for testing guards (skip LMDB init)."""
        buf = DiskPersistentBuffer.__new__(DiskPersistentBuffer)
        buf._lock = threading.Lock()
        buf._state = BufferState.ACTIVE
        buf._stats = {
            "total_puts": 0,
            "disk_full_events": 0,
        }
        return buf

    def test_put_raises_disk_buffer_error_when_group_writer_none(self) -> None:
        """put() raises DiskBufferError when group_commit_enabled but _group_writer is None."""
        buf = self._make_buffer_stub()
        buf._settings = MagicMock()
        buf._settings.group_commit_enabled = True
        buf._settings.fail_open_on_disk_full = False
        buf._group_writer = None
        buf._disk_monitor = MagicMock()
        buf._disk_monitor.check.return_value = (True, 0.8)

        with pytest.raises(DiskBufferError, match="GroupCommitWriter not initialized"):
            buf.put({"test": "data"})

    def test_check_disk_space_raises_when_monitor_none(self) -> None:
        """_check_disk_space() raises DiskBufferError when _disk_monitor is None."""
        buf = self._make_buffer_stub()
        buf._disk_monitor = None

        with pytest.raises(DiskBufferError, match="DiskSpaceMonitor not initialized"):
            buf._check_disk_space()

    def test_flush_to_raises_when_dead_letters_none(self) -> None:
        """flush_to() raises DiskBufferError when _dead_letters is None."""
        buf = self._make_buffer_stub()
        buf._dead_letters = None

        with pytest.raises(DiskBufferError, match="DeadLetterStore not initialized"):
            buf.flush_to(handler=lambda batch: True)

    def test_handle_flush_failure_raises_when_dead_letters_none(self) -> None:
        """_handle_flush_failure() raises DiskBufferError when _dead_letters is None."""
        buf = self._make_buffer_stub()
        buf._dead_letters = None

        with pytest.raises(DiskBufferError, match="DeadLetterStore not initialized"):
            buf._handle_flush_failure(entries=[], error=RuntimeError("test"))


class TestDiskBufferHealthStatusCountBehavior:
    """get_health_status should use DeadLetterStore.count() for O(1) performance."""

    def _make_buf(self):
        buf = DiskPersistentBuffer.__new__(DiskPersistentBuffer)
        buf._lock = threading.Lock()
        buf._state = BufferState.ACTIVE
        buf._env = MagicMock()
        buf._settings = MagicMock()
        buf._settings.enable_dead_letter_db = True
        buf._disk_monitor = MagicMock()
        buf._disk_monitor.is_healthy.return_value = (True, 0.8, [])
        buf.count = MagicMock(return_value=100)
        return buf

    def test_get_health_status_calls_dead_letters_count(self) -> None:
        """get_health_status uses _dead_letters.count() instead of get_dead_letters()."""
        buf = self._make_buf()
        buf._dead_letters = MagicMock()
        buf._dead_letters.count.return_value = 5

        result = buf.get_health_status()

        buf._dead_letters.count.assert_called_once()
        assert result["dead_letter_count"] == 5
        assert result["healthy"] is True

    def test_get_health_status_high_dead_letter_count_adds_error(self) -> None:
        """get_health_status adds error when dead letter count > 100."""
        buf = self._make_buf()
        buf._dead_letters = MagicMock()
        buf._dead_letters.count.return_value = 200

        result = buf.get_health_status()

        assert result["dead_letter_count"] == 200
        assert any("High dead letter count" in e for e in result["errors"])

    def test_get_health_status_skips_dead_letters_when_none(self) -> None:
        """get_health_status skips dead letter count when _dead_letters is None."""
        buf = self._make_buf()
        buf._dead_letters = None

        result = buf.get_health_status()

        assert result["dead_letter_count"] == 0
