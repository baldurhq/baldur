"""Retention cleaner unit tests.

Targets:
- WALRetentionCleaner
- RetentionCleanupScheduler
- mark_as_synced
"""

from __future__ import annotations

import os
import time


class TestWALRetentionCleaner:
    """WALRetentionCleaner behavior."""

    def test_cleanup_deletes_old_files(self, tmp_path):
        """A file past the retention period is deleted."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # Old WAL file
        old_file = tmp_path / "old.wal"
        old_file.touch()

        # Backdate the file by 100 days
        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        # Synced marker: the file has been replicated
        synced_marker = tmp_path / "old.synced"
        synced_marker.touch()

        # Cleaner with a 90-day retention period
        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
            check_synced=True,
        )

        deleted = cleaner.cleanup()

        assert deleted == 1
        assert not old_file.exists()
        assert not synced_marker.exists()

    def test_cleanup_keeps_recent_files(self, tmp_path):
        """A recent file is kept."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # Recent WAL file
        recent_file = tmp_path / "recent.wal"
        recent_file.touch()

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
        )

        deleted = cleaner.cleanup()

        assert deleted == 0
        assert recent_file.exists()

    def test_cleanup_skips_unsynced_files(self, tmp_path):
        """An unsynced file is skipped."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # Old WAL file, never synced
        old_file = tmp_path / "old_unsynced.wal"
        old_file.touch()

        # Backdate the file by 100 days
        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        # No synced marker

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
            check_synced=True,
        )

        deleted = cleaner.cleanup()

        assert deleted == 0
        assert old_file.exists()

    def test_cleanup_deletes_without_sync_check(self, tmp_path):
        """With the sync check off, an unsynced file is deleted too."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # Old WAL file, never synced
        old_file = tmp_path / "old_unsynced.wal"
        old_file.touch()

        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
            check_synced=False,
        )

        deleted = cleaner.cleanup()

        assert deleted == 1
        assert not old_file.exists()

    def test_get_stats_returns_correct_info(self, tmp_path):
        """get_stats reports file counts, size and retention."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # WAL files
        (tmp_path / "file1.wal").write_text("data1")
        (tmp_path / "file2.wal").write_text("data2data2")
        (tmp_path / "file1.synced").touch()

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
        )

        stats = cleaner.get_stats()

        assert stats["exists"] is True
        assert stats["total_files"] == 2
        assert stats["total_size_bytes"] > 0
        assert stats["synced_files"] == 1
        assert stats["retention_days"] == 90

    def test_get_stats_handles_missing_dir(self, tmp_path):
        """A missing directory is reported rather than raising."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        nonexistent = tmp_path / "nonexistent"

        cleaner = WALRetentionCleaner(
            wal_dir=nonexistent,
            retention_days=90,
        )

        stats = cleaner.get_stats()

        assert stats["exists"] is False
        assert stats["total_files"] == 0


class TestRetentionCleanupScheduler:
    """RetentionCleanupScheduler behavior."""

    def test_scheduler_starts_and_stops(self, tmp_path):
        """The scheduler starts and stops."""
        from baldur.audit.retention_cleaner import (
            RetentionCleanupScheduler,
            WALRetentionCleaner,
        )

        cleaner = WALRetentionCleaner(wal_dir=tmp_path, retention_days=90)
        scheduler = RetentionCleanupScheduler(cleaner, interval_hours=24)

        assert scheduler.is_running is False

        scheduler.start()
        assert scheduler.is_running is True

        scheduler.stop()
        time.sleep(0.1)  # let the thread finish
        assert scheduler.is_running is False

    def test_scheduler_calls_cleanup(self, tmp_path):
        """The scheduler invokes cleanup."""
        from baldur.audit.retention_cleaner import (
            RetentionCleanupScheduler,
            WALRetentionCleaner,
        )

        cleaner = WALRetentionCleaner(wal_dir=tmp_path, retention_days=90)

        callback_called = []

        def on_cleanup(deleted):
            callback_called.append(deleted)

        scheduler = RetentionCleanupScheduler(
            cleaner,
            interval_hours=24,
            on_cleanup=on_cleanup,
        )

        scheduler.start()
        time.sleep(0.2)  # let one cleanup run
        scheduler.stop()

        assert len(callback_called) >= 1

    def test_scheduler_prevents_double_start(self, tmp_path):
        """A second start is ignored."""
        from baldur.audit.retention_cleaner import (
            RetentionCleanupScheduler,
            WALRetentionCleaner,
        )

        cleaner = WALRetentionCleaner(wal_dir=tmp_path)
        scheduler = RetentionCleanupScheduler(cleaner, interval_hours=24)

        scheduler.start()
        scheduler.start()  # ignored

        assert scheduler.is_running is True
        scheduler.stop()


class TestMarkAsSynced:
    """mark_as_synced behavior."""

    def test_creates_synced_marker(self, tmp_path):
        """A synced marker file is created."""
        from baldur.audit.retention_cleaner import mark_as_synced

        wal_file = tmp_path / "test.wal"
        wal_file.touch()

        result = mark_as_synced(wal_file)

        assert result is True
        assert (tmp_path / "test.synced").exists()

    def test_mark_as_synced_with_string_path(self, tmp_path):
        """A string path works as well as a Path."""
        from baldur.audit.retention_cleaner import mark_as_synced

        wal_file = tmp_path / "test.wal"
        wal_file.touch()

        result = mark_as_synced(str(wal_file))

        assert result is True
        assert (tmp_path / "test.synced").exists()

    def test_mark_as_synced_handles_error(self, tmp_path, mocker):
        """A write failure is reported as False rather than raising."""
        from baldur.audit.retention_cleaner import mark_as_synced

        # Simulate a failure inside Path.touch()
        mocker.patch("pathlib.Path.touch", side_effect=PermissionError("Access denied"))

        result = mark_as_synced(tmp_path / "test.wal")

        assert result is False


