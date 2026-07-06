"""Disk space monitoring for the disk-persistent buffer.

Monitors free disk space ratio and triggers fail-open mode or
priority-based purge when thresholds are breached.  All thresholds
are ratio-based (0.0 -- 1.0) and sourced from DiskBufferSettings.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.persistence.config import DiskBufferSettings

__all__ = [
    "DiskSpaceMonitor",
]

logger = structlog.get_logger()


class DiskSpaceMonitor:
    """Ratio-based disk space monitor.

    Watches disk usage against ``full_threshold`` and ``recovery_threshold``
    from :class:`DiskBufferSettings` and exposes helpers so that the
    owning :class:`DiskPersistentBuffer` can decide on state transitions.

    Args:
        path: Filesystem path to monitor (typically ``settings.data_path``).
        settings: Buffer settings that supply threshold values.
    """

    def __init__(self, path: Path, settings: DiskBufferSettings) -> None:
        self._path = path
        self._settings = settings

    # ── Public helpers ────────────────────────────────────

    def check(self) -> tuple[bool, float]:
        """Check current disk space.

        Returns:
            A ``(ok, free_ratio)`` tuple.  *ok* is ``False`` when the
            free ratio is below ``disk_full_threshold``.  If the disk
            check itself fails the method returns ``(True, -1.0)`` so
            that the caller keeps writing (fail-open on check failure).
        """
        try:
            usage = shutil.disk_usage(self._path)
            free_ratio = usage.free / usage.total
            ok = free_ratio >= self._settings.disk_full_threshold
            return ok, free_ratio
        except Exception as e:
            logger.debug(
                "disk_buffer.disk_check_failed",
                error=e,
            )
            return True, -1.0  # Continue on check failure

    def should_recover(self, free_ratio: float) -> bool:
        """Return ``True`` when free space exceeds the recovery threshold."""
        return free_ratio > self._settings.disk_recovery_threshold

    def should_fail_open(self) -> bool:
        """Return ``True`` when disk space is below the full threshold."""
        ok, _ratio = self.check()
        return not ok

    def execute_priority_purge(
        self,
        *,
        count_fn: Any,
        iter_fn: Any,
        delete_batch_fn: Any,
    ) -> int:
        """Purge the oldest 10 % of entries to reclaim disk space.

        Args:
            count_fn: Callable returning current entry count.
            iter_fn: Callable accepting ``limit`` kwarg yielding entries.
            delete_batch_fn: Callable accepting a list of keys, returning
                the number of deleted entries.

        Returns:
            Number of purged entries (0 if fewer than 100 entries exist).
        """
        total = count_fn()
        if total < 100:
            return 0

        to_delete = total // 10
        keys_to_delete = [entry.key for entry in iter_fn(limit=to_delete)]
        return delete_batch_fn(keys_to_delete)

    def send_disk_full_alert(self) -> None:
        """Send a critical notification about disk-full state."""
        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            payload = NotificationPayload(
                title="\U0001f6a8 DiskBuffer Disk Full - Fail-Open Mode",
                message=(
                    "DiskBuffer disk space exhausted, switching to fail-open mode. "
                    "Immediate action required!"
                ),
                priority=NotificationPriority.CRITICAL,
                category=NotificationCategory.OPERATIONS,
                source="DiskPersistentBuffer",
                dedup_key="disk_buffer:disk_full",
            )
            UnifiedNotificationManager().notify(payload)
        except Exception as e:
            logger.debug(
                "disk_buffer.alert_send_failed",
                error=e,
            )

    def is_healthy(self) -> tuple[bool, float, list[str]]:
        """Health-check helper for :meth:`DiskPersistentBuffer.get_health_status`.

        Returns:
            ``(ok, disk_free_ratio, errors)`` where *ok* is ``False``
            when the free ratio falls below the configured
            ``disk_recovery_threshold``.
        """
        errors: list[str] = []
        disk_free_ratio = -1.0
        try:
            usage = shutil.disk_usage(self._path)
            disk_free_ratio = usage.free / usage.total

            if disk_free_ratio < self._settings.disk_recovery_threshold:
                errors.append(f"Low disk space: {disk_free_ratio:.1%}")
        except Exception as e:
            errors.append(f"Cannot check disk space: {e}")

        return len(errors) == 0, disk_free_ratio, errors
