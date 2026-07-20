"""
Sync Status Tracking for SafeGauge.

Provides synchronization state management for metric reliability.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import structlog

logger = structlog.get_logger()


class SyncStatus(str, Enum):
    """Metric sync status."""

    SYNCED = "synced"  # Synced normally
    STALE = "stale"  # Sync lag (staleness threshold exceeded)
    UNKNOWN = "unknown"  # Initial state, or unknown
    RECOVERING = "recovering"  # Recovering from strict mode


@dataclass
class SyncInfo:
    """Metric sync info."""

    status: SyncStatus = SyncStatus.UNKNOWN
    last_sync_time: float | None = None  # Unix timestamp
    last_sync_source: str = "none"  # "push", "hydration", "manual", "snapshot"
    staleness_threshold: float = 300.0  # 5 min (seconds)
    stabilization_start: float | None = None  # Recovery start time
    stabilization_duration: float = 60.0  # Stabilization window (seconds)

    @property
    def age_seconds(self) -> float | None:
        """Seconds elapsed since the last sync."""
        if self.last_sync_time is None:
            return None
        return time.time() - self.last_sync_time

    @property
    def is_synced(self) -> bool:
        """Whether the data can be trusted."""
        if self.status == SyncStatus.SYNCED:
            age = self.age_seconds
            return not (age is not None and age > self.staleness_threshold)
        return False

    @property
    def is_recovering(self) -> bool:
        """Whether recovery is in progress."""
        if self.status != SyncStatus.RECOVERING:
            return False
        if self.stabilization_start is None:
            return False
        elapsed = time.time() - self.stabilization_start
        return elapsed < self.stabilization_duration

    @property
    def recovery_progress(self) -> float:
        """Recovery progress (0.0 - 1.0)."""
        if not self.is_recovering or self.stabilization_start is None:
            return 1.0
        elapsed = time.time() - self.stabilization_start
        return min(1.0, elapsed / self.stabilization_duration)

    def mark_synced(self, source: str = "push") -> None:
        """Mark the sync as complete."""
        now = time.time()

        if self.status in (SyncStatus.STALE, SyncStatus.UNKNOWN):
            # Recovering from stale -> start the stabilization window
            self.status = SyncStatus.RECOVERING
            self.stabilization_start = now
            logger.info(
                "sync_info.starting_stabilization_period",
                stabilization_duration=self.stabilization_duration,
            )
        elif self.status == SyncStatus.RECOVERING:
            # Still syncing while recovering -> keep the stabilization window
            if not self.is_recovering:
                # Stabilization window done -> transition to the normal state
                self.status = SyncStatus.SYNCED
                self.stabilization_start = None
                logger.info("sync_info.stabilization_complete_now_synced")
        else:
            self.status = SyncStatus.SYNCED

        self.last_sync_time = now
        self.last_sync_source = source

    def mark_stale(self, reason: str = "timeout") -> None:
        """Mark the state as stale."""
        if self.status != SyncStatus.STALE:
            logger.warning(
                "sync_info.marked_stale",
                reason=reason,
            )
        self.status = SyncStatus.STALE
        self.stabilization_start = None

    def check_staleness(self) -> bool:
        """
        Automatic staleness check.

        Returns:
            True if now stale, False otherwise
        """
        if self.status == SyncStatus.SYNCED:
            age = self.age_seconds
            if age is not None and age > self.staleness_threshold:
                self.mark_stale(
                    f"age {age:.1f}s > threshold {self.staleness_threshold}s"
                )
                return True
        return False


__all__ = [
    "SyncStatus",
    "SyncInfo",
]
