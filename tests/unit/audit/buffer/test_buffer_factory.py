"""
Buffer Factory 및 Fallback Chain DiskBuffer 통합 단위 테스트.

get_audit_buffer() 팩토리 함수와 HashChainFallbackChain의
DiskBuffer 사용을 테스트합니다.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Generator
from unittest import mock

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
    temp_dir = tempfile.mkdtemp(prefix="buffer_factory_test_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def disk_buffer_settings(temp_db_path: str):
    """테스트용 DiskBufferSettings."""
    from baldur.audit.persistence.config import DiskBufferSettings

    return DiskBufferSettings(
        data_dir=temp_db_path,
        lmdb_map_size_mb=50,
        max_entries=1000,
        sync_on_write=True,
        enable_checksum=True,
        group_commit_enabled=False,
        enable_dead_letter_db=True,
        enable_shutdown_handlers=False,
        include_hostname_in_db_name=False,
        include_pid_in_db_name=False,
    )


class TestGetAuditBufferFactory:
    """get_audit_buffer() 팩토리 함수 테스트."""

    def test_default_returns_memory_buffer(self):
        """기본값(환경변수 없음)은 InMemoryAuditBuffer 반환."""
        # 환경변수 제거
        with mock.patch.dict(os.environ, {}, clear=True):
            # BALDUR_BUFFER_TYPE 없으면 memory
            os.environ.pop("BALDUR_BUFFER_TYPE", None)

            from baldur.audit.resilience.buffer import (
                InMemoryAuditBuffer,
                get_audit_buffer,
            )

            buffer = get_audit_buffer()
            assert isinstance(buffer, InMemoryAuditBuffer)

    def test_memory_type_returns_memory_buffer(self):
        """BALDUR_BUFFER_TYPE=memory는 InMemoryAuditBuffer 반환."""
        with mock.patch.dict(os.environ, {"BALDUR_BUFFER_TYPE": "memory"}):
            from baldur.audit.resilience.buffer import (
                InMemoryAuditBuffer,
                get_audit_buffer,
            )

            buffer = get_audit_buffer()
            assert isinstance(buffer, InMemoryAuditBuffer)

    def test_disk_type_returns_disk_adapter(self, temp_db_path):
        """BALDUR_BUFFER_TYPE=disk는 DiskBufferAdapter 반환."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_BUFFER_TYPE": "disk",
                "BALDUR_DISK_BUFFER_DATA_DIR": temp_db_path,
                "BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB": "100",
            },
        ):
            # 싱글톤 리셋
            from baldur.audit.persistence.config import reset_disk_buffer_settings
            from baldur.audit.persistence.disk_buffer import (
                DiskBufferAdapter,
                reset_disk_buffer,
            )
            from baldur.audit.resilience.buffer import get_audit_buffer

            reset_disk_buffer_settings()
            reset_disk_buffer()
            DiskBufferAdapter.reset_instance()

            try:
                buffer = get_audit_buffer()
                assert isinstance(buffer, DiskBufferAdapter)
            finally:
                # 정리
                DiskBufferAdapter.reset_instance()
                reset_disk_buffer()
                reset_disk_buffer_settings()

    def test_disk_adapter_interface_methods_exist(self, temp_db_path):
        """DiskBufferAdapter는 InMemoryAuditBuffer와 동일한 메서드 제공."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_BUFFER_TYPE": "disk",
                "BALDUR_DISK_BUFFER_DATA_DIR": temp_db_path,
                "BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB": "100",
            },
        ):
            from baldur.audit.persistence.config import reset_disk_buffer_settings
            from baldur.audit.persistence.disk_buffer import (
                DiskBufferAdapter,
                reset_disk_buffer,
            )
            from baldur.audit.resilience.buffer import get_audit_buffer

            reset_disk_buffer_settings()
            reset_disk_buffer()
            DiskBufferAdapter.reset_instance()

            try:
                buffer = get_audit_buffer()

                # InMemoryAuditBuffer와 동일한 메서드 확인
                assert hasattr(buffer, "add")
                assert hasattr(buffer, "try_flush")
                assert hasattr(buffer, "get_stats")
                assert hasattr(buffer, "__len__")
                assert callable(buffer.add)
                assert callable(buffer.try_flush)
                assert callable(buffer.get_stats)

            finally:
                DiskBufferAdapter.reset_instance()
                reset_disk_buffer()
                reset_disk_buffer_settings()


class TestFallbackChainDiskBuffer:
    """HashChainFallbackChain DiskBuffer 사용 테스트."""

    def test_add_integrity_memory_uses_disk_buffer(self, temp_db_path):
        """_add_integrity_memory 메서드가 DiskBuffer 사용."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DISK_BUFFER_DATA_DIR": temp_db_path,
                "BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB": "100",
                "BALDUR_DISK_BUFFER_ENABLE_SHUTDOWN_HANDLERS": "false",
            },
        ):
            from baldur.audit.graceful_degradation.fallback import (
                HashChainFallbackChain,
            )
            from baldur.audit.persistence.config import reset_disk_buffer_settings
            from baldur.audit.persistence.disk_buffer import reset_disk_buffer

            reset_disk_buffer_settings()
            reset_disk_buffer()

            try:
                # Redis 없이 초기화
                fallback = HashChainFallbackChain(
                    redis_primary=None,
                    redis_replica=None,
                )

                entry = {"event_type": "test_memory", "data": "test"}

                # _add_integrity_memory 직접 호출
                result = fallback._add_integrity_memory(entry)

                # integrity 필드 확인
                assert "integrity" in result
                integrity = result["integrity"]

                # DiskBuffer 사용 시 tier=disk_buffer, volatile=False
                assert integrity["tier"] == "disk_buffer"
                assert integrity["volatile"] is False
                assert integrity["degraded"] is True

            finally:
                reset_disk_buffer()
                reset_disk_buffer_settings()

    def test_add_integrity_memory_fallback_to_memory(self):
        """DiskBuffer import 실패 시 메모리 버퍼로 폴백."""
        from baldur.audit.graceful_degradation.fallback import (
            HashChainFallbackChain,
        )

        fallback = HashChainFallbackChain(
            redis_primary=None,
            redis_replica=None,
        )

        entry = {"event_type": "test_memory_fallback", "data": "test"}

        # get_disk_buffer import 실패 시뮬레이션
        with mock.patch.dict("sys.modules", {"lmdb": None}):
            with mock.patch(
                "baldur.audit.persistence.disk_buffer.get_disk_buffer",
                side_effect=Exception("lmdb not available"),
            ):
                result = fallback._add_integrity_memory(entry)

                # 메모리 폴백 시 tier=memory, volatile=True
                integrity = result["integrity"]
                assert integrity["tier"] == "memory"
                assert integrity["volatile"] is True

    def test_add_integrity_memory_fields_complete(self, temp_db_path):
        """_add_integrity_memory에서 모든 필수 필드가 설정됨."""
        with mock.patch.dict(
            os.environ,
            {
                "BALDUR_DISK_BUFFER_DATA_DIR": temp_db_path,
                "BALDUR_DISK_BUFFER_LMDB_MAP_SIZE_MB": "100",
            },
        ):
            from baldur.audit.graceful_degradation.fallback import (
                HashChainFallbackChain,
            )
            from baldur.audit.persistence.config import reset_disk_buffer_settings
            from baldur.audit.persistence.disk_buffer import reset_disk_buffer

            reset_disk_buffer_settings()
            reset_disk_buffer()

            try:
                fallback = HashChainFallbackChain(
                    redis_primary=None,
                    redis_replica=None,
                )

                entry = {"event_type": "integrity_test"}
                result = fallback._add_integrity_memory(entry)

                integrity = result["integrity"]

                # 필수 필드 확인
                assert "sequence" in integrity
                assert "previous_hash" in integrity
                assert "timestamp" in integrity
                assert "pod_id" in integrity
                assert "current_hash" in integrity
                assert "degraded" in integrity
                assert "degraded_reason" in integrity
                assert "degraded_at" in integrity
                assert "volatile" in integrity
                assert "tier" in integrity

            finally:
                reset_disk_buffer()
                reset_disk_buffer_settings()

    def test_fallback_chain_local_tier_no_volatile_field(self):
        """local tier는 volatile 필드가 없음 (정상 동작)."""
        from baldur.audit.graceful_degradation.fallback import (
            HashChainFallbackChain,
        )

        # Redis 없이 초기화 - local tier로 폴백
        fallback = HashChainFallbackChain(
            redis_primary=None,
            redis_replica=None,
        )

        entry = {"event_type": "test_local"}
        result = fallback.add_integrity(entry)

        integrity = result["integrity"]

        # local tier 확인
        assert integrity["tier"] == "local"
        assert integrity["degraded"] is True
        # local tier는 volatile 필드 없음 (파일에 저장되므로)
        assert "volatile" not in integrity or integrity.get("volatile") is None
