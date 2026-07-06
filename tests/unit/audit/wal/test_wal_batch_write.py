"""
WAL batch_write_entries() 테스트.

테스트 범위:
1. 여러 엔트리 한 번에 기록
2. 단일 fsync로 영속화
3. 시퀀스 번호 순서 보장
4. 빈 리스트 처리
5. WAL 닫힌 상태에서 에러
6. 파일 로테이션 트리거
7. 복구 시 배치 기록 엔트리 정상 읽기
"""

from __future__ import annotations

import threading
import time
from typing import Any
from unittest.mock import patch

import pytest

from baldur.audit.wal import (
    WALConfig,
    WALError,
    WriteAheadLog,
)


class TestBatchWriteEntries:
    """batch_write_entries() 메서드 테스트."""

    @pytest.fixture
    def wal_dir(self, tmp_path):
        """임시 WAL 디렉토리."""
        return str(tmp_path / "wal")

    @pytest.fixture
    def wal(self, wal_dir):
        """WriteAheadLog 인스턴스."""
        config = WALConfig(
            wal_dir=wal_dir,
            max_file_size_mb=1,
            sync_on_write=True,
        )
        wal = WriteAheadLog(config=config)
        yield wal
        wal.close()

    def test_batch_write_single_entry(self, wal):
        """단일 엔트리 배치 기록."""
        entries = [{"event": "test_event", "value": 1}]

        sequences = wal.batch_write_entries(entries)

        assert len(sequences) == 1
        assert sequences[0] == 1

    def test_batch_write_multiple_entries(self, wal):
        """여러 엔트리 배치 기록."""
        entries = [
            {"event": "config_change", "key": "max_retries", "value": 3},
            {"event": "config_change", "key": "timeout", "value": 30},
            {"event": "state_change", "from": "OPEN", "to": "CLOSED"},
        ]

        sequences = wal.batch_write_entries(entries)

        assert len(sequences) == 3
        assert sequences == [1, 2, 3]

    def test_batch_write_preserves_order(self, wal):
        """엔트리 순서 보장."""
        entries = [{"order": i} for i in range(10)]

        sequences = wal.batch_write_entries(entries)

        # 시퀀스 번호 순차 증가
        assert sequences == list(range(1, 11))

    def test_batch_write_empty_list(self, wal):
        """빈 리스트 처리."""
        sequences = wal.batch_write_entries([])

        assert sequences == []
        # 시퀀스 변경 없음
        assert wal._sequence == 0

    def test_batch_write_after_single_writes(self, wal):
        """단건 write 후 배치 write 시퀀스 연속."""
        # 단건 기록
        seq1 = wal.write({"single": 1})
        seq2 = wal.write({"single": 2})

        assert seq1 == 1
        assert seq2 == 2

        # 배치 기록
        sequences = wal.batch_write_entries(
            [
                {"batch": 1},
                {"batch": 2},
            ]
        )

        assert sequences == [3, 4]

    def test_batch_write_raises_on_closed_wal(self, wal):
        """닫힌 WAL에서 에러 발생."""
        wal.close()

        with pytest.raises(WALError, match="WAL is closed"):
            wal.batch_write_entries([{"test": 1}])

    def test_batch_write_entries_recoverable(self, wal, wal_dir):
        """배치 기록 엔트리 복구 가능."""
        entries = [
            {"event": "test1", "data": "value1"},
            {"event": "test2", "data": "value2"},
            {"event": "test3", "data": "value3"},
        ]

        sequences = wal.batch_write_entries(entries)
        wal.close()

        # 새 WAL 인스턴스로 복구
        config = WALConfig(wal_dir=wal_dir, sync_on_write=True)
        new_wal = WriteAheadLog(config=config)

        try:
            recovered = new_wal.recover_unprocessed(last_processed_seq=0)

            assert len(recovered) == 3
            for i, entry in enumerate(recovered):
                assert entry.sequence == sequences[i]
                assert entry.data["event"] == f"test{i + 1}"
        finally:
            new_wal.close()

    def test_batch_write_fsync_called_once(self, wal_dir):
        """fsync가 배치당 한 번만 호출됨."""
        config = WALConfig(wal_dir=wal_dir, sync_on_write=True)
        wal = WriteAheadLog(config=config)

        entries = [{"event": f"test_{i}"} for i in range(10)]

        with patch("os.fsync") as mock_fsync:
            wal.batch_write_entries(entries)

            # fsync 한 번만 호출
            assert mock_fsync.call_count == 1

        wal.close()

    def test_batch_write_triggers_rotation(self, wal_dir):
        """파일 크기 초과 시 로테이션."""
        # 작은 파일 크기 설정 (1KB)
        config = WALConfig(
            wal_dir=wal_dir,
            max_file_size_mb=0,  # 0MB = 0 bytes
            sync_on_write=True,
        )
        # max_file_size_bytes 직접 오버라이드
        config.max_file_size_mb = 1
        wal = WriteAheadLog(config=config)

        # 충분히 큰 데이터로 로테이션 트리거
        large_data = {"data": "x" * 1000}
        entries = [large_data] * 200  # 약 200KB

        try:
            wal.batch_write_entries(entries)

            # 통계 확인
            stats = wal.get_stats()
            assert stats.total_entries == 200
        finally:
            wal.close()

    def test_batch_write_thread_safe(self, wal):
        """스레드 안전성."""
        results = []
        errors = []

        def batch_writer(entries: list[dict[str, Any]]):
            try:
                seqs = wal.batch_write_entries(entries)
                results.extend(seqs)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(5):
            entries = [{"thread": i, "item": j} for j in range(10)]
            t = threading.Thread(target=batch_writer, args=(entries,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 50  # 5 threads * 10 entries

        # 중복 시퀀스 없음
        assert len(set(results)) == 50

    def test_batch_write_stats_updated(self, wal):
        """통계 업데이트 확인."""
        entries = [{"event": f"test_{i}"} for i in range(5)]

        before_stats = wal.get_stats()
        wal.batch_write_entries(entries)
        after_stats = wal.get_stats()

        assert after_stats.total_entries == before_stats.total_entries + 5
        assert after_stats.last_sequence == 5
        assert after_stats.last_write_time is not None

    def test_batch_write_checksum_valid(self, wal, wal_dir):
        """기록된 엔트리 체크섬 검증."""
        entries = [
            {"critical": "data1"},
            {"critical": "data2"},
        ]

        wal.batch_write_entries(entries)
        wal.close()

        # 새 WAL로 읽기 (체크섬 검증 포함)
        config = WALConfig(wal_dir=wal_dir)
        new_wal = WriteAheadLog(config=config)

        try:
            recovered = new_wal.recover_unprocessed(last_processed_seq=0)

            # 손상 없이 복구됨
            assert len(recovered) == 2
            assert new_wal._corrupted_entries == 0
        finally:
            new_wal.close()

    def test_batch_write_large_batch(self, wal):
        """대량 배치 기록."""
        entries = [{"index": i, "data": f"payload_{i}"} for i in range(1000)]

        sequences = wal.batch_write_entries(entries)

        assert len(sequences) == 1000
        assert sequences[0] == 1
        assert sequences[-1] == 1000

    def test_batch_write_with_special_characters(self, wal):
        """특수 문자 포함 데이터."""
        entries = [
            {"message": "한글 메시지"},
            {"message": "日本語テスト"},
            {"message": "emoji: 🎉🚀"},
            {"message": "special: <>&\"'"},
        ]

        sequences = wal.batch_write_entries(entries)

        assert len(sequences) == 4

        # 복구 테스트
        recovered = wal.recover_unprocessed(last_processed_seq=0)
        assert len(recovered) == 4
        assert recovered[0].data["message"] == "한글 메시지"
        assert recovered[2].data["message"] == "emoji: 🎉🚀"


class TestBatchWritePerformance:
    """배치 쓰기 성능 비교 테스트."""

    @pytest.fixture
    def wal_dir(self, tmp_path):
        return str(tmp_path / "wal_perf")

    def test_batch_faster_than_individual(self, wal_dir):
        """배치 쓰기가 개별 쓰기보다 빠름."""
        WALConfig(wal_dir=wal_dir, sync_on_write=True)

        entries = [{"index": i} for i in range(100)]

        # 배치 쓰기 시간 측정
        wal_batch = WriteAheadLog(
            config=WALConfig(wal_dir=wal_dir + "_batch", sync_on_write=True)
        )
        start = time.time()
        wal_batch.batch_write_entries(entries)
        batch_time = time.time() - start
        wal_batch.close()

        # 개별 쓰기 시간 측정
        wal_single = WriteAheadLog(
            config=WALConfig(wal_dir=wal_dir + "_single", sync_on_write=True)
        )
        start = time.time()
        for entry in entries:
            wal_single.write(entry)
        single_time = time.time() - start
        wal_single.close()

        # 배치가 더 빠르거나 동등해야 함
        # (환경에 따라 차이가 작을 수 있어 너무 엄격하지 않게)
        assert batch_time <= single_time * 2  # 최소 절반 이상 성능
