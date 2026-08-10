"""
WAL disk management module.

Handles disk-full conditions, priority-based purging, and recovery checks.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from baldur.core.file_utils import safe_unlink
from baldur.core.process_utils import fork_repaired

if TYPE_CHECKING:
    from pathlib import Path

    from baldur.audit.wal._models import WALConfig

logger = structlog.get_logger()


class WALDiskManagerMixin:
    """Disk management methods."""

    if TYPE_CHECKING:
        # Host contract — attributes provided by WriteAheadLog.
        _config: WALConfig
        _wal_dir: Path
        _current_file: Path | None

    def _handle_disk_full(self) -> None:
        """Handle a disk-full condition.

        Tries a priority-based purge first, then switches to Fail-Open mode.
        """
        from baldur.audit.wal._models import WALState

        # Try a priority-based purge
        if self._config.priority_based_purge and self._purge_by_priority():
            logger.info("wal.purge_recovered")
            return

        # Switch to Fail-Open mode if the purge failed or is disabled
        self._state = WALState.DISK_FULL_FAILOPEN
        logger.critical("wal.disk_full_failopen")

        # Record metrics
        try:
            from baldur.metrics.drift_metrics import record_wal_disk_full

            record_wal_disk_full()
        except ImportError:
            pass

        # Send a notification
        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            payload = NotificationPayload(
                title="🚨 WAL Disk Full - Fail-Open Mode",
                message="WAL disk space exhausted. Switched to Fail-Open mode. Immediate action required!",
                priority=NotificationPriority.CRITICAL,
                category=NotificationCategory.OPERATIONS,
                source="WriteAheadLog",
                dedup_key="wal:disk_full",
            )
            UnifiedNotificationManager().notify(payload)
        except Exception as e:
            logger.exception(
                "wal.send_disk_full_failed",
                error=e,
            )

    def _reclaimable_in_order(self, wal_files: list[Path]) -> list[Path]:
        """Order candidate files by who may reclaim them, dropping live peers.

        A file whose embedded PID names another living process is never
        reclaimable — its owner is still appending to it, and freeing space by
        unlinking it would send the owner's subsequent writes to an unlinked
        inode.

        This process's **currently open** file is excluded outright. It is the
        one file guaranteed to have an active writer, so unlinking it is the
        live-peer mistake committed against ourselves: the handle stays open,
        every subsequent record goes to an unlinked inode, and no drain ever
        reads them. The pre-existing oldest-first ordering hid this by putting
        the newest file last; ordering by owner does not, so the exclusion is
        explicit.

        Own-PID files then come first: they are this process's own rotated
        history, the only files it can be sure nobody else is waiting to drain.
        Dead-PID (and PID-less) files come last, because those are precisely
        the un-absorbed orphan backlog — deleting them ahead of this process's
        own history would discard a crashed peer's undelivered entries first.
        """
        from baldur.audit.wal._reader import is_live_peer_wal_file, wal_file_owner_pid

        own_pid = os.getpid()
        own: list[Path] = []
        foreign: list[Path] = []

        for wal_file in wal_files:
            if self._current_file is not None and wal_file == self._current_file:
                continue
            if is_live_peer_wal_file(wal_file):
                continue
            if wal_file_owner_pid(wal_file) == own_pid:
                own.append(wal_file)
            else:
                foreign.append(wal_file)

        return own + foreign

    def _purge_by_priority(self) -> bool:  # noqa: C901
        """
        Free disk space by deleting files in priority order.

        Returns:
            True: enough space was freed
            False: failed to free enough space
        """
        freed_bytes = 0
        target_free = self._config.max_file_size_bytes

        # Delete in priority order, excluding CRITICAL
        purge_priorities = self._config.purge_priority_order[:-1]

        for priority in purge_priorities:
            priority_pattern = f"{self._config.file_prefix}_{priority.lower()}_*.wal"
            priority_files = self._reclaimable_in_order(
                sorted(
                    self._wal_dir.glob(priority_pattern),
                    key=lambda f: f.stat().st_mtime,
                )
            )

            for wal_file in priority_files:
                if freed_bytes >= target_free:
                    logger.info(
                        "wal.priority_purge_complete_freed",
                        freed_bytes=freed_bytes,
                    )
                    return True

                try:
                    file_size = wal_file.stat().st_size
                    if safe_unlink(wal_file):
                        freed_bytes += file_size
                        logger.warning(
                            "wal.priority_purge_deleted",
                            wal_file=wal_file.name,
                            priority=priority,
                            file_size=file_size,
                        )
                except Exception as e:
                    logger.exception(
                        "wal.delete_failed",
                        wal_file=wal_file,
                        error=e,
                    )

        # With no priority files left, delete general files oldest-first
        if freed_bytes < target_free:
            all_general = sorted(
                self._wal_dir.glob(f"{self._config.file_prefix}_*.wal"),
                key=lambda f: f.stat().st_mtime,
            )
            general_files = self._reclaimable_in_order(all_general)
            critical_min_bytes = self._config.critical_retention_min_mb * 1024 * 1024
            # The retention floor is "how much WAL data is left on disk", so it
            # counts every file in the directory — not only the ones this
            # process may reclaim. Measuring it over the reclaimable subset
            # would let a live peer's untouchable file shrink the denominator
            # and stop this process from freeing its own rotated history, in
            # exactly the multi-worker topology per-process WAL files create.
            total_size = sum(f.stat().st_size for f in all_general)

            for wal_file in general_files:
                if freed_bytes >= target_free:
                    return True

                remaining_size = total_size - freed_bytes
                if remaining_size <= critical_min_bytes:
                    logger.warning(
                        "wal.priority_purge_stopped_protect",
                        remaining_size=remaining_size,
                    )
                    break

                try:
                    file_size = wal_file.stat().st_size
                    if safe_unlink(wal_file):
                        freed_bytes += file_size
                        logger.warning(
                            "wal.general_purge_deleted",
                            wal_file=wal_file.name,
                            file_size=file_size,
                        )
                except Exception as e:
                    logger.exception(
                        "wal.delete_failed",
                        wal_file=wal_file,
                        error=e,
                    )

        if freed_bytes >= target_free:
            logger.info(
                "wal.priority_purge_complete_freed",
                freed_bytes=freed_bytes,
            )
            return True

        logger.critical(
            "wal.priority_purge_insufficient_freed",
            freed_bytes=freed_bytes,
            target_free=target_free,
        )
        return False

    @fork_repaired
    def check_disk_recovery(self) -> bool:
        """
        Return to normal mode once free disk space is available again.

        Returns:
            True: returned to normal mode
            False: still disk-full
        """
        from baldur.audit.wal._models import WALState

        if self._state != WALState.DISK_FULL_FAILOPEN:
            return True

        try:
            import shutil

            usage = shutil.disk_usage(self._wal_dir)
            free_ratio = usage.free / usage.total

            if free_ratio > self._config.disk_recovery_threshold:
                self._state = WALState.ACTIVE
                logger.info("wal.disk_recovered")
                return True
        except Exception as e:
            logger.debug(
                "wal.disk_recovery_check_failed",
                error=e,
            )

        return False
