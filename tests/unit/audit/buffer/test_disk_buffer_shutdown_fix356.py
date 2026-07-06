"""
Unit tests for disk_buffer_shutdown.py — fix(356) logging restore and the
signal-path / full-teardown function split.

Tests:
I. _shutdown_disk_buffer restores logging.raiseExceptions in finally block,
   returns a success bool, and nulls the module instance.
II. _flush_disk_buffer (signal path) is non-destructive: instance and state
    stay alive, exceptions are swallowed, the flushed count is returned.

Mocks are spec-less MagicMock by design: the module under test reads
private buffer fields (_settings/_env/_group_writer) that exist only on
instances, so a class spec cannot express them (file precedent).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import baldur.audit.persistence.disk_buffer_shutdown as shutdown_module


class TestDiskBufferShutdownLoggingRestoreBehavior:
    """logging.raiseExceptions must be restored after shutdown, even on error."""

    def setup_method(self) -> None:
        self._original_instance = shutdown_module._disk_buffer_instance

    def teardown_method(self) -> None:
        shutdown_module._disk_buffer_instance = self._original_instance
        logging.raiseExceptions = True

    def test_logging_raise_exceptions_restored_on_success(self) -> None:
        """logging.raiseExceptions is restored after successful shutdown."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()

        shutdown_module._disk_buffer_instance = mock_buffer
        logging.raiseExceptions = True

        shutdown_module._shutdown_disk_buffer()

        assert logging.raiseExceptions is True
        assert shutdown_module._disk_buffer_instance is None

    def test_logging_raise_exceptions_restored_on_error(self) -> None:
        """logging.raiseExceptions is restored even when shutdown raises."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = True
        mock_buffer.flush_group_commit.side_effect = RuntimeError("flush failed")

        shutdown_module._disk_buffer_instance = mock_buffer
        logging.raiseExceptions = True

        # Should not raise (exception is caught)
        shutdown_module._shutdown_disk_buffer()

        assert logging.raiseExceptions is True
        assert shutdown_module._disk_buffer_instance is None

    def test_logging_raise_exceptions_restored_to_original_false(self) -> None:
        """If logging.raiseExceptions was False, it stays False after shutdown."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()

        shutdown_module._disk_buffer_instance = mock_buffer
        logging.raiseExceptions = False

        shutdown_module._shutdown_disk_buffer()

        assert logging.raiseExceptions is False
        assert shutdown_module._disk_buffer_instance is None

    def test_noop_when_instance_is_none(self) -> None:
        """No-op when _disk_buffer_instance is None."""
        shutdown_module._disk_buffer_instance = None
        original = logging.raiseExceptions

        shutdown_module._shutdown_disk_buffer()

        assert logging.raiseExceptions == original

    def test_instance_set_to_none_after_shutdown(self) -> None:
        """_disk_buffer_instance is set to None after shutdown."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = None

        shutdown_module._disk_buffer_instance = mock_buffer

        shutdown_module._shutdown_disk_buffer()

        assert shutdown_module._disk_buffer_instance is None

    def test_shutdown_returns_true_on_success(self) -> None:
        """Full teardown returns True when flush and close succeed."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()
        shutdown_module._disk_buffer_instance = mock_buffer

        result = shutdown_module._shutdown_disk_buffer()

        assert result is True
        mock_buffer.close.assert_called_once()

    def test_shutdown_returns_true_when_instance_none(self) -> None:
        """No-op call (instance already None) reports success."""
        shutdown_module._disk_buffer_instance = None

        result = shutdown_module._shutdown_disk_buffer()

        assert result is True

    def test_shutdown_returns_false_when_close_raises(self) -> None:
        """A close failure is swallowed but observable via the False return."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()
        mock_buffer.close.side_effect = RuntimeError("close failed")
        shutdown_module._disk_buffer_instance = mock_buffer

        result = shutdown_module._shutdown_disk_buffer()

        assert result is False
        # Instance is still nulled in finally — re-runs stay no-op.
        assert shutdown_module._disk_buffer_instance is None
        assert logging.raiseExceptions is True

    def test_shutdown_second_call_is_noop_and_closes_once(self) -> None:
        """Second call after teardown is a None-guard no-op (idempotent)."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()
        shutdown_module._disk_buffer_instance = mock_buffer

        first = shutdown_module._shutdown_disk_buffer()
        second = shutdown_module._shutdown_disk_buffer()

        assert first is True
        assert second is True
        mock_buffer.close.assert_called_once()


class TestFlushDiskBufferSignalPathBehavior:
    """_flush_disk_buffer (signal path) is non-destructive.

    The signal-time step must leave the buffer open and the module
    instance set so audit writes arriving during the graceful-shutdown
    drain window keep persisting; teardown is owned by
    _shutdown_disk_buffer.
    """

    def setup_method(self) -> None:
        self._original_instance = shutdown_module._disk_buffer_instance

    def teardown_method(self) -> None:
        shutdown_module._disk_buffer_instance = self._original_instance

    def test_flush_when_instance_none_returns_zero(self) -> None:
        """No-op when _disk_buffer_instance is None — returns 0."""
        shutdown_module._disk_buffer_instance = None

        assert shutdown_module._flush_disk_buffer() == 0

    def test_flush_returns_pending_count_and_keeps_instance(self) -> None:
        """Returns the pre-flush pending count; instance is NOT nulled."""
        # Given — 3 pending group-commit entries
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = True
        mock_buffer._group_writer.pending = [object(), object(), object()]
        mock_buffer._env = MagicMock()
        shutdown_module._disk_buffer_instance = mock_buffer

        # When
        flushed = shutdown_module._flush_disk_buffer()

        # Then — flush + sync dispatched, nothing destructive
        assert flushed == 3
        mock_buffer.flush_group_commit.assert_called_once()
        mock_buffer._env.sync.assert_called_once()
        mock_buffer.close.assert_not_called()
        assert shutdown_module._disk_buffer_instance is mock_buffer

    def test_flush_with_group_commit_disabled_returns_zero_and_syncs(self) -> None:
        """Group-commit-disabled config: no flush dispatch, still fsyncs."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = MagicMock()
        shutdown_module._disk_buffer_instance = mock_buffer

        flushed = shutdown_module._flush_disk_buffer()

        assert flushed == 0
        mock_buffer.flush_group_commit.assert_not_called()
        mock_buffer._env.sync.assert_called_once()

    def test_flush_swallows_exception_and_returns_zero(self) -> None:
        """A flush failure is swallowed (signal context); returns 0."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = True
        mock_buffer._group_writer.pending = [object()]
        mock_buffer.flush_group_commit.side_effect = RuntimeError("flush failed")
        shutdown_module._disk_buffer_instance = mock_buffer

        flushed = shutdown_module._flush_disk_buffer()

        assert flushed == 0
        # Non-destructive even on failure: instance survives for the
        # drain-positioned teardown to own.
        assert shutdown_module._disk_buffer_instance is mock_buffer
        mock_buffer.close.assert_not_called()

    def test_flush_skips_sync_when_env_is_none(self) -> None:
        """No fsync dispatch when the LMDB env is absent."""
        mock_buffer = MagicMock()
        mock_buffer._settings.group_commit_enabled = False
        mock_buffer._env = None
        shutdown_module._disk_buffer_instance = mock_buffer

        flushed = shutdown_module._flush_disk_buffer()

        assert flushed == 0
        assert shutdown_module._disk_buffer_instance is mock_buffer
