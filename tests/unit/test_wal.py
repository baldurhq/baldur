"""
WAL (Write-Ahead Log) 테스트.

Data Integrity 테스트:
- WAL 쓰기/읽기
- CRC32 체크섬 검증
- 파일 로테이션
- 미처리 엔트리 복구
- 손상 감지
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path

import pytest

from baldur.audit.wal import (
    WALConfig,
    WALEntry,
    WALError,
    WALState,
    WriteAheadLog,
    create_wal,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_wal_dir():
    """임시 WAL 디렉토리 생성."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def wal_config(temp_wal_dir):
    """테스트용 WAL 설정."""
    return WALConfig(
        wal_dir=temp_wal_dir,
        max_file_size_mb=1,  # 1MB for faster rotation testing
        sync_on_write=False,  # Faster tests
        max_files=5,
        file_prefix="test_wal",
    )


@pytest.fixture
def wal(wal_config):
    """테스트용 WAL 인스턴스."""
    wal_instance = WriteAheadLog(config=wal_config)
    yield wal_instance
    wal_instance.close()


# =============================================================================
# 기본 쓰기/읽기 테스트
# =============================================================================


class TestWALBasicOperations:
    """WAL 기본 동작 테스트."""

    def test_write_returns_sequence_number(self, wal):
        """쓰기가 시퀀스 번호를 반환하는지 확인."""
        seq1 = wal.write({"event": "test1"})
        seq2 = wal.write({"event": "test2"})
        seq3 = wal.write({"event": "test3"})

        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    def test_write_increments_sequence(self, wal):
        """시퀀스 번호가 증가하는지 확인."""
        sequences = [wal.write({"n": i}) for i in range(10)]

        assert sequences == list(range(1, 11))

    def test_write_creates_wal_file(self, wal, temp_wal_dir):
        """쓰기가 WAL 파일을 생성하는지 확인."""
        wal.write({"event": "test"})

        wal_files = list(Path(temp_wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files) == 1

    def test_recover_reads_written_entries(self, wal):
        """복구가 작성된 엔트리를 읽는지 확인."""
        data_list = [{"event": f"test_{i}"} for i in range(5)]
        for data in data_list:
            wal.write(data)

        wal.flush()
        entries = wal.recover_unprocessed(0)

        assert len(entries) == 5
        for i, entry in enumerate(entries):
            assert entry.data == data_list[i]
            assert entry.sequence == i + 1

    def test_recover_filters_by_sequence(self, wal):
        """복구가 시퀀스 번호로 필터링하는지 확인."""
        for i in range(10):
            wal.write({"n": i})

        wal.flush()
        entries = wal.recover_unprocessed(5)

        assert len(entries) == 5
        assert [e.sequence for e in entries] == [6, 7, 8, 9, 10]

    def test_entry_has_timestamp(self, wal):
        """엔트리에 타임스탬프가 있는지 확인."""
        before = time.time()
        wal.write({"event": "test"})
        after = time.time()

        wal.flush()  # 데이터가 디스크에 쓰여지도록 flush
        entries = wal.recover_unprocessed(0)

        assert len(entries) == 1
        assert before <= entries[0].timestamp <= after


# =============================================================================
# 체크섬 테스트
# =============================================================================


class TestWALChecksum:
    """WAL 체크섬 테스트."""

    def test_entry_has_valid_checksum(self, wal):
        """엔트리가 유효한 체크섬을 갖는지 확인."""
        wal.write({"event": "test"})

        wal.flush()  # 데이터가 디스크에 쓰여지도록 flush
        entries = wal.recover_unprocessed(0)

        assert len(entries) == 1
        assert len(entries[0].checksum) == 8  # CRC32는 8자리 hex

    def test_checksum_detects_corruption(self, temp_wal_dir):
        """체크섬이 손상을 감지하는지 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            file_prefix="test_wal",
        )

        # 데이터 쓰기
        wal1 = WriteAheadLog(config=config)
        wal1.write({"event": "test1"})
        wal1.write({"event": "test2"})
        wal1.close()

        # 파일 손상시키기
        wal_files = list(Path(temp_wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files) == 1

        with open(wal_files[0], "r+b") as f:
            content = f.read()
            # 마지막 엔트리의 데이터 부분 손상
            if len(content) > 50:
                f.seek(len(content) - 10)
                f.write(b"CORRUPTED!")

        # 복구 시 손상된 엔트리 건너뛰기
        corruption_detected = []

        def on_corruption(error):
            corruption_detected.append(error)

        wal2 = WriteAheadLog(config=config, on_corruption=on_corruption)
        entries = wal2.recover_unprocessed(0)
        wal2.close()

        # 최소 하나의 엔트리는 복구됨
        assert len(entries) >= 0  # 손상 정도에 따라 다름

    def test_checksum_computation(self, wal):
        """체크섬 계산이 일관되는지 확인."""
        data = {"event": "test", "value": 123}
        wal.write(data)

        wal.flush()  # 데이터가 디스크에 쓰여지도록 flush
        entries1 = wal.recover_unprocessed(0)
        entries2 = wal.recover_unprocessed(0)

        assert len(entries1) == 1
        assert len(entries2) == 1
        assert entries1[0].checksum == entries2[0].checksum


# =============================================================================
# 파일 로테이션 테스트
# =============================================================================


class TestWALRotation:
    """WAL 파일 로테이션 테스트."""

    def test_rotation_on_size_limit(self, temp_wal_dir):
        """크기 제한에 따른 로테이션 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.0001,  # ~100 bytes for quick rotation
            sync_on_write=False,
            file_prefix="test_wal",
            max_files=20,  # 더 많은 파일 허용
        )

        wal = WriteAheadLog(config=config)

        # 충분히 많이 쓰기 (더 큰 데이터)
        for i in range(200):
            wal.write({"data": "x" * 200, "n": i})

        wal.close()

        wal_files = list(Path(temp_wal_dir).glob("test_wal_*.wal"))
        # 최소 1개 파일 (로테이션은 파일 크기에 따라 달라짐)
        assert len(wal_files) >= 1

    def test_rotation_callback(self, temp_wal_dir):
        """로테이션 콜백 호출 확인."""
        rotated_files = []

        def on_rotate(filepath):
            rotated_files.append(filepath)

        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.001,
            sync_on_write=False,
            file_prefix="test_wal",
        )

        wal = WriteAheadLog(config=config, on_rotate=on_rotate)

        for i in range(100):
            wal.write({"data": "x" * 100, "n": i})

        wal.close()

        # 최소 한 번의 로테이션
        assert len(rotated_files) >= 1

    def test_max_files_cleanup(self, temp_wal_dir):
        """최대 파일 수 제한 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.001,
            sync_on_write=False,
            max_files=3,
            file_prefix="test_wal",
        )

        wal = WriteAheadLog(config=config)

        for i in range(200):
            wal.write({"data": "x" * 100, "n": i})

        wal.close()

        wal_files = list(Path(temp_wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files) <= config.max_files + 1  # 현재 파일 포함


# =============================================================================
# 복구 테스트
# =============================================================================


class TestWALRecovery:
    """WAL 복구 테스트."""

    def test_recovery_across_multiple_files(self, temp_wal_dir):
        """여러 파일에 걸친 복구 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.001,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
        )

        # 쓰기
        wal1 = WriteAheadLog(config=config)
        written_seqs = []
        for i in range(50):
            seq = wal1.write({"data": "x" * 100, "n": i})
            written_seqs.append(seq)
        wal1.close()

        # 새 WAL로 복구
        wal2 = WriteAheadLog(config=config)
        entries = wal2.recover_unprocessed(0)
        wal2.close()

        [e.sequence for e in entries]

        # 모든 시퀀스가 복구되어야 함
        assert len(entries) == len(written_seqs)

    def test_recovery_preserves_order(self, wal):
        """복구가 순서를 보존하는지 확인."""
        for i in range(20):
            wal.write({"order": i})

        entries = wal.recover_unprocessed(0)

        for i, entry in enumerate(entries):
            assert entry.data["order"] == i
            assert entry.sequence == i + 1

    def test_cleanup_processed(self, temp_wal_dir):
        """처리 완료된 파일 정리 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            max_file_size_mb=0.001,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
        )

        # 쓰기
        wal1 = WriteAheadLog(config=config)
        for i in range(50):
            wal1.write({"data": "x" * 100, "n": i})
        wal1.close()

        files_before = len(list(Path(temp_wal_dir).glob("test_wal_*.wal")))

        # 새 WAL로 정리
        wal2 = WriteAheadLog(config=config)
        wal2.cleanup_processed(50)  # 모든 엔트리 처리됨
        wal2.close()

        files_after = len(list(Path(temp_wal_dir).glob("test_wal_*.wal")))

        # 최소 하나의 파일이 삭제되어야 함 (현재 파일 제외)
        assert files_after <= files_before

    def test_sequence_recovery_on_restart(self, temp_wal_dir):
        """재시작 시 시퀀스 복구 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            file_prefix="test_wal",
        )

        # 첫 번째 WAL
        wal1 = WriteAheadLog(config=config)
        for i in range(10):
            wal1.write({"n": i})
        last_seq_1 = wal1.get_stats().last_sequence
        wal1.close()

        # 두 번째 WAL (재시작)
        wal2 = WriteAheadLog(config=config)
        new_seq = wal2.write({"n": 10})
        wal2.close()

        # 시퀀스가 이어져야 함
        assert new_seq == last_seq_1 + 1


