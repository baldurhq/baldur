"""
WAL Retention Cleaner - time-based audit log cleanup.

Provides retention-period-based cleanup of WAL files.

Policy:
1. Delete files older than retention_days
2. Runs alongside the file-count limit (max_files)
3. Only synced files are eligible for deletion (optional)
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()

# Default retention period (days)
DEFAULT_RETENTION_DAYS = 90


class WALRetentionCleaner:
    """
    Retention-period-based WAL file cleanup.

    Characteristics:
    - Deletes files older than retention_days
    - Can run alongside the file-count limit
    - Only files synced to the central store are deleted (optional)
    """

    def __init__(
        self,
        wal_dir: Path | str,
        retention_days: int | None = None,
        check_synced: bool = True,
        file_pattern: str = "*.wal",
    ):
        """
        Initialize cleaner.

        Args:
            wal_dir: WAL file directory
            retention_days: Retention period (loaded from settings, or the
                default, when None)
            check_synced: Whether to check for sync completion
            file_pattern: Pattern of files eligible for deletion
        """
        self._wal_dir = Path(wal_dir)
        self._retention_days = retention_days or self._get_retention_from_settings()
        self._check_synced = check_synced
        self._file_pattern = file_pattern

    def _get_retention_from_settings(self) -> int:
        """Load retention_days from settings."""
        try:
            from baldur.settings.audit import get_audit_settings

            settings = get_audit_settings()
            return getattr(settings, "retention_days", DEFAULT_RETENTION_DAYS)
        except ImportError:
            logger.debug("retention_cleaner.available")
        except Exception as e:
            logger.debug(
                "retention_cleaner.load_settings_failed",
                error=e,
            )

        return DEFAULT_RETENTION_DAYS

    def cleanup(self) -> int:
        """
        Clean up WAL files past the retention period.

        Returns:
            Number of deleted files
        """
        if not self._wal_dir.exists():
            logger.debug(
                "retention_cleaner.wal_directory_found",
                wal_dir=self._wal_dir,
            )
            return 0

        cutoff = utc_now() - timedelta(days=self._retention_days)
        deleted_count = 0

        for wal_file in self._wal_dir.glob(self._file_pattern):
            try:
                # Check the file's modification time
                mtime = datetime.fromtimestamp(
                    wal_file.stat().st_mtime,
                    tz=UTC,
                )

                if mtime >= cutoff:
                    continue

                # Sync-completion check (optional)
                if self._check_synced and not self._is_synced(wal_file):
                    logger.warning(
                        "retention_cleaner.skipping_unsynced_old_file",
                        wal_file=wal_file.name,
                    )
                    continue

                # Delete the file
                age_days = (utc_now() - mtime).days
                if safe_unlink(wal_file):
                    deleted_count += 1

                    logger.info(
                        "retention_cleaner.deleted_expired_wal_age",
                        wal_file=wal_file.name,
                        age_days=age_days,
                    )

                # Delete the synced marker too
                synced_marker = wal_file.with_suffix(".synced")
                safe_unlink(synced_marker)

            except PermissionError:
                logger.warning(
                    "retention_cleaner.permission_denied",
                    wal_file=wal_file,
                )
            except Exception as e:
                logger.exception(
                    "retention_cleaner.clean_failed",
                    wal_file=wal_file,
                    error=e,
                )

        return deleted_count

    def _is_synced(self, wal_file: Path) -> bool:
        """Check whether a WAL file has finished syncing."""
        # Check for the .synced marker file
        synced_marker = wal_file.with_suffix(".synced")
        return synced_marker.exists()

    def get_stats(self) -> dict:
        """
        Return current WAL directory statistics.

        Returns:
            Statistics dictionary
        """
        if not self._wal_dir.exists():
            return {
                "wal_dir": str(self._wal_dir),
                "exists": False,
                "total_files": 0,
                "total_size_bytes": 0,
                "expired_files": 0,
                "synced_files": 0,
            }

        cutoff = utc_now() - timedelta(days=self._retention_days)
        total_files = 0
        total_size = 0
        expired_files = 0
        synced_files = 0

        for wal_file in self._wal_dir.glob(self._file_pattern):
            try:
                stat = wal_file.stat()
                total_files += 1
                total_size += stat.st_size

                mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if mtime < cutoff:
                    expired_files += 1

                if self._is_synced(wal_file):
                    synced_files += 1

            except Exception:
                pass

        return {
            "wal_dir": str(self._wal_dir),
            "exists": True,
            "total_files": total_files,
            "total_size_bytes": total_size,
            "expired_files": expired_files,
            "synced_files": synced_files,
            "retention_days": self._retention_days,
        }


class RetentionCleanupScheduler:
    """
    Periodic retention cleanup scheduler.

    Runs WAL file cleanup periodically on a background thread.
    """

    def __init__(
        self,
        cleaner: WALRetentionCleaner,
        interval_hours: int = 24,
        on_cleanup: Callable[[int], None] | None = None,
    ):
        """
        Initialize scheduler.

        Args:
            cleaner: WALRetentionCleaner instance
            interval_hours: Cleanup interval (hours)
            on_cleanup: Cleanup-complete callback (receives the deleted count)
        """
        self._cleaner = cleaner
        self._interval_seconds = interval_hours * 3600
        self._on_cleanup = on_cleanup
        self._running = False
        self._thread: Thread | None = None
        self._handle: DaemonWorkerHandle | None = None

    def start(self) -> None:
        """Start the scheduler."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._running:
            return

        self._running = True
        self._spawn_thread()
        assert self._thread is not None  # _spawn_thread() invariant
        self._handle = DaemonWorkerHandle(
            thread=self._thread,
            tick_interval_seconds=float(self._interval_seconds),
            restart_callback=self._spawn_thread,
        )
        register_daemon_worker("WAL-RetentionCleanupScheduler", self._handle)
        logger.info("retention_scheduler.started")

    def _spawn_thread(self) -> None:
        """Construct + start a fresh cleanup thread (impl 489 D9)."""
        self._thread = Thread(
            target=self._cleanup_loop_with_crash_capture,
            daemon=True,
            name="WAL-RetentionCleanupScheduler",
        )
        self._thread.start()
        if self._handle is not None:
            self._handle.thread = self._thread

    def _cleanup_loop_with_crash_capture(self) -> None:
        try:
            self._cleanup_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> None:
        """Stop the scheduler."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker
        from baldur.settings.thread_management import (
            get_thread_management_settings,
        )

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False
        timeout = get_thread_management_settings().join_timeout
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        unregister_daemon_worker("WAL-RetentionCleanupScheduler")
        if self._thread is not None and self._thread.is_alive():
            logger.critical(
                "daemon_worker.stop_join_timeout",
                worker_name="WAL-RetentionCleanupScheduler",
                join_timeout_seconds=timeout,
            )
        logger.info("retention_scheduler.stopped")

    def _cleanup_loop(self) -> None:
        """Background cleanup loop."""
        while self._running:
            iter_start = time.monotonic()
            try:
                deleted = self._cleaner.cleanup()

                if deleted > 0:
                    logger.info(
                        "retention_scheduler.cleaned_expired_wal_files",
                        deleted=deleted,
                    )

                if self._on_cleanup:
                    self._on_cleanup(deleted)

            except Exception as e:
                logger.exception(
                    "retention_scheduler.cleanup_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            # Wait until the next cleanup
            for _ in range(int(self._interval_seconds)):
                if not self._running:
                    break
                time.sleep(1)
                if self._handle is not None:
                    self._handle.heartbeat()

    @property
    def is_running(self) -> bool:
        """Whether the scheduler is running."""
        return self._running


def schedule_retention_cleanup(
    wal_dir: Path | str | None = None,
    interval_hours: int = 24,
    retention_days: int | None = None,
) -> RetentionCleanupScheduler:
    """
    Convenience function for scheduling periodic retention cleanup.

    Args:
        wal_dir: WAL directory (env var or default when None)
        interval_hours: Cleanup interval (hours)
        retention_days: Retention period (days)

    Returns:
        The started RetentionCleanupScheduler instance
    """
    if wal_dir is None:
        wal_dir = os.environ.get("AUDIT_WAL_DIR", "/var/log/audit/wal")

    cleaner = WALRetentionCleaner(
        wal_dir=wal_dir,
        retention_days=retention_days,
    )

    scheduler = RetentionCleanupScheduler(
        cleaner=cleaner,
        interval_hours=interval_hours,
    )
    scheduler.start()

    return scheduler


def mark_as_synced(wal_file: Path | str) -> bool:
    """
    Mark a WAL file as synced.

    Args:
        wal_file: WAL file path

    Returns:
        Whether it succeeded
    """
    try:
        wal_path = Path(wal_file)
        synced_marker = wal_path.with_suffix(".synced")
        synced_marker.touch()
        return True
    except Exception as e:
        logger.exception(
            "retention_cleaner.mark_synced_failed",
            error=e,
        )
        return False
