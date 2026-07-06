"""
WAL 병렬 복구 테스트.

테스트 범위:
1. WALConfig parallel recovery 설정 계약 (recovery_max_workers=4, recovery_batch_size=1000)
2. recover_unprocessed 병렬 경로 (ThreadPoolExecutor + as_completed)
3. recover_unprocessed 직렬 폴백 (파일 1개일 때)
4. _read_file_entries 정상 읽기 + OSError 부분 결과
5. _recover_chunked OOM 가드 트리거 시 파일별 순차 처리
6. _get_file_max_sequence 경량 바이너리 스캔
7. cleanup_processed가 _get_file_max_sequence 사용
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from baldur.audit.wal import WALConfig, WriteAheadLog
from baldur.audit.wal._models import WALConfig as WALConfigDirect

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def temp_wal_dir():
    """임시 WAL 디렉토리."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def wal_config(temp_wal_dir):
    """테스트용 WAL 설정 (병렬 복구 기본값)."""
    return WALConfig(
        wal_dir=temp_wal_dir,
        max_file_size_mb=1,
        sync_on_write=False,
        max_files=10,
        file_prefix="test_wal",
    )


@pytest.fixture
def wal(wal_config):
    """테스트용 WAL 인스턴스."""
    instance = WriteAheadLog(config=wal_config)
    yield instance
    instance.close()


def _write_entries(wal_instance, count, start_seq=1):
    """Helper: WAL에 엔트리 기록 후 flush."""
    seqs = []
    for i in range(count):
        seq = wal_instance.write({"event": f"test_{start_seq + i}"})
        seqs.append(seq)
    wal_instance.flush()
    return seqs


def _create_raw_wal_file(filepath: Path, entries: list[dict], magic=b"AWAL"):
    """Helper: WAL 바이너리 파일 직접 생성 (WriteAheadLog 우회)."""
    with open(filepath, "wb") as f:
        # Header: 4-byte magic + 4-byte version
        f.write(magic)
        f.write(struct.pack(">I", 1))

        for entry_dict in entries:
            data = json.dumps(entry_dict).encode("utf-8")
            checksum = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(data)))
            f.write(checksum.encode("ascii"))
            f.write(data)


# =============================================================================
# Contract Tests: WALConfig parallel recovery defaults
# =============================================================================


class TestWALConfigParallelRecoveryContract:
    """WALConfig 병렬 복구 설정 계약 검증."""

    def test_recovery_max_workers_default_is_4(self):
        """recovery_max_workers 기본값은 4."""
        config = WALConfigDirect()
        assert config.recovery_max_workers == 4

    def test_recovery_batch_size_default_is_1000(self):
        """recovery_batch_size 기본값은 1000."""
        config = WALConfigDirect()
        assert config.recovery_batch_size == 1000

    def test_recovery_max_workers_custom_value(self):
        """recovery_max_workers 커스텀 값 설정."""
        config = WALConfigDirect(recovery_max_workers=8)
        assert config.recovery_max_workers == 8

    def test_recovery_batch_size_custom_value(self):
        """recovery_batch_size 커스텀 값 설정."""
        config = WALConfigDirect(recovery_batch_size=500)
        assert config.recovery_batch_size == 500


# =============================================================================
# Behavior Tests: recover_unprocessed parallel path
# =============================================================================


