"""
WAL Retention Cleaner - 시간 기반 감사 로그 정리.

WAL 파일 보관 기간 기반 정리 기능 제공.

정책:
1. retention_days 이전 파일 삭제
2. 파일 개수 제한 (max_files)과 병행
3. 동기화 완료된 파일만 삭제 대상 (옵션)
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

# 기본 보관 기간 (일)
DEFAULT_RETENTION_DAYS = 90


class WALRetentionCleaner:
    """
    WAL 파일 보관 기간 기반 정리.

    특징:
    - retention_days 이전 파일 삭제
    - 파일 개수 제한과 병행 가능
    - 중앙 저장소 동기화 완료된 파일만 삭제 (옵션)
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
            wal_dir: WAL 파일 디렉토리
            retention_days: 보관 기간 (None이면 설정에서 로드 또는 기본값)
            check_synced: 동기화 완료 확인 여부
            file_pattern: 삭제 대상 파일 패턴
        """
        self._wal_dir = Path(wal_dir)
        self._retention_days = retention_days or self._get_retention_from_settings()
        self._check_synced = check_synced
        self._file_pattern = file_pattern

    def _get_retention_from_settings(self) -> int:
        """설정에서 retention_days 로드."""
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
        보관 기간 초과 WAL 파일 정리.

        Returns:
            삭제된 파일 수
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
                # 파일 수정 시간 확인
                mtime = datetime.fromtimestamp(
                    wal_file.stat().st_mtime,
                    tz=UTC,
                )

                if mtime >= cutoff:
                    continue

                # 동기화 완료 확인 (옵션)
                if self._check_synced and not self._is_synced(wal_file):
                    logger.warning(
                        "retention_cleaner.skipping_unsynced_old_file",
                        wal_file=wal_file.name,
                    )
                    continue

                # 파일 삭제
                age_days = (utc_now() - mtime).days
                if safe_unlink(wal_file):
                    deleted_count += 1

                    logger.info(
                        "retention_cleaner.deleted_expired_wal_age",
                        wal_file=wal_file.name,
                        age_days=age_days,
                    )

                # synced 마커도 삭제
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
        """WAL 파일이 동기화 완료되었는지 확인."""
        # .synced 마커 파일 존재 확인
        synced_marker = wal_file.with_suffix(".synced")
        return synced_marker.exists()

    def get_stats(self) -> dict:
        """
        현재 WAL 디렉토리 통계 반환.

        Returns:
            통계 딕셔너리
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
    주기적 Retention 정리 스케줄러.

    백그라운드 스레드에서 주기적으로 WAL 파일 정리 실행.
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
            cleaner: WALRetentionCleaner 인스턴스
            interval_hours: 정리 주기 (시간)
            on_cleanup: 정리 완료 콜백 (삭제 파일 수 전달)
        """
        self._cleaner = cleaner
        self._interval_seconds = interval_hours * 3600
        self._on_cleanup = on_cleanup
        self._running = False
        self._thread: Thread | None = None
        self._handle: DaemonWorkerHandle | None = None

    def start(self) -> None:
        """스케줄러 시작."""
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
        """스케줄러 중지."""
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
        """백그라운드 정리 루프."""
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

            # 다음 정리까지 대기
            for _ in range(int(self._interval_seconds)):
                if not self._running:
                    break
                time.sleep(1)
                if self._handle is not None:
                    self._handle.heartbeat()

    @property
    def is_running(self) -> bool:
        """스케줄러 실행 중 여부."""
        return self._running


def schedule_retention_cleanup(
    wal_dir: Path | str | None = None,
    interval_hours: int = 24,
    retention_days: int | None = None,
) -> RetentionCleanupScheduler:
    """
    주기적 Retention 정리 스케줄링 편의 함수.

    Args:
        wal_dir: WAL 디렉토리 (None이면 환경변수 또는 기본값)
        interval_hours: 정리 주기 (시간)
        retention_days: 보관 기간 (일)

    Returns:
        시작된 RetentionCleanupScheduler 인스턴스
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
    WAL 파일을 동기화 완료로 마크.

    Args:
        wal_file: WAL 파일 경로

    Returns:
        성공 여부
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
