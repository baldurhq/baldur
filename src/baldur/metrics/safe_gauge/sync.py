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
    """메트릭 동기화 상태."""

    SYNCED = "synced"  # 정상 동기화됨
    STALE = "stale"  # 동기화 지연 (staleness threshold 초과)
    UNKNOWN = "unknown"  # 초기 상태 또는 알 수 없음
    RECOVERING = "recovering"  # Strict Mode에서 복구 중


@dataclass
class SyncInfo:
    """메트릭 동기화 정보."""

    status: SyncStatus = SyncStatus.UNKNOWN
    last_sync_time: float | None = None  # Unix timestamp
    last_sync_source: str = "none"  # "push", "hydration", "manual", "snapshot"
    staleness_threshold: float = 300.0  # 5분 (초)
    stabilization_start: float | None = None  # 복구 시작 시간
    stabilization_duration: float = 60.0  # 안정화 기간 (초)

    @property
    def age_seconds(self) -> float | None:
        """마지막 동기화 이후 경과 시간 (초)."""
        if self.last_sync_time is None:
            return None
        return time.time() - self.last_sync_time

    @property
    def is_synced(self) -> bool:
        """데이터가 신뢰할 수 있는지 여부."""
        if self.status == SyncStatus.SYNCED:
            age = self.age_seconds
            return not (age is not None and age > self.staleness_threshold)
        return False

    @property
    def is_recovering(self) -> bool:
        """복구 중인지 여부."""
        if self.status != SyncStatus.RECOVERING:
            return False
        if self.stabilization_start is None:
            return False
        elapsed = time.time() - self.stabilization_start
        return elapsed < self.stabilization_duration

    @property
    def recovery_progress(self) -> float:
        """복구 진행률 (0.0 ~ 1.0)."""
        if not self.is_recovering or self.stabilization_start is None:
            return 1.0
        elapsed = time.time() - self.stabilization_start
        return min(1.0, elapsed / self.stabilization_duration)

    def mark_synced(self, source: str = "push") -> None:
        """동기화 완료 마킹."""
        now = time.time()

        if self.status in (SyncStatus.STALE, SyncStatus.UNKNOWN):
            # Stale에서 복구 → 안정화 기간 시작
            self.status = SyncStatus.RECOVERING
            self.stabilization_start = now
            logger.info(
                "sync_info.starting_stabilization_period",
                stabilization_duration=self.stabilization_duration,
            )
        elif self.status == SyncStatus.RECOVERING:
            # 복구 중 계속 동기화 → 안정화 기간 유지
            if not self.is_recovering:
                # 안정화 기간 완료 → 정상 상태로 전환
                self.status = SyncStatus.SYNCED
                self.stabilization_start = None
                logger.info("sync_info.stabilization_complete_now_synced")
        else:
            self.status = SyncStatus.SYNCED

        self.last_sync_time = now
        self.last_sync_source = source

    def mark_stale(self, reason: str = "timeout") -> None:
        """Stale 상태로 마킹."""
        if self.status != SyncStatus.STALE:
            logger.warning(
                "sync_info.marked_stale",
                reason=reason,
            )
        self.status = SyncStatus.STALE
        self.stabilization_start = None

    def check_staleness(self) -> bool:
        """
        Staleness 자동 체크.

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
