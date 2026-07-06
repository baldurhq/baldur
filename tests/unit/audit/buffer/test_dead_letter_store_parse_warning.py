"""
Unit tests for dead_letter_store.py fix(356) — parse failure warning log.

Tests:
K. get_dead_letters logs warning on parse failure instead of silent pass.
"""

from __future__ import annotations

import json
import struct
from unittest.mock import MagicMock, patch


class TestDeadLetterStoreParseWarningBehavior:
    """get_dead_letters should log warning on parse failure."""

    def _make_store(self, entries: dict[bytes, bytes] | None = None):
        """Create a DeadLetterStore with mocked LMDB env."""
        from baldur.audit.persistence.dead_letter_store import DeadLetterStore

        store = DeadLetterStore.__new__(DeadLetterStore)
        store._settings = MagicMock()
        store._settings.enable_dead_letter_db = True

        # Mock LMDB env and cursor
        mock_env = MagicMock()
        mock_txn = MagicMock()
        mock_cursor = MagicMock()

        if entries is None:
            entries = {}

        mock_cursor.__iter__ = lambda self_: iter(entries.items())
        mock_cursor.__enter__ = lambda self_: self_
        mock_cursor.__exit__ = lambda *args: None
        mock_txn.cursor.return_value = mock_cursor
        mock_env.begin.return_value.__enter__ = lambda self_: mock_txn
        mock_env.begin.return_value.__exit__ = lambda *args: None
        store._env = mock_env
        store._dead_letter_db = MagicMock()

        return store

    def _make_valid_entry(self, data: dict) -> bytes:
        """Create a valid entry with checksum prefix."""
        data_bytes = json.dumps(data).encode("utf-8")
        checksum = 0  # dummy checksum
        return struct.pack(">I", checksum) + data_bytes

    def _make_corrupt_entry(self) -> bytes:
        """Create a corrupt entry that will fail JSON parse."""
        return struct.pack(">I", 0) + b"not-valid-json{{"

    def test_parse_failure_logs_warning(self) -> None:
        """Corrupt dead letter entry triggers warning log."""
        entries = {
            b"key-good": self._make_valid_entry({"event": "ok"}),
            b"key-bad": self._make_corrupt_entry(),
        }
        store = self._make_store(entries)

        with patch("baldur.audit.persistence.dead_letter_store.logger") as mock_logger:
            store.get_dead_letters(limit=10)

            mock_logger.warning.assert_called_once()
            call_args = mock_logger.warning.call_args
            assert call_args[0][0] == "disk_buffer.dead_letter_parse_failed"
            assert "key-bad" in call_args[1]["key"]

    def test_parse_failure_does_not_stop_iteration(self) -> None:
        """Corrupt entry is skipped, valid entries still returned."""
        entries = {
            b"key-1": self._make_valid_entry({"event": "first"}),
            b"key-bad": self._make_corrupt_entry(),
            b"key-2": self._make_valid_entry({"event": "second"}),
        }
        store = self._make_store(entries)

        with patch("baldur.audit.persistence.dead_letter_store.logger"):
            result = store.get_dead_letters(limit=10)

        assert len(result) == 2
        events = [r["event"] for r in result]
        assert "first" in events
        assert "second" in events
