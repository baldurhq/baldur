"""
WAL 디스크 풀 Fail-Open 및 멀티 프로세스 안전성 테스트.

테스트 대상:
1. 멀티 프로세스 파일명 충돌 방지 (PID 포함)
2. 디스크 풀 Fail-Open 모드
3. Best-Effort Recovery
4. count_unprocessed() 메서드
"""

import os
import tempfile
from unittest.mock import patch

from baldur.audit.wal import (
    WALConfig,
    WALState,
    WriteAheadLog,
)


class TestWALFilenamePIDInclusion:
    """멀티 프로세스 파일명 충돌 방지 테스트."""

    def test_wal_filename_includes_pid(self):
        """WAL 파일명에 PID가 포함되는지 확인."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            filename = wal._get_current_wal_filename()

            # PID가 파일명에 포함되어야 함
            assert str(os.getpid()) in filename
            # 파일명 형식: prefix_timestamp_pid.wal
            assert filename.endswith(".wal")
            assert config.file_prefix in filename

            wal.close()

    def test_different_processes_generate_different_filenames(self):
        """다른 PID는 다른 파일명 생성."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            filename1 = wal._get_current_wal_filename()

            # PID 변경 시뮬레이션
            with patch("os.getpid", return_value=99999):
                filename2 = wal._get_current_wal_filename()

            # 동일 시간에도 PID가 다르면 파일명이 다름
            assert "_99999." in filename2
            assert filename1 != filename2

            wal.close()


class TestWALDiskFullFailOpen:
    """디스크 풀 Fail-Open 모드 테스트."""

    def test_disk_full_failopen_state_added(self):
        """DISK_FULL_FAILOPEN 상태가 WALState에 존재."""
        assert hasattr(WALState, "DISK_FULL_FAILOPEN")
        assert WALState.DISK_FULL_FAILOPEN.value == "disk_full_failopen"

    def test_wal_config_has_failopen_settings(self):
        """WALConfig에 Fail-Open 관련 설정 존재."""
        config = WALConfig()

        assert hasattr(config, "fail_open_on_disk_full")
        assert config.fail_open_on_disk_full is True  # 기본값
        assert hasattr(config, "disk_recovery_threshold")
        assert config.disk_recovery_threshold == 0.1  # 10%

    def test_check_disk_recovery_returns_true_when_not_failopen(self):
        """정상 상태에서 check_disk_recovery()는 True 반환."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            # 정상 상태에서는 True
            assert wal.check_disk_recovery() is True
            assert wal._state == WALState.ACTIVE

            wal.close()

    def test_direct_write_skips_when_disk_full_failopen(self):
        """DISK_FULL_FAILOPEN 상태에서 _direct_write()는 -1 반환."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            # Fail-Open 상태로 강제 설정
            wal._state = WALState.DISK_FULL_FAILOPEN

            # write 호출 시 -1 반환 (스킵)
            seq = wal._direct_write({"test": "data"})
            assert seq == -1

            wal.close()

    def test_handle_disk_full_changes_state(self):
        """_handle_disk_full()가 상태를 DISK_FULL_FAILOPEN으로 변경."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            wal._handle_disk_full()

            assert wal._state == WALState.DISK_FULL_FAILOPEN

            wal.close()


class TestWALBestEffortRecovery:
    """Best-Effort Recovery 테스트."""

    def test_wal_config_has_best_effort_recovery_setting(self):
        """WALConfig에 best_effort_recovery 설정 존재."""
        config = WALConfig()

        assert hasattr(config, "best_effort_recovery")
        assert config.best_effort_recovery is True  # 기본값

    def test_record_magic_constant_exists(self):
        """RECORD_MAGIC 상수 존재."""
        assert hasattr(WriteAheadLog, "RECORD_MAGIC")
        assert WriteAheadLog.RECORD_MAGIC == b"\xab\xcd"

    def test_read_wal_file_best_effort_method_exists(self):
        """_read_wal_file_best_effort() 메서드 존재."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            assert hasattr(wal, "_read_wal_file_best_effort")
            assert callable(wal._read_wal_file_best_effort)

            wal.close()

    def test_scan_for_valid_record_method_exists(self):
        """_scan_for_valid_record() 메서드 존재."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            assert hasattr(wal, "_scan_for_valid_record")
            assert callable(wal._scan_for_valid_record)

            wal.close()


class TestWALCountUnprocessed:
    """count_unprocessed() 메서드 테스트."""

    def test_count_unprocessed_method_exists(self):
        """count_unprocessed() 메서드 존재."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            assert hasattr(wal, "count_unprocessed")
            assert callable(wal.count_unprocessed)

            wal.close()

    def test_count_unprocessed_returns_zero_initially(self):
        """초기 상태에서 count_unprocessed()는 0 반환."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir)
            wal = WriteAheadLog(config=config)

            count = wal.count_unprocessed(last_processed_seq=0)
            assert count == 0

            wal.close()

    def test_count_unprocessed_after_writes(self):
        """엔트리 작성 후 count_unprocessed() 정확성."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = WALConfig(wal_dir=tmpdir, sync_on_write=False)
            wal = WriteAheadLog(config=config)

            # 5개 엔트리 작성
            for i in range(5):
                wal.write({"idx": i})

            # 전체 미처리
            assert wal.count_unprocessed(last_processed_seq=0) == 5

            # 3개 처리됨
            assert wal.count_unprocessed(last_processed_seq=3) == 2

            # 모두 처리됨
            assert wal.count_unprocessed(last_processed_seq=5) == 0

            wal.close()
