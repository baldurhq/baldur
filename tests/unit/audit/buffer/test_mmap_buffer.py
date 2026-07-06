"""
MmapBuffer 단위 테스트.

mmap 기반 대안 버퍼의 기능을 테스트합니다.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Generator

import pytest


@pytest.fixture
def temp_mmap_path() -> Generator[str, None, None]:
    """임시 mmap 파일 경로."""
    temp_dir = tempfile.mkdtemp(prefix="mmap_buffer_test_")
    file_path = os.path.join(temp_dir, "mmap_buffer.dat")
    yield file_path
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mmap_buffer(temp_mmap_path) -> Generator:
    """테스트용 MmapBuffer 인스턴스."""
    from baldur.audit.persistence.mmap_buffer import MmapBuffer

    buffer = MmapBuffer(file_path=temp_mmap_path, size_mb=1)  # 1MB 테스트용
    yield buffer
    buffer.close()


class TestMmapBufferBasic:
    """MmapBuffer 기본 기능 테스트."""

    def test_put_and_iter(self, mmap_buffer):
        """저장 및 조회 테스트."""
        entry = {"event_type": "test", "value": 42}
        result = mmap_buffer.put(entry)
        assert result is True

        entries = mmap_buffer.iter_entries()
        assert len(entries) == 1
        assert entries[0]["event_type"] == "test"
        assert entries[0]["value"] == 42

    def test_put_multiple(self, mmap_buffer):
        """다중 엔트리 저장 테스트."""
        for i in range(5):
            mmap_buffer.put({"index": i})

        entries = mmap_buffer.iter_entries()
        assert len(entries) == 5

        for i, entry in enumerate(entries):
            assert entry["index"] == i

    def test_count(self, mmap_buffer):
        """엔트리 수 확인 테스트."""
        assert mmap_buffer.count() == 0

        mmap_buffer.put({"data": "test1"})
        mmap_buffer.put({"data": "test2"})

        assert mmap_buffer.count() == 2

    def test_clear(self, mmap_buffer):
        """버퍼 초기화 테스트."""
        mmap_buffer.put({"data": "test"})
        assert mmap_buffer.count() == 1

        mmap_buffer.clear()
        assert mmap_buffer.count() == 0

    def test_stats(self, mmap_buffer):
        """통계 조회 테스트."""
        mmap_buffer.put({"data": "test"})

        stats = mmap_buffer.get_stats()
        assert "entry_count" in stats
        assert "write_pos" in stats
        assert "file_size" in stats
        assert stats["entry_count"] == 1


class TestMmapBufferPersistence:
    """MmapBuffer 영속성 테스트."""

    def test_persistence_across_restart(self, temp_mmap_path):
        """재시작 후 데이터 보존 테스트."""
        from baldur.audit.persistence.mmap_buffer import MmapBuffer

        # 첫 번째 인스턴스
        buffer1 = MmapBuffer(file_path=temp_mmap_path, size_mb=1)
        buffer1.put({"event": "persistent"})
        count1 = buffer1.count()
        buffer1.close()

        # 두 번째 인스턴스 (재시작 시뮬레이션)
        buffer2 = MmapBuffer(file_path=temp_mmap_path, size_mb=1)
        count2 = buffer2.count()
        entries = buffer2.iter_entries()
        buffer2.close()

        assert count2 == count1 == 1
        assert entries[0]["event"] == "persistent"


class TestMmapBufferContextManager:
    """MmapBuffer Context Manager 테스트."""

    def test_context_manager(self, temp_mmap_path):
        """with문 사용 테스트."""
        from baldur.audit.persistence.mmap_buffer import MmapBuffer

        with MmapBuffer(file_path=temp_mmap_path, size_mb=1) as buffer:
            buffer.put({"event": "context_test"})
            assert buffer.count() == 1