class TestScheduleRetentionCleanup:
    """schedule_retention_cleanup behavior."""

    def test_creates_and_starts_scheduler(self, tmp_path):
        """The scheduler is created and started."""
        from baldur.audit.retention_cleaner import schedule_retention_cleanup

        scheduler = schedule_retention_cleanup(
            wal_dir=tmp_path,
            interval_hours=24,
            retention_days=90,
        )

        try:
            assert scheduler.is_running is True
        finally:
            scheduler.stop()


class TestRetentionCleanerEnvContract:
    """WAL directory resolution from the environment, prefixed name first.

    ``retention_cleaner`` read the unprefixed ``AUDIT_WAL_DIR`` for the same
    directory every other surface names ``BALDUR_AUDIT_WAL_DIR``. The legacy
    alias is still honored so existing user code keeps working, but the read
    warns and the prefixed name wins.
    """

    def test_prefixed_env_var_is_returned(self, monkeypatch):
        """Design contract: the canonical variable is read first."""
        from baldur.audit.retention_cleaner import _resolve_wal_dir_from_env
        from baldur.audit.wal import WAL_DIR_ENV_VAR

        monkeypatch.setenv(WAL_DIR_ENV_VAR, "/srv/prefixed")

        assert _resolve_wal_dir_from_env() == "/srv/prefixed"

    def test_prefixed_env_var_does_not_warn(self, monkeypatch):
        """Negative: the canonical read is not a deprecation event."""
        from structlog.testing import capture_logs

        from baldur.audit.retention_cleaner import _resolve_wal_dir_from_env
        from baldur.audit.wal import WAL_DIR_ENV_VAR

        monkeypatch.setenv(WAL_DIR_ENV_VAR, "/srv/prefixed")

        with capture_logs() as logs:
            _resolve_wal_dir_from_env()

        assert [
            r for r in logs if r["event"] == "retention_cleaner.legacy_env_var_used"
        ] == []

    def test_legacy_env_var_is_honored_with_a_warning_naming_both(self, monkeypatch):
        """Design contract: the naming break is honored but announced."""
        from structlog.testing import capture_logs

        from baldur.audit.retention_cleaner import _resolve_wal_dir_from_env
        from baldur.audit.wal import LEGACY_WAL_DIR_ENV_VAR, WAL_DIR_ENV_VAR

        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        monkeypatch.setenv(LEGACY_WAL_DIR_ENV_VAR, "/srv/legacy")

        with capture_logs() as logs:
            resolved = _resolve_wal_dir_from_env()

        records = [
            r for r in logs if r["event"] == "retention_cleaner.legacy_env_var_used"
        ]
        assert resolved == "/srv/legacy"
        assert len(records) == 1
        assert records[0]["legacy_env"] == LEGACY_WAL_DIR_ENV_VAR
        assert records[0]["canonical_env"] == WAL_DIR_ENV_VAR
        assert records[0]["log_level"] == "warning"

    def test_prefixed_env_var_wins_over_the_legacy_alias(self, monkeypatch):
        """Design contract: prefixed-first, so a migration can set both."""
        from baldur.audit.retention_cleaner import _resolve_wal_dir_from_env
        from baldur.audit.wal import LEGACY_WAL_DIR_ENV_VAR, WAL_DIR_ENV_VAR

        monkeypatch.setenv(WAL_DIR_ENV_VAR, "/srv/prefixed")
        monkeypatch.setenv(LEGACY_WAL_DIR_ENV_VAR, "/srv/legacy")

        assert _resolve_wal_dir_from_env() == "/srv/prefixed"

    def test_neither_env_var_yields_the_default_wal_dir(self, monkeypatch):
        """Design contract: the default matches WALConfig's own."""
        from baldur.audit.retention_cleaner import (
            DEFAULT_WAL_DIR,
            _resolve_wal_dir_from_env,
        )
        from baldur.audit.wal import LEGACY_WAL_DIR_ENV_VAR, WAL_DIR_ENV_VAR

        monkeypatch.delenv(WAL_DIR_ENV_VAR, raising=False)
        monkeypatch.delenv(LEGACY_WAL_DIR_ENV_VAR, raising=False)

        assert _resolve_wal_dir_from_env() == DEFAULT_WAL_DIR
        assert DEFAULT_WAL_DIR == "/var/log/audit/wal"

    def test_scheduler_without_an_explicit_dir_uses_the_prefixed_env_var(
        self, monkeypatch, tmp_path
    ):
        """The convenience entry point is what actually consumes the resolution."""
        from baldur.audit.retention_cleaner import schedule_retention_cleanup
        from baldur.audit.wal import WAL_DIR_ENV_VAR

        monkeypatch.setenv(WAL_DIR_ENV_VAR, str(tmp_path / "from-env"))

        scheduler = schedule_retention_cleanup(interval_hours=24, retention_days=90)

        try:
            assert scheduler._cleaner._wal_dir == tmp_path / "from-env"
        finally:
            scheduler.stop()