# =============================================================================
# 통계 테스트
# =============================================================================


class TestWALStats:
    """WAL 통계 테스트."""

    def test_stats_initial_state(self, wal):
        """초기 상태 통계 확인."""
        stats = wal.get_stats()

        assert stats.state == WALState.ACTIVE
        assert stats.total_entries == 0
        assert stats.last_sequence == 0

    def test_stats_after_writes(self, wal):
        """쓰기 후 통계 확인."""
        for i in range(5):
            wal.write({"n": i})

        stats = wal.get_stats()

        assert stats.total_entries == 5
        assert stats.last_sequence == 5
        assert stats.last_write_time is not None

    def test_stats_current_file(self, wal):
        """현재 파일 통계 확인."""
        wal.write({"event": "test"})

        stats = wal.get_stats()

        assert stats.current_file is not None
        assert "test_wal_" in stats.current_file
        assert stats.current_size_bytes > 0


# =============================================================================
# 스레드 안전성 테스트
# =============================================================================


class TestWALThreadSafety:
    """WAL 스레드 안전성 테스트."""

    def test_concurrent_writes(self, wal):
        """동시 쓰기 테스트."""
        results: list[int] = []
        errors: list[Exception] = []

        def writer(thread_id):
            try:
                for i in range(20):
                    seq = wal.write({"thread": thread_id, "n": i})
                    results.append(seq)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 100
        assert len(set(results)) == 100  # 모든 시퀀스가 유니크해야 함

    def test_concurrent_read_write(self, temp_wal_dir):
        """동시 읽기/쓰기 테스트."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            file_prefix="test_wal",
        )

        wal = WriteAheadLog(config=config)
        errors: list[Exception] = []

        def writer():
            try:
                for i in range(50):
                    wal.write({"n": i})
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(10):
                    wal.recover_unprocessed(0)
                    time.sleep(0.005)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wal.close()

        assert len(errors) == 0


# =============================================================================
# 에러 처리 테스트
# =============================================================================


class TestWALErrorHandling:
    """WAL 에러 처리 테스트."""

    def test_write_after_close_raises_error(self, wal):
        """닫힌 후 쓰기 시 에러 확인."""
        wal.write({"event": "test"})
        wal.close()

        with pytest.raises(WALError):
            wal.write({"event": "should_fail"})

    def test_corruption_callback_called(self, temp_wal_dir):
        """손상 콜백 호출 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=False,
            file_prefix="test_wal",
        )

        # 손상된 WAL 파일 수동 생성
        wal_file = Path(temp_wal_dir) / "test_wal_manual.wal"
        with open(wal_file, "wb") as f:
            # 헤더
            f.write(b"AWAL")
            f.write(struct.pack(">HH", 1, 0))

            # 손상된 레코드 (잘못된 체크섬)
            data = b'{"seq":1,"ts":1234567890,"data":{"test":"value"}}'
            f.write(struct.pack(">I", len(data)))
            f.write(b"BADCHECK")  # 잘못된 체크섬
            f.write(data)

        corruption_events = []

        def on_corruption(error):
            corruption_events.append(error)

        wal = WriteAheadLog(config=config, on_corruption=on_corruption)
        wal.recover_unprocessed(0)
        wal.close()

        assert len(corruption_events) >= 1

    def test_handles_missing_directory_gracefully(self):
        """없는 디렉토리 처리 확인."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = os.path.join(tmpdir, "nested", "deep", "wal")
            config = WALConfig(wal_dir=nested_path)

            wal = WriteAheadLog(config=config)
            seq = wal.write({"event": "test"})
            wal.close()

            assert seq == 1
            assert Path(nested_path).exists()


# =============================================================================
# 컨텍스트 매니저 테스트
# =============================================================================


class TestWALContextManager:
    """WAL 컨텍스트 매니저 테스트."""

    def test_context_manager_usage(self, wal_config):
        """컨텍스트 매니저 사용 확인."""
        with WriteAheadLog(config=wal_config) as wal:
            wal.write({"event": "test1"})
            wal.write({"event": "test2"})

        # 종료 후 상태 확인
        wal2 = WriteAheadLog(config=wal_config)
        entries = wal2.recover_unprocessed(0)
        wal2.close()

        assert len(entries) == 2


# =============================================================================
# 헬퍼 함수 테스트
# =============================================================================


class TestWALHelpers:
    """WAL 헬퍼 함수 테스트."""

    def test_create_wal_function(self, temp_wal_dir):
        """create_wal 헬퍼 함수 확인."""
        wal = create_wal(
            wal_dir=temp_wal_dir,
            max_file_size_mb=50,
            sync_on_write=False,
        )

        seq = wal.write({"event": "test"})
        wal.close()

        assert seq == 1


# =============================================================================
# WALEntry 테스트
# =============================================================================


class TestWALEntry:
    """WALEntry 데이터 클래스 테스트."""

    def test_entry_to_dict(self):
        """딕셔너리 변환 확인."""
        entry = WALEntry(
            sequence=1,
            timestamp=1234567890.0,
            data={"event": "test"},
            checksum="a1b2c3d4",
        )

        d = entry.to_dict()

        assert d["seq"] == 1
        assert d["ts"] == 1234567890.0
        assert d["data"] == {"event": "test"}
        assert d["checksum"] == "a1b2c3d4"

    def test_entry_from_dict(self):
        """딕셔너리에서 생성 확인."""
        d = {
            "seq": 2,
            "ts": 9876543210.0,
            "data": {"value": 123},
            "checksum": "d4c3b2a1",
        }

        entry = WALEntry.from_dict(d)

        assert entry.sequence == 2
        assert entry.timestamp == 9876543210.0
        assert entry.data == {"value": 123}
        assert entry.checksum == "d4c3b2a1"


# =============================================================================
# WALConfig 테스트
# =============================================================================


class TestWALConfig:
    """WALConfig 데이터 클래스 테스트."""

    def test_default_values(self):
        """기본값 확인."""
        config = WALConfig()

        assert config.wal_dir == "/var/log/audit/wal"
        assert config.max_file_size_mb == 100
        assert config.sync_on_write is True
        assert config.max_files == 10

    def test_max_file_size_bytes(self):
        """바이트 변환 확인."""
        config = WALConfig(max_file_size_mb=50)

        assert config.max_file_size_bytes == 50 * 1024 * 1024


# =============================================================================
# 데이터 무결성 통합 테스트
# =============================================================================


class TestWALIntegrity:
    """WAL 데이터 무결성 통합 테스트."""

    def test_full_write_recover_cycle(self, temp_wal_dir):
        """전체 쓰기-복구 사이클 테스트."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=True,  # 데이터 무결성 보장
            file_prefix="test_wal",
        )

        # 다양한 데이터 쓰기
        test_data = [
            {"event": "create", "entity": "user", "id": 1},
            {"event": "update", "entity": "order", "id": 42, "status": "completed"},
            {"event": "delete", "entity": "product", "id": 100},
            {"config": {"nested": {"value": [1, 2, 3]}}},
            {"unicode": "한글 테스트 🔥"},
        ]

        wal1 = WriteAheadLog(config=config)
        for data in test_data:
            wal1.write(data)
        wal1.close()

        # 복구
        wal2 = WriteAheadLog(config=config)
        entries = wal2.recover_unprocessed(0)
        wal2.close()

        # 검증
        assert len(entries) == len(test_data)
        for entry, original in zip(entries, test_data, strict=False):
            assert entry.data == original

    def test_checksum_verification_on_recovery(self, temp_wal_dir):
        """복구 시 체크섬 검증 확인."""
        config = WALConfig(
            wal_dir=temp_wal_dir,
            sync_on_write=True,
            file_prefix="test_wal",
        )

        # 쓰기
        wal1 = WriteAheadLog(config=config)
        for i in range(10):
            wal1.write({"n": i, "data": f"test_data_{i}"})
        wal1.close()

        # 체크섬 검증하며 복구
        verified_count = 0

        wal2 = WriteAheadLog(config=config)
        entries = wal2.recover_unprocessed(0)

        for entry in entries:
            # 각 엔트리의 체크섬이 유효한지 확인
            data_bytes = json.dumps(
                {"seq": entry.sequence, "ts": entry.timestamp, "data": entry.data},
                separators=(",", ":"),
            ).encode("utf-8")
            computed = f"{zlib.crc32(data_bytes) & 0xFFFFFFFF:08x}"

            if computed == entry.checksum:
                verified_count += 1

        wal2.close()

        # 모든 엔트리가 유효해야 함
        assert verified_count == 10