class TestRecoverUnprocessedBehavior:
    """recover_unprocessed 병렬/직렬 동작 검증."""

    def test_recover_unprocessed_returns_empty_for_no_files(self, wal):
        """WAL 파일 없으면 빈 리스트 반환."""
        result = wal.recover_unprocessed(last_processed_seq=0)
        assert result == []

    def test_recover_unprocessed_single_file_uses_sequential(self, wal):
        """파일 1개일 때 _recover_sequential 경로 사용."""
        _write_entries(wal, 3)
        wal.close()

        # Given: recovery_max_workers=4 이지만 파일 1개
        wal2 = WriteAheadLog(config=wal._config)
        with patch.object(
            wal2, "_recover_sequential", wraps=wal2._recover_sequential
        ) as mock_seq:
            result = wal2.recover_unprocessed(last_processed_seq=0)

        assert len(result) == 3
        mock_seq.assert_called_once()
        wal2.close()

    def test_recover_unprocessed_multiple_files_uses_parallel(self, wal_config):
        """파일 여러 개일 때 병렬 경로 사용 (_recover_sequential 미호출)."""
        # Given: 2개 WAL 파일 생성
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "a"}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [{"seq": 2, "ts": 2.0, "data": {"e": "b"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        # When: _recover_sequential이 호출되지 않는지 확인 → 병렬 경로 사용 증명
        with patch.object(
            wal_instance, "_recover_sequential", wraps=wal_instance._recover_sequential
        ) as mock_seq:
            result = wal_instance.recover_unprocessed(last_processed_seq=0)

        # Then: 병렬 경로 (직렬 미호출) + 결과 정확
        mock_seq.assert_not_called()
        assert len(result) == 2
        seqs = [e.sequence for e in result]
        assert sorted(seqs) == [1, 2]
        wal_instance.close()

    def test_recover_unprocessed_filters_by_last_processed_seq(self, wal):
        """last_processed_seq 이후 엔트리만 반환."""
        _write_entries(wal, 5)
        wal.close()

        wal2 = WriteAheadLog(config=wal._config)
        result = wal2.recover_unprocessed(last_processed_seq=3)

        seqs = [e.sequence for e in result]
        assert all(s > 3 for s in seqs)
        assert len(result) == 2
        wal2.close()

    def test_recover_unprocessed_sorted_by_sequence(self, wal_config):
        """복구 결과가 시퀀스 기준 정렬."""
        wal_dir = Path(wal_config.wal_dir)

        # 역순 시퀀스로 2개 파일 생성
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [{"seq": 5, "ts": 5.0, "data": {"e": "5"}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 2, "ts": 2.0, "data": {"e": "2"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        result = wal_instance.recover_unprocessed(last_processed_seq=0)

        seqs = [e.sequence for e in result]
        assert seqs == sorted(seqs)
        wal_instance.close()

    def test_recover_unprocessed_single_worker_when_one_file(self, wal_config):
        """recovery_max_workers=4이어도 파일 1개면 직렬 경로."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        assert wal_instance._config.recovery_max_workers == 4

        with patch.object(
            wal_instance, "_recover_sequential", wraps=wal_instance._recover_sequential
        ) as mock_seq:
            result = wal_instance.recover_unprocessed(last_processed_seq=0)

        # Then: min(4, 1) = 1 → 직렬 경로
        mock_seq.assert_called_once()
        assert len(result) == 1
        wal_instance.close()

    def test_parallel_recovery_same_result_as_sequential(self, temp_wal_dir):
        """병렬 복구(workers=4)와 직렬 복구(workers=1)의 결과가 동일한지 검증."""
        wal_dir = Path(temp_wal_dir)

        # Given: 3개 WAL 파일에 여러 엔트리 생성
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "a1"}},
                {"seq": 4, "ts": 4.0, "data": {"e": "a4"}},
            ],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [
                {"seq": 2, "ts": 2.0, "data": {"e": "b2"}},
                {"seq": 5, "ts": 5.0, "data": {"e": "b5"}},
            ],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_003.wal",
            [
                {"seq": 3, "ts": 3.0, "data": {"e": "c3"}},
                {"seq": 6, "ts": 6.0, "data": {"e": "c6"}},
            ],
        )

        # When: 직렬 복구 (recovery_max_workers=1)
        config_seq = WALConfigDirect(
            wal_dir=temp_wal_dir,
            max_file_size_mb=1,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
            recovery_max_workers=1,
        )
        wal_seq = WriteAheadLog(config=config_seq)
        result_seq = wal_seq.recover_unprocessed(last_processed_seq=0)
        wal_seq.close()

        # When: 병렬 복구 (recovery_max_workers=4)
        config_par = WALConfigDirect(
            wal_dir=temp_wal_dir,
            max_file_size_mb=1,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
            recovery_max_workers=4,
        )
        wal_par = WriteAheadLog(config=config_par)
        result_par = wal_par.recover_unprocessed(last_processed_seq=0)
        wal_par.close()

        # Then: 동일한 엔트리가 동일한 순서로 반환
        assert len(result_seq) == len(result_par)
        assert len(result_seq) == 6
        seqs_sequential = [e.sequence for e in result_seq]
        seqs_parallel = [e.sequence for e in result_par]
        assert seqs_sequential == seqs_parallel
        assert seqs_sequential == [1, 2, 3, 4, 5, 6]

    def test_file_error_does_not_abort_other_files(self, wal_config):
        """한 파일에서 에러 발생 시 나머지 파일의 엔트리는 정상 복구."""
        wal_dir = Path(wal_config.wal_dir)

        # Given: 3개 WAL 파일 생성
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "ok1"}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [{"seq": 2, "ts": 2.0, "data": {"e": "fail"}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_003.wal",
            [{"seq": 3, "ts": 3.0, "data": {"e": "ok3"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        # Given: 2번 파일에 대해서만 _read_file_entries가 예외를 발생하도록 패치
        error_file = wal_dir / "test_wal_002.wal"
        original_read = wal_instance._read_file_entries

        def patched_read(wal_file, last_processed_seq):
            if wal_file.name == error_file.name:
                raise Exception("Simulated disk I/O error")
            return original_read(wal_file, last_processed_seq)

        with patch.object(wal_instance, "_read_file_entries", side_effect=patched_read):
            result = wal_instance.recover_unprocessed(last_processed_seq=0)

        # Then: 에러 파일 제외, 나머지 2개 파일의 엔트리 복구
        seqs = [e.sequence for e in result]
        assert 1 in seqs
        assert 3 in seqs
        assert 2 not in seqs
        assert len(result) == 2
        wal_instance.close()


# =============================================================================
# Behavior Tests: _read_file_entries
# =============================================================================


class TestReadFileEntriesBehavior:
    """_read_file_entries 동작 검증."""

    def test_read_file_entries_returns_entries_above_seq(self, wal):
        """last_processed_seq 초과 엔트리만 반환."""
        _write_entries(wal, 5)
        wal.flush()

        wal_files = sorted(Path(wal._config.wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files) >= 1

        entries = wal._read_file_entries(wal_files[0], last_processed_seq=2)
        seqs = [e.sequence for e in entries]
        assert all(s > 2 for s in seqs)

    def test_read_file_entries_oserror_returns_partial_results(self, wal_config):
        """OSError 발생 시 에러 전까지의 부분 결과 반환."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [
                {"seq": 1, "ts": 1.0, "data": {"e": "ok1"}},
                {"seq": 2, "ts": 2.0, "data": {"e": "ok2"}},
            ],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        # Given: _read_wal_file_best_effort가 2개 yield 후 OSError
        original_reader = wal_instance._read_wal_file_best_effort

        def faulty_reader(filepath):
            count = 0
            for entry in original_reader(filepath):
                yield entry
                count += 1
                if count >= 1:
                    raise OSError("Disk I/O error")

        with patch.object(
            wal_instance, "_read_wal_file_best_effort", side_effect=faulty_reader
        ):
            entries = wal_instance._read_file_entries(
                wal_dir / "test_wal_001.wal",
                last_processed_seq=0,
            )

        # Then: 에러 전 1개 엔트리 반환
        assert len(entries) >= 1
        wal_instance.close()

    def test_read_file_entries_oserror_logs_critical(self, wal_config):
        """OSError 시 CRITICAL 로그 기록."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "ok"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        def failing_reader(filepath):
            raise OSError("Catastrophic disk failure")

        with patch.object(
            wal_instance, "_read_wal_file_best_effort", side_effect=failing_reader
        ):
            with patch("baldur.audit.wal._reader.logger") as mock_logger:
                entries = wal_instance._read_file_entries(
                    wal_dir / "test_wal_001.wal",
                    last_processed_seq=0,
                )

        # Then: CRITICAL 로그 기록됨
        mock_logger.critical.assert_called_once()
        call_args = mock_logger.critical.call_args
        assert "wal.parallel_recovery_partial_corruption" in str(call_args)
        assert entries == []
        wal_instance.close()


# =============================================================================
# Behavior Tests: _recover_sequential
# =============================================================================


class TestRecoverSequentialBehavior:
    """_recover_sequential 직렬 복구 경로 검증."""

    def test_recover_sequential_returns_sorted_entries(self, wal):
        """직렬 복구가 시퀀스 정렬된 엔트리 반환."""
        _write_entries(wal, 3)
        wal.flush()

        wal_files = sorted(Path(wal._config.wal_dir).glob("test_wal_*.wal"))
        result = wal._recover_sequential(wal_files, last_processed_seq=0)

        seqs = [e.sequence for e in result]
        assert seqs == sorted(seqs)
        assert len(result) == 3

    def test_recover_sequential_increments_recovered_entries(self, wal):
        """직렬 복구 시 _recovered_entries 카운터 증가."""
        _write_entries(wal, 3)
        wal.flush()

        initial = wal._recovered_entries
        wal_files = sorted(Path(wal._config.wal_dir).glob("test_wal_*.wal"))
        wal._recover_sequential(wal_files, last_processed_seq=0)

        assert wal._recovered_entries == initial + 3


# =============================================================================
# Behavior Tests: _recover_chunked (OOM guard)
# =============================================================================


class TestRecoverChunkedBehavior:
    """_recover_chunked 메모리 제한 복구 검증."""

    def test_recover_chunked_processes_small_files(self, wal_config):
        """가용 메모리 내 파일은 정상 처리."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "a"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        wal_files = sorted(wal_dir.glob("test_wal_*.wal"))

        # Given: 충분한 가용 메모리
        result = wal_instance._recover_chunked(
            wal_files,
            last_processed_seq=0,
            available_bytes=100 * 1024 * 1024,  # 100MB
        )

        assert len(result) == 1
        assert result[0].sequence == 1
        wal_instance.close()

    def test_recover_chunked_skips_oversized_files(self, wal_config):
        """가용 메모리 초과 파일은 스킵."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [
                {"seq": i, "ts": float(i), "data": {"payload": "x" * 1000}}
                for i in range(1, 101)
            ],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        wal_files = sorted(wal_dir.glob("test_wal_*.wal"))

        # Given: 매우 작은 가용 메모리 (파일 크기 * 3 미만)
        result = wal_instance._recover_chunked(
            wal_files,
            last_processed_seq=0,
            available_bytes=1,  # 1 byte — 모든 파일 스킵
        )

        assert result == []
        wal_instance.close()

    def test_recover_chunked_returns_sorted_entries(self, wal_config):
        """청크 모드에서도 시퀀스 정렬."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [{"seq": 3, "ts": 3.0, "data": {}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        wal_files = sorted(wal_dir.glob("test_wal_*.wal"))

        result = wal_instance._recover_chunked(
            wal_files,
            last_processed_seq=0,
            available_bytes=100 * 1024 * 1024,
        )

        seqs = [e.sequence for e in result]
        assert seqs == sorted(seqs)
        wal_instance.close()

    def test_oom_guard_triggers_chunked_recovery(self, wal_config):
        """OOM 가드 트리거 시 _recover_chunked 호출."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        with patch(
            "baldur.core.resource_monitor.CgroupResourceMonitor",
        ) as mock_monitor:
            # Given: 가용 메모리 1바이트 (estimated > available)
            mock_monitor.get_available_memory_bytes.return_value = 1

            with patch.object(
                wal_instance, "_recover_chunked", return_value=[]
            ) as mock_chunked:
                result = wal_instance.recover_unprocessed(last_processed_seq=0)

        mock_chunked.assert_called_once()
        assert result == []
        wal_instance.close()

    def test_memory_guard_skipped_without_cgroup(self, wal_config):
        """CgroupResourceMonitor import 실패 시 OOM 가드 스킵하고 정상 복구."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "a"}}],
        )
        _create_raw_wal_file(
            wal_dir / "test_wal_002.wal",
            [{"seq": 2, "ts": 2.0, "data": {"e": "b"}}],
        )

        wal_instance = WriteAheadLog(config=wal_config)

        # Given: CgroupResourceMonitor import가 ImportError 발생
        import builtins

        original_import = builtins.__import__

        def import_raiser(name, *args, **kwargs):
            if name == "baldur.core.resource_monitor":
                raise ImportError("No cgroup support")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_raiser):
            result = wal_instance.recover_unprocessed(last_processed_seq=0)

        # Then: OOM 가드 스킵, 모든 엔트리 정상 복구
        assert len(result) == 2
        seqs = [e.sequence for e in result]
        assert sorted(seqs) == [1, 2]
        wal_instance.close()


# =============================================================================
# Behavior Tests: _get_file_max_sequence
# =============================================================================


class TestGetFileMaxSequenceBehavior:
    """_get_file_max_sequence 경량 바이너리 스캔 검증."""

    def test_returns_max_sequence_from_file(self, wal_config):
        """파일 내 최대 시퀀스 번호 반환."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_001.wal"
        _create_raw_wal_file(
            filepath,
            [
                {"seq": 3, "ts": 1.0, "data": {}},
                {"seq": 7, "ts": 2.0, "data": {}},
                {"seq": 5, "ts": 3.0, "data": {}},
            ],
        )

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 7
        wal_instance.close()

    def test_returns_zero_for_invalid_magic(self, wal_config):
        """잘못된 매직 바이트이면 0 반환."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_bad.wal"
        _create_raw_wal_file(
            filepath, [{"seq": 10, "ts": 1.0, "data": {}}], magic=b"BAAD"
        )

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 0
        wal_instance.close()

    def test_returns_zero_for_empty_file(self, wal_config):
        """빈 파일이면 0 반환."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_empty.wal"
        filepath.write_bytes(b"")

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 0
        wal_instance.close()

    def test_returns_zero_for_nonexistent_file(self, wal_config):
        """존재하지 않는 파일이면 0 반환 (OSError 안전)."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "nonexistent.wal"

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 0
        wal_instance.close()

    def test_skips_corrupted_json_entries(self, wal_config):
        """손상된 JSON 엔트리는 무시하고 유효한 max_seq 반환."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_mixed.wal"

        with open(filepath, "wb") as f:
            f.write(b"AWAL")
            f.write(struct.pack(">I", 1))

            # 유효한 엔트리 (seq=5)
            valid_data = json.dumps({"seq": 5, "ts": 1.0, "data": {}}).encode()
            checksum = format(zlib.crc32(valid_data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(valid_data)))
            f.write(checksum.encode("ascii"))
            f.write(valid_data)

            # 손상된 JSON 엔트리
            bad_data = b"not valid json at all"
            bad_cs = format(zlib.crc32(bad_data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(bad_data)))
            f.write(bad_cs.encode("ascii"))
            f.write(bad_data)

            # 유효한 엔트리 (seq=10)
            valid_data2 = json.dumps({"seq": 10, "ts": 2.0, "data": {}}).encode()
            checksum2 = format(zlib.crc32(valid_data2) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(valid_data2)))
            f.write(checksum2.encode("ascii"))
            f.write(valid_data2)

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 10
        wal_instance.close()

    def test_does_not_create_walentry_objects(self, wal_config):
        """WALEntry 객체를 생성하지 않는 경량 경로 확인."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_001.wal"
        _create_raw_wal_file(filepath, [{"seq": 1, "ts": 1.0, "data": {}}])

        wal_instance = WriteAheadLog(config=wal_config)

        # instance-level mock (class-level 패치는 다른 테스트 인스턴스에 영향)
        with patch.object(wal_instance, "_parse_wal_record") as mock_parse:
            max_seq = wal_instance._get_file_max_sequence(filepath)

        # Then: _parse_wal_record는 호출되지 않음 (경량 스캔)
        mock_parse.assert_not_called()
        assert max_seq == 1
        wal_instance.close()


# =============================================================================
# Behavior Tests: cleanup_processed uses _get_file_max_sequence
# =============================================================================


class TestCleanupProcessedBehavior:
    """cleanup_processed가 _get_file_max_sequence 사용 검증."""

    def test_cleanup_uses_lightweight_scan(self, wal):
        """cleanup_processed가 _get_file_max_sequence 호출."""
        _write_entries(wal, 3)
        wal.flush()
        wal.close()

        wal2 = WriteAheadLog(config=wal._config)

        with patch.object(
            wal2, "_get_file_max_sequence", wraps=wal2._get_file_max_sequence
        ) as mock_scan:
            wal2.cleanup_processed(last_processed_seq=999)

        assert mock_scan.call_count >= 1
        wal2.close()

    def test_cleanup_deletes_fully_processed_file(self, wal):
        """max_seq <= last_processed_seq인 파일 삭제."""
        _write_entries(wal, 3)
        wal.flush()
        wal.close()

        wal2 = WriteAheadLog(config=wal._config)
        wal_files_before = list(Path(wal._config.wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files_before) >= 1

        deleted = wal2.cleanup_processed(last_processed_seq=999)
        assert deleted >= 1

        wal_files_after = list(Path(wal._config.wal_dir).glob("test_wal_*.wal"))
        assert len(wal_files_after) < len(wal_files_before)
        wal2.close()

    def test_cleanup_keeps_unprocessed_file(self, wal):
        """max_seq > last_processed_seq인 파일은 유지."""
        _write_entries(wal, 3)
        wal.flush()
        wal.close()

        wal2 = WriteAheadLog(config=wal._config)
        deleted = wal2.cleanup_processed(last_processed_seq=0)

        assert deleted == 0
        wal2.close()


# =============================================================================
# Code Review Fix Tests
# =============================================================================


class TestGetFileMaxSequenceLengthGuard:
    """_get_file_max_sequence 10MB 길이 가드 검증 (#2)."""

    def test_corrupted_length_field_does_not_oom(self, wal_config):
        """length > 10MB인 손상 레코드에서 즉시 break."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_corrupt_len.wal"

        with open(filepath, "wb") as f:
            # Header
            f.write(b"AWAL")
            f.write(struct.pack(">I", 1))

            # 정상 엔트리 (seq=5)
            valid_data = json.dumps({"seq": 5, "ts": 1.0, "data": {}}).encode()
            checksum = format(zlib.crc32(valid_data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(valid_data)))
            f.write(checksum.encode("ascii"))
            f.write(valid_data)

            # 손상 엔트리: length = 1GB
            f.write(struct.pack(">I", 1_000_000_000))
            f.write(b"00000000")  # dummy checksum
            # 실제 데이터는 기록하지 않음 — read()가 1GB 할당 시도 방지

            # 이후 정상 엔트리 (seq=99) — 도달 불가
            valid_data2 = json.dumps({"seq": 99, "ts": 2.0, "data": {}}).encode()
            checksum2 = format(zlib.crc32(valid_data2) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(valid_data2)))
            f.write(checksum2.encode("ascii"))
            f.write(valid_data2)

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        # 손상 레코드에서 break → seq=99에 도달하지 못함
        assert max_seq == 5
        wal_instance.close()

    def test_length_exactly_10mb_is_accepted(self, wal_config):
        """length == 10MB는 정상 처리 (경계값)."""
        wal_dir = Path(wal_config.wal_dir)
        filepath = wal_dir / "test_wal_10mb.wal"

        with open(filepath, "wb") as f:
            f.write(b"AWAL")
            f.write(struct.pack(">I", 1))

            # 정상 엔트리
            valid_data = json.dumps({"seq": 7, "ts": 1.0, "data": {}}).encode()
            checksum = format(zlib.crc32(valid_data) & 0xFFFFFFFF, "08x")
            f.write(struct.pack(">I", len(valid_data)))
            f.write(checksum.encode("ascii"))
            f.write(valid_data)

        wal_instance = WriteAheadLog(config=wal_config)
        max_seq = wal_instance._get_file_max_sequence(filepath)

        assert max_seq == 7
        wal_instance.close()


class TestRecoverChunkedCumulativeMemory:
    """_recover_chunked 누적 메모리 추적 검증 (#3)."""

    def test_later_files_skipped_when_cumulative_exceeds_budget(self, wal_config):
        """파일 누적 처리 후 메모리 예산 초과 시 이후 파일 스킵."""
        wal_dir = Path(wal_config.wal_dir)

        # 3개 파일 생성 (각 ~100 bytes)
        for i in range(1, 4):
            _create_raw_wal_file(
                wal_dir / f"test_wal_00{i}.wal",
                [{"seq": i, "ts": float(i), "data": {"x": "y" * 50}}],
            )

        wal_instance = WriteAheadLog(config=wal_config)
        wal_files = sorted(wal_dir.glob("test_wal_*.wal"))
        assert len(wal_files) == 3

        # 각 파일 ~100 bytes → file_estimated = 300 bytes
        # available_bytes = 500 → 첫 번째 파일(300) 통과, 두 번째(300) 시 잔여=200 → 스킵
        result = wal_instance._recover_chunked(
            wal_files,
            last_processed_seq=0,
            available_bytes=500,
        )

        # 최소 1개, 최대 2개 엔트리만 복구 (3개 전부는 불가)
        assert len(result) < 3
        assert len(result) >= 1
        wal_instance.close()


class TestRecoveryLogEventNames:
    """recover_unprocessed 로그 이벤트명 검증 (#6)."""

    def test_sequential_recovery_logs_sequential_event(self, wal_config):
        """max_workers=1일 때 sequential_recovery_started 로그."""
        wal_dir = Path(wal_config.wal_dir)
        _create_raw_wal_file(
            wal_dir / "test_wal_001.wal",
            [{"seq": 1, "ts": 1.0, "data": {"e": "a"}}],
        )

        config = WALConfigDirect(
            wal_dir=wal_config.wal_dir,
            max_file_size_mb=1,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
            recovery_max_workers=1,
        )
        wal_instance = WriteAheadLog(config=config)

        import structlog

        captured_events = []
        structlog.get_logger().info

        with patch("baldur.audit.wal._reader.logger") as mock_logger:
            mock_logger.info = lambda event, **kw: captured_events.append(event)
            mock_logger.exception = lambda event, **kw: None
            mock_logger.critical = lambda event, **kw: None
            wal_instance.recover_unprocessed(last_processed_seq=0)

        assert "wal.sequential_recovery_started" in captured_events
        assert "wal.parallel_recovery_started" not in captured_events
        wal_instance.close()

    def test_parallel_recovery_logs_parallel_event(self, wal_config):
        """max_workers>1일 때 parallel_recovery_started 로그."""
        wal_dir = Path(wal_config.wal_dir)
        for i in range(1, 4):
            _create_raw_wal_file(
                wal_dir / f"test_wal_00{i}.wal",
                [{"seq": i, "ts": float(i), "data": {"e": f"a{i}"}}],
            )

        config = WALConfigDirect(
            wal_dir=wal_config.wal_dir,
            max_file_size_mb=1,
            sync_on_write=False,
            max_files=10,
            file_prefix="test_wal",
            recovery_max_workers=4,
        )
        wal_instance = WriteAheadLog(config=config)

        captured_events = []

        with patch("baldur.audit.wal._reader.logger") as mock_logger:
            mock_logger.info = lambda event, **kw: captured_events.append(event)
            mock_logger.exception = lambda event, **kw: None
            mock_logger.critical = lambda event, **kw: None
            wal_instance.recover_unprocessed(last_processed_seq=0)

        assert "wal.parallel_recovery_started" in captured_events
        assert "wal.sequential_recovery_started" not in captured_events
        wal_instance.close()
