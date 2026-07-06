"""
WAL 디스크 관리 모듈.

디스크 풀 처리, 우선순위 기반 Purge, 복구 체크 등을 담당합니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.core.file_utils import safe_unlink

if TYPE_CHECKING:
    from pathlib import Path

    from baldur.audit.wal._models import WALConfig

logger = structlog.get_logger()


class WALDiskManagerMixin:
    """디스크 관리 관련 메서드."""

    if TYPE_CHECKING:
        # Host contract — attributes provided by WriteAheadLog.
        _config: WALConfig
        _wal_dir: Path

    def _handle_disk_full(self) -> None:
        """디스크 풀 상황 처리 (우선순위 기반 Purge 시도 후 Fail-Open 모드 전환)."""
        from baldur.audit.wal._models import WALState

        # 우선순위 기반 Purge 시도
        if self._config.priority_based_purge and self._purge_by_priority():
            logger.info("wal.purge_recovered")
            return

        # Purge 실패 또는 비활성화 시 Fail-Open 모드 전환
        self._state = WALState.DISK_FULL_FAILOPEN
        logger.critical("wal.disk_full_failopen")

        # 메트릭 기록
        try:
            from baldur.metrics.drift_metrics import record_wal_disk_full

            record_wal_disk_full()
        except ImportError:
            pass

        # 알림 전송
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

    def _purge_by_priority(self) -> bool:  # noqa: C901
        """
        우선순위 기반 삭제로 디스크 공간 확보.

        Returns:
            True: 충분한 공간 확보 성공
            False: 공간 확보 실패
        """
        freed_bytes = 0
        target_free = self._config.max_file_size_bytes

        # CRITICAL 제외한 우선순위 순서로 삭제
        purge_priorities = self._config.purge_priority_order[:-1]

        for priority in purge_priorities:
            priority_pattern = f"{self._config.file_prefix}_{priority.lower()}_*.wal"
            priority_files = sorted(
                self._wal_dir.glob(priority_pattern),
                key=lambda f: f.stat().st_mtime,
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

        # 우선순위 파일 없으면 일반 파일 중 오래된 것부터 삭제
        if freed_bytes < target_free:
            general_files = sorted(
                self._wal_dir.glob(f"{self._config.file_prefix}_*.wal"),
                key=lambda f: f.stat().st_mtime,
            )
            critical_min_bytes = self._config.critical_retention_min_mb * 1024 * 1024
            total_size = sum(f.stat().st_size for f in general_files)

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

    def check_disk_recovery(self) -> bool:
        """
        디스크 여유 공간 확보 시 정상 모드 복귀.

        Returns:
            True: 정상 모드로 복귀
            False: 여전히 디스크 풀 상태
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
