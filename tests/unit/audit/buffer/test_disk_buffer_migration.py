"""
Disk Buffer Migration (Drain-on-Startup) 단위 테스트.

Pod 재시작 시 버퍼 복구 기능을 테스트합니다.
"""

from __future__ import annotations

import shutil
import tempfile
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
    """임시 LMDB 경로."""
    temp_dir = tempfile.mkdtemp(prefix="drain_test_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def drain_buffer(temp_db_path) -> Generator:
    """Drain 테스트용 버퍼."""
    from baldur.audit.persistence.config import DiskBufferSettings
    from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

    settings = DiskBufferSettings(
        data_dir=temp_db_path,
        lmdb_map_size_mb=50,
        sync_on_write=True,
        group_commit_enabled=False,
        enable_shutdown_handlers=False,
        include_hostname_in_db_name=False,
        include_pid_in_db_name=False,
        disk_full_threshold=0.0,  # 테스트에서는 디스크 체크 비활성화
    )

    buffer = DiskPersistentBuffer(settings=settings, db_name="drain_test")
    yield buffer
    buffer.close()


class TestDrainOnStartup:
    """drain_on_startup 테스트."""

    def test_drain_empty_buffer(self, drain_buffer):
        """빈 버퍼 drain 테스트."""
        from baldur.audit.persistence.migration import drain_on_startup

        result = drain_on_startup(
            buffer=drain_buffer,
            flush_handler=lambda entries: True,
        )

        assert result.drained == 0
        assert result.failed == 0
        assert result.skipped == 0

    def test_drain_success(self, drain_buffer):
        """성공적인 drain 테스트."""
        from baldur.audit.persistence.migration import drain_on_startup

        # 버퍼에 데이터 추가
        for i in range(10):
            drain_buffer.put({"index": i})

        processed = []

        def handler(entries):
            for e in entries:
                processed.append(e)
            return True

        result = drain_on_startup(
            buffer=drain_buffer,
            flush_handler=handler,
            batch_size=5,
        )

        assert result.drained == 10
        assert result.failed == 0
        assert len(processed) == 10
        assert drain_buffer.count() == 0

    def test_drain_with_failure(self, drain_buffer):
        """실패가 있는 drain 테스트."""
        from baldur.audit.persistence.migration import drain_on_startup

        for i in range(5):
            drain_buffer.put({"index": i})

        call_count = [0]

        def failing_handler(entries):
            call_count[0] += 1
            raise ValueError("Simulated failure")

        result = drain_on_startup(
            buffer=drain_buffer,
            flush_handler=failing_handler,
            fail_fast=True,
        )

        assert result.failed > 0
        assert len(result.errors) > 0

    def test_drain_max_batches(self, drain_buffer):
        """최대 배치 수 제한 테스트."""
        from baldur.audit.persistence.migration import drain_on_startup

        for i in range(100):
            drain_buffer.put({"index": i})

        result = drain_on_startup(
            buffer=drain_buffer,
            flush_handler=lambda entries: True,
            batch_size=10,
            max_batches=3,  # 30개만 처리
        )

        assert result.drained == 30
        assert drain_buffer.count() == 70


class TestDrainResult:
    """DrainResult 데이터 클래스 테스트."""

    def test_drain_result_defaults(self):
        """기본값 테스트."""
        from baldur.audit.persistence.migration import DrainResult

        result = DrainResult()

        assert result.drained == 0
        assert result.failed == 0
        assert result.skipped == 0
        assert result.duration_seconds == 0.0
        assert result.errors == []

    def test_drain_result_with_values(self):
        """값 설정 테스트."""
        from baldur.audit.persistence.migration import DrainResult

        result = DrainResult(
            drained=10,
            failed=2,
            skipped=1,
            duration_seconds=1.5,
            errors=["error1"],
        )

        assert result.drained == 10
        assert result.failed == 2
        assert result.skipped == 1
        assert result.duration_seconds == 1.5
        assert result.errors == ["error1"]


@pytest.mark.asyncio
class TestAsyncDrainOnStartup:
    """비동기 drain_on_startup 테스트."""

    async def test_async_drain_success(self, drain_buffer):
        """비동기 drain 성공 테스트."""
        from baldur.audit.persistence.migration import async_drain_on_startup

        for i in range(5):
            drain_buffer.put({"index": i})

        async def async_handler(entries):
            return True

        result = await async_drain_on_startup(
            buffer=drain_buffer,
            async_flush_handler=async_handler,
            batch_size=5,
        )

        assert result.drained == 5
        assert drain_buffer.count() == 0
