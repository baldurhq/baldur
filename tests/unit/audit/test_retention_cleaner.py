"""
Retention Cleaner 단위 테스트.

테스트 대상:
- WALRetentionCleaner
- RetentionCleanupScheduler
- mark_as_synced
"""

from __future__ import annotations

import os
import time


class TestWALRetentionCleaner:
    """WALRetentionCleaner 클래스 테스트."""

    def test_cleanup_deletes_old_files(self, tmp_path):
        """보관 기간 초과 파일이 삭제되는지 확인."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # 오래된 WAL 파일 생성
        old_file = tmp_path / "old.wal"
        old_file.touch()

        # 파일 시간을 100일 전으로 설정
        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        # synced 마커 생성 (동기화 완료 상태)
        synced_marker = tmp_path / "old.synced"
        synced_marker.touch()

        # 90일 보관 기간으로 cleaner 생성
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
        """최근 파일은 유지되는지 확인."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # 최근 WAL 파일 생성
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
        """동기화 안된 파일은 건너뛰는지 확인."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # 오래된 WAL 파일 생성 (미동기화)
        old_file = tmp_path / "old_unsynced.wal"
        old_file.touch()

        # 파일 시간을 100일 전으로 설정
        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        # synced 마커 없음 (미동기화)

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
            check_synced=True,  # 동기화 확인 활성화
        )

        deleted = cleaner.cleanup()

        assert deleted == 0
        assert old_file.exists()  # 미동기화 파일은 유지

    def test_cleanup_deletes_without_sync_check(self, tmp_path):
        """동기화 확인 비활성화 시 미동기화 파일도 삭제 확인."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # 오래된 WAL 파일 생성 (미동기화)
        old_file = tmp_path / "old_unsynced.wal"
        old_file.touch()

        old_time = time.time() - (100 * 24 * 3600)
        os.utime(old_file, (old_time, old_time))

        cleaner = WALRetentionCleaner(
            wal_dir=tmp_path,
            retention_days=90,
            check_synced=False,  # 동기화 확인 비활성화
        )

        deleted = cleaner.cleanup()

        assert deleted == 1
        assert not old_file.exists()

    def test_get_stats_returns_correct_info(self, tmp_path):
        """get_stats가 올바른 정보를 반환하는지 확인."""
        from baldur.audit.retention_cleaner import WALRetentionCleaner

        # WAL 파일들 생성
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
        """존재하지 않는 디렉토리 처리 확인."""
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
    """RetentionCleanupScheduler 클래스 테스트."""

    def test_scheduler_starts_and_stops(self, tmp_path):
        """스케줄러 시작/중지 확인."""
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
        time.sleep(0.1)  # 스레드 종료 대기
        assert scheduler.is_running is False

    def test_scheduler_calls_cleanup(self, tmp_path):
        """스케줄러가 cleanup을 호출하는지 확인."""
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
        time.sleep(0.2)  # cleanup 실행 대기
        scheduler.stop()

        assert len(callback_called) >= 1

    def test_scheduler_prevents_double_start(self, tmp_path):
        """중복 시작 방지 확인."""
        from baldur.audit.retention_cleaner import (
            RetentionCleanupScheduler,
            WALRetentionCleaner,
        )

        cleaner = WALRetentionCleaner(wal_dir=tmp_path)
        scheduler = RetentionCleanupScheduler(cleaner, interval_hours=24)

        scheduler.start()
        scheduler.start()  # 두 번째 시작은 무시되어야 함

        assert scheduler.is_running is True
        scheduler.stop()


class TestMarkAsSynced:
    """mark_as_synced 함수 테스트."""

    def test_creates_synced_marker(self, tmp_path):
        """synced 마커 파일이 생성되는지 확인."""
        from baldur.audit.retention_cleaner import mark_as_synced

        wal_file = tmp_path / "test.wal"
        wal_file.touch()

        result = mark_as_synced(wal_file)

        assert result is True
        assert (tmp_path / "test.synced").exists()

    def test_mark_as_synced_with_string_path(self, tmp_path):
        """문자열 경로로도 동작하는지 확인."""
        from baldur.audit.retention_cleaner import mark_as_synced

        wal_file = tmp_path / "test.wal"
        wal_file.touch()

        result = mark_as_synced(str(wal_file))

        assert result is True
        assert (tmp_path / "test.synced").exists()

    def test_mark_as_synced_handles_error(self, tmp_path, mocker):
        """에러 처리 확인."""
        from baldur.audit.retention_cleaner import mark_as_synced

        # Path.touch()에서 예외 발생 시뮬레이션
        mocker.patch("pathlib.Path.touch", side_effect=PermissionError("Access denied"))

        result = mark_as_synced(tmp_path / "test.wal")

        assert result is False


class TestScheduleRetentionCleanup:
    """schedule_retention_cleanup 함수 테스트."""

    def test_creates_and_starts_scheduler(self, tmp_path):
        """스케줄러가 생성되고 시작되는지 확인."""
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
