"""Unit tests for baldur.core.file_utils (safe_unlink)."""

import logging
from pathlib import Path
from unittest.mock import MagicMock

from baldur.core.file_utils import safe_unlink


class TestSafeUnlinkBehavior:
    """safe_unlink() behavior verification."""

    def test_safe_unlink_existing_file_returns_true(self, tmp_path):
        """Existing file is deleted and returns True."""
        f = tmp_path / "target.txt"
        f.write_text("data")

        result = safe_unlink(f)

        assert result is True
        assert not f.exists()

    def test_safe_unlink_missing_file_returns_false(self, tmp_path):
        """Non-existent file returns False without raising."""
        missing = tmp_path / "nonexistent.txt"

        result = safe_unlink(missing)

        assert result is False

    def test_safe_unlink_permission_error_returns_false(self, tmp_path):
        """PermissionError is caught and returns False."""
        mock_path = MagicMock(spec=Path)
        mock_path.unlink.side_effect = PermissionError("access denied")

        result = safe_unlink(mock_path)

        assert result is False

    def test_safe_unlink_os_error_returns_false(self, tmp_path):
        """Generic OSError is caught and returns False."""
        mock_path = MagicMock(spec=Path)
        mock_path.unlink.side_effect = OSError("device busy")

        result = safe_unlink(mock_path)

        assert result is False

    def test_safe_unlink_permission_error_logs_warning(self, caplog):
        """PermissionError triggers a warning log."""
        mock_path = MagicMock(spec=Path)
        mock_path.unlink.side_effect = PermissionError("denied")
        mock_path.__str__ = lambda _: "/fake/path"

        with caplog.at_level(logging.WARNING, logger="baldur.core.file_utils"):
            safe_unlink(mock_path)

        assert any("permission_denied" in r.message for r in caplog.records)

    def test_safe_unlink_os_error_logs_warning(self, caplog):
        """OSError triggers a warning log."""
        mock_path = MagicMock(spec=Path)
        mock_path.unlink.side_effect = OSError("device busy")
        mock_path.__str__ = lambda _: "/fake/path"

        with caplog.at_level(logging.WARNING, logger="baldur.core.file_utils"):
            safe_unlink(mock_path)

        assert any("os_failed" in r.message for r in caplog.records)

    def test_safe_unlink_idempotent_on_double_delete(self, tmp_path):
        """Calling safe_unlink twice on same file: first True, second False."""
        f = tmp_path / "once.txt"
        f.write_text("data")

        first = safe_unlink(f)
        second = safe_unlink(f)

        assert first is True
        assert second is False
