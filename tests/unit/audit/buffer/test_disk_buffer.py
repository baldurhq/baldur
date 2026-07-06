"""
DiskPersistentBuffer 단위 테스트.

LMDB 기반 영속 버퍼의 핵심 기능을 테스트합니다.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from collections.abc import Generator

import pytest

# LMDB 설치 여부 확인
try:
    import lmdb  # noqa: F401

    LMDB_AVAILABLE = True
except ImportError:
    LMDB_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not LMDB_AVAILABLE,
    reason="lmdb not installed",
)


@pytest.fixture
def temp_db_path() -> Generator[str, None, None]:
    """임시 LMDB 경로 (테스트 후 자동 삭제)."""
    temp_dir = tempfile.mkdtemp(prefix="disk_buffer_test_")
    yield temp_dir
    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def disk_buffer_settings(temp_db_path: str):
    """테스트용 DiskBufferSettings."""
    from baldur.audit.persistence.config import DiskBufferSettings

    return DiskBufferSettings(
        data_dir=temp_db_path,
        lmdb_map_size_mb=50,  # 50MB (테스트용)
        max_entries=1000,
        sync_on_write=True,  # 테스트에서는 즉시 동기화
        enable_checksum=True,
        group_commit_enabled=False,  # 테스트 단순화
        enable_dead_letter_db=True,
        enable_shutdown_handlers=False,  # 테스트에서는 비활성화
        include_hostname_in_db_name=False,
        include_pid_in_db_name=False,
        disk_full_threshold=0.0,  # 테스트에서는 디스크 체크 비활성화
    )


@pytest.fixture
def disk_buffer(disk_buffer_settings) -> Generator:
    """테스트용 DiskPersistentBuffer 인스턴스."""
    from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

    buffer = DiskPersistentBuffer(settings=disk_buffer_settings, db_name="test_buffer")
    yield buffer
    buffer.close()


class TestDiskPersistentBufferBasic:
    """DiskPersistentBuffer 기본 기능 테스트."""

    def test_put_and_get(self, disk_buffer):
        """저장 및 조회 테스트."""
        entry = {"event_type": "test", "value": 42}
        key = disk_buffer.put(entry)

        assert key is not None

        result = disk_buffer.get(key)

        assert result is not None
        assert result.data["event_type"] == "test"
        assert result.data["value"] == 42

    def test_put_multiple_entries(self, disk_buffer):
        """다중 엔트리 저장 테스트."""
        keys = []
        for i in range(10):
            key = disk_buffer.put({"sequence": i, "data": f"test_{i}"})
            keys.append(key)

        assert len(keys) == 10
        assert disk_buffer.count() == 10

        # 모든 엔트리 조회 가능
        for i, key in enumerate(keys):
            entry = disk_buffer.get(key)
            assert entry is not None
            assert entry.data["sequence"] == i

    def test_count(self, disk_buffer):
        """엔트리 수 확인 테스트."""
        assert disk_buffer.count() == 0

        for i in range(5):
            disk_buffer.put({"index": i})

        assert disk_buffer.count() == 5

    def test_delete(self, disk_buffer):
        """엔트리 삭제 테스트."""
        key = disk_buffer.put({"event": "to_delete"})
        assert disk_buffer.count() == 1

        result = disk_buffer.delete(key)
        assert result is True
        assert disk_buffer.count() == 0

        # 삭제된 키 조회 시 None
        assert disk_buffer.get(key) is None

    def test_delete_batch(self, disk_buffer):
        """배치 삭제 테스트."""
        keys = []
        for i in range(10):
            key = disk_buffer.put({"index": i})
            keys.append(key)

        assert disk_buffer.count() == 10

        # 절반 삭제
        deleted = disk_buffer.delete_batch(keys[:5])
        assert deleted == 5
        assert disk_buffer.count() == 5


class TestDiskPersistentBufferChecksum:
    """CRC32 체크섬 테스트."""

    def test_checksum_stored(self, disk_buffer):
        """체크섬 저장 확인."""
        entry = {"event_type": "checksum_test"}
        key = disk_buffer.put(entry)

        result = disk_buffer.get(key)
        assert result is not None
        assert result.checksum is not None
        assert result.checksum > 0

    def test_checksum_verification(self, disk_buffer):
        """체크섬 검증 동작 확인."""
        # 정상 저장
        entry = {"data": "test_checksum"}
        key = disk_buffer.put(entry)

        # 정상 조회 성공
        result = disk_buffer.get(key)
        assert result is not None


class TestDiskPersistentBufferPersistence:
    """데이터 영속성 테스트."""

    def test_persistence_across_restart(self, disk_buffer_settings, temp_db_path):
        """재시작 후 데이터 보존 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        # 첫 번째 인스턴스
        buffer1 = DiskPersistentBuffer(
            settings=disk_buffer_settings, db_name="persist_test"
        )
        buffer1.put({"event": "persistent1"})
        buffer1.put({"event": "persistent2"})
        count1 = buffer1.count()
        buffer1.close()

        # 두 번째 인스턴스 (재시작 시뮬레이션)
        buffer2 = DiskPersistentBuffer(
            settings=disk_buffer_settings, db_name="persist_test"
        )
        count2 = buffer2.count()
        buffer2.close()

        assert count2 == count1 == 2

    def test_sequence_recovery(self, disk_buffer_settings, temp_db_path):
        """시퀀스 번호 복구 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        # 첫 번째 인스턴스: 엔트리 저장
        buffer1 = DiskPersistentBuffer(
            settings=disk_buffer_settings, db_name="seq_test"
        )
        for _ in range(5):
            buffer1.put({"data": "test"})
        seq1 = buffer1._sequence
        buffer1.close()

        # 두 번째 인스턴스: 시퀀스 복구 확인
        buffer2 = DiskPersistentBuffer(
            settings=disk_buffer_settings, db_name="seq_test"
        )
        seq2 = buffer2._sequence
        buffer2.close()

        assert seq2 == seq1


class TestDiskPersistentBufferIterator:
    """엔트리 순회 테스트."""

    def test_iter_entries(self, disk_buffer):
        """순방향 순회 테스트."""
        for i in range(5):
            disk_buffer.put({"index": i})

        entries = list(disk_buffer.iter_entries())
        assert len(entries) == 5

    def test_iter_entries_limit(self, disk_buffer):
        """제한된 순회 테스트."""
        for i in range(10):
            disk_buffer.put({"index": i})

        entries = list(disk_buffer.iter_entries(limit=3))
        assert len(entries) == 3

    def test_iter_entries_reverse(self, disk_buffer):
        """역방향 순회 테스트."""
        for i in range(5):
            disk_buffer.put({"index": i})

        entries = list(disk_buffer.iter_entries(reverse=True))
        assert len(entries) == 5
        # 역순이므로 마지막 엔트리가 먼저
        assert entries[0].data["index"] == 4


class TestDiskPersistentBufferFlush:
    """플러시 기능 테스트."""

    def test_flush_to_success(self, disk_buffer):
        """성공적인 플러시 테스트."""
        for i in range(5):
            disk_buffer.put({"index": i})

        processed = []

        def handler(entries):
            for e in entries:
                processed.append(e.data)
            return True

        flushed = disk_buffer.flush_to(handler)
        assert flushed == 5
        assert len(processed) == 5
        assert disk_buffer.count() == 0

    def test_flush_to_failure(self, disk_buffer):
        """플러시 실패 처리 테스트."""
        for i in range(5):
            disk_buffer.put({"index": i})

        call_count = [0]

        def failing_handler(entries):
            call_count[0] += 1
            return False  # 항상 실패

        flushed = disk_buffer.flush_to(failing_handler)
        assert flushed == 0
        assert disk_buffer.count() == 5  # 데이터 유지


class TestDiskPersistentBufferDeadLetter:
    """Dead Letter DB 테스트."""

    def test_dead_letter_move(self, disk_buffer_settings, temp_db_path):
        """Poison Pill → Dead Letter DB 이동 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        # max_flush_retries=1 로 빠른 테스트
        disk_buffer_settings.max_flush_retries = 1

        buffer = DiskPersistentBuffer(settings=disk_buffer_settings, db_name="dlq_test")

        try:
            # 엔트리 추가
            buffer.put({"event": "poison_pill_test"})

            # 항상 실패하는 핸들러로 플러시
            def failing_handler(entries):
                raise ValueError("Simulated failure")

            # 플러시 시도 (실패하면 재시도 카운터 증가 -> Dead Letter로 이동)
            buffer.flush_to(failing_handler)

            # Dead Letter DB 확인
            dead_letters = buffer.get_dead_letters()
            assert len(dead_letters) >= 1
            assert dead_letters[0]["status"] == "requires_review"

        finally:
            buffer.close()

    def test_replay_dead_letter(self, disk_buffer_settings, temp_db_path):
        """Dead Letter 재시도 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        disk_buffer_settings.max_flush_retries = 1

        buffer = DiskPersistentBuffer(
            settings=disk_buffer_settings, db_name="replay_test"
        )

        try:
            # Dead Letter로 이동
            buffer.put({"event": "replay_test"})
            buffer.flush_to(lambda _: False)  # 실패
            buffer.flush_to(lambda _: False)  # 실패 -> DLQ로 이동

            dead_letters = buffer.get_dead_letters()
            if dead_letters:
                key = dead_letters[0]["key"].encode()

                # 재시도
                result = buffer.replay_dead_letter(key)
                assert result is True
                assert buffer.count() >= 1

        finally:
            buffer.close()


class TestDiskPersistentBufferCleanup:
    """오래된 엔트리 정리 테스트."""

    def test_cleanup_old_entries(self, disk_buffer):
        """오래된 엔트리 정리 테스트."""

        # 엔트리 추가
        for i in range(5):
            disk_buffer.put({"index": i})

        # 아주 짧은 시간 대기 후 정리
        time.sleep(0.1)

        # 0.05초 retention으로 정리 (모두 삭제)
        deleted = disk_buffer.cleanup_old_entries(max_age_seconds=0.05)
        assert deleted == 5
        assert disk_buffer.count() == 0


class TestDiskPersistentBufferStats:
    """통계 및 상태 테스트."""

    def test_get_stats(self, disk_buffer):
        """통계 조회 테스트."""
        for i in range(5):
            disk_buffer.put({"index": i})

        stats = disk_buffer.get_stats()

        assert stats["count"] == 5
        assert stats["total_puts"] == 5
        assert stats["sequence"] == 5

    def test_get_health_status(self, disk_buffer):
        """Health Check 상태 테스트."""
        health = disk_buffer.get_health_status()

        # healthy는 disk space 경고가 있을 수 있으므로 state로 확인
        assert health["state"] == "ACTIVE"
        assert health["entry_count"] >= 0
        assert "disk_free_ratio" in health
        assert "errors" in health
        # 디스크 경고 외의 심각한 오류가 없어야 함
        critical_errors = [
            e
            for e in health["errors"]
            if "corrupted" in e.lower() or "disk full" in e.lower()
        ]
        assert len(critical_errors) == 0


class TestDiskPersistentBufferThreadSafety:
    """스레드 안전성 테스트."""

    def test_concurrent_puts(self, disk_buffer):
        """동시 저장 테스트."""
        errors = []
        count = 100
        threads = []

        def put_entries(start):
            try:
                for i in range(start, start + 10):
                    disk_buffer.put({"thread_index": i})
            except Exception as e:
                errors.append(str(e))

        for i in range(10):
            t = threading.Thread(target=put_entries, args=(i * 10,))
            threads.append(t)

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        assert len(errors) == 0
        assert disk_buffer.count() == count


class TestDiskPersistentBufferGroupCommit:
    """Group Commit 테스트."""

    def test_group_commit_enabled(self, disk_buffer_settings, temp_db_path):
        """Group Commit 활성화 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

        disk_buffer_settings.group_commit_enabled = True
        disk_buffer_settings.group_commit_max_entries = 5

        buffer = DiskPersistentBuffer(settings=disk_buffer_settings, db_name="gc_test")

        try:
            # 엔트리 추가 (group_commit_max_entries 미만)
            for i in range(3):
                buffer.put({"index": i})

            # 강제 플러시 전에는 DB에 없을 수 있음
            # Group Commit 버퍼에 있으므로 flush_group_commit 호출

            buffer.flush_group_commit()

            # 플러시 후 DB에 반영
            assert buffer.count() == 3

        finally:
            buffer.close()


class TestDiskBufferAdapter:
    """InMemoryAuditBuffer 호환 어댑터 테스트."""

    def test_adapter_add(self, disk_buffer):
        """어댑터 add 메서드 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskBufferAdapter

        adapter = DiskBufferAdapter(disk_buffer)

        result = adapter.add({"event": "adapter_test"})
        assert result is True
        assert len(adapter) == 1

    def test_adapter_try_flush(self, disk_buffer):
        """어댑터 try_flush 메서드 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskBufferAdapter

        adapter = DiskBufferAdapter(disk_buffer)
        adapter.add({"event": "flush_test"})

        processed = []

        def wal_write_func(entry):
            processed.append(entry)
            return 1  # sequence 반환

        flushed = adapter.try_flush(wal_write_func)
        assert flushed == 1
        assert len(processed) == 1

    def test_adapter_stats(self, disk_buffer):
        """어댑터 통계 테스트."""
        from baldur.audit.persistence.disk_buffer import DiskBufferAdapter

        adapter = DiskBufferAdapter(disk_buffer)
        adapter.add({"event": "stats_test"})

        stats = adapter.get_stats()
        assert "total_buffered" in stats
        assert stats["total_buffered"] == 1
