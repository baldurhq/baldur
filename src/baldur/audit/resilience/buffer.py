"""
In-Memory Audit Buffer.

WAL 실패 시 메모리 폴백 버퍼.
디스크 장애 시 중요 로그를 메모리에 임시 보관하고,
시스템 정상화 시 파일로 플러시합니다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from baldur.audit.resilience.buffer_protocol import AuditBufferProtocol

import structlog

from baldur.settings.resilient_recorder import get_resilient_recorder_settings
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_max_entries() -> int:
    """Get max entries from settings."""
    return get_resilient_recorder_settings().memory_buffer_max_entries


def _get_flush_interval() -> float:
    """Get flush interval from settings."""
    return get_resilient_recorder_settings().memory_buffer_flush_interval


class InMemoryAuditBuffer:
    """
    WAL 실패 시 메모리 폴백 버퍼.

    디스크 장애 시 중요 로그를 메모리에 임시 보관하고,
    시스템 정상화 시 파일로 플러시합니다.

    설계 원칙:
    - 최대 엔트리 수는 ResilientRecorderSettings에서 설정 (기본 10,000개)
    - FIFO: 용량 초과 시 가장 오래된 엔트리 삭제
    - 주기적 플러시 시도 (기본 30초 간격)

    Thread-safe: RLock 사용
    """

    _instance: InMemoryAuditBuffer | None = None
    _lock = threading.Lock()

    # Legacy constants for backward compatibility
    MAX_ENTRIES = 10_000
    FLUSH_INTERVAL_SECONDS = 30.0

    def __init__(
        self,
        max_entries: int | None = None,
        flush_interval_seconds: float | None = None,
    ):
        """
        InMemoryAuditBuffer 초기화.

        Args:
            max_entries: 최대 엔트리 수 (default from ResilientRecorderSettings)
            flush_interval_seconds: 플러시 간격 (default from ResilientRecorderSettings)
        """
        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = threading.RLock()
        self._last_flush_attempt: datetime | None = None
        self._flush_failures: int = 0
        self._total_dropped: int = 0
        self._total_buffered: int = 0
        self._max_entries = (
            max_entries if max_entries is not None else _get_max_entries()
        )
        self._flush_interval_seconds = (
            flush_interval_seconds
            if flush_interval_seconds is not None
            else _get_flush_interval()
        )

    @classmethod
    def get_instance(cls) -> InMemoryAuditBuffer:
        """싱글톤 인스턴스 반환."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """인스턴스 리셋 (테스트용)."""
        with cls._lock:
            cls._instance = None

    def add(self, entry: dict[str, Any]) -> bool:
        """
        엔트리 추가.

        Args:
            entry: WAL 엔트리 딕셔너리

        Returns:
            True if added successfully, False if buffer full and oldest removed
        """
        with self._buffer_lock:
            dropped = False
            if len(self._buffer) >= self._max_entries:
                # FIFO: 가장 오래된 엔트리 삭제
                self._buffer.pop(0)
                self._total_dropped += 1
                dropped = True
                logger.warning(
                    "[InMemoryAuditBuffer] Buffer full, dropped oldest entry "  # noqa: G004
                    f"(total dropped: {self._total_dropped})"
                )

            entry["buffered_at"] = utc_now().isoformat()
            self._buffer.append(entry)
            self._total_buffered += 1

            return not dropped

    def try_flush(self, wal_write_func: Callable[[dict[str, Any]], int | None]) -> int:
        """
        버퍼를 WAL로 플러시 시도.

        Args:
            wal_write_func: WAL 쓰기 함수 (entry dict -> sequence 반환)

        Returns:
            플러시된 엔트리 수
        """
        with self._buffer_lock:
            if not self._buffer:
                return 0

            self._last_flush_attempt = utc_now()
            flushed = 0
            remaining = []

            for entry in self._buffer:
                try:
                    # buffered_at 제거 후 WAL에 기록
                    entry_copy = {k: v for k, v in entry.items() if k != "buffered_at"}
                    result = wal_write_func(entry_copy)
                    if result is not None:
                        flushed += 1
                    else:
                        remaining.append(entry)
                except Exception as e:
                    logger.debug(
                        "in_memory_audit_buffer.flush_entry_failed",
                        error=e,
                    )
                    remaining.append(entry)

            self._buffer = remaining

            if flushed > 0:
                logger.info(
                    "in_memory_audit_buffer.flushed_entries_wal",
                    flushed=flushed,
                )

            if remaining:
                self._flush_failures += 1

            return flushed

    def count(self) -> int:
        """현재 버퍼 크기 (AuditBufferProtocol)."""
        with self._buffer_lock:
            return len(self._buffer)

    def get_buffer_size(self) -> int:
        """현재 버퍼 크기 (legacy alias)."""
        return self.count()

    def get_stats(self) -> dict[str, Any]:
        """버퍼 통계."""
        with self._buffer_lock:
            current = len(self._buffer)
            capacity = self._max_entries
            return {
                # Common keys (AuditBufferProtocol)
                "count": current,
                "total_added": self._total_buffered,
                "total_dropped": self._total_dropped,
                "capacity": capacity,
                "usage_percent": (current / capacity * 100) if capacity else None,
                # Implementation-specific keys
                "buffered_entries": current,
                "max_entries": capacity,
                "flush_interval_seconds": self._flush_interval_seconds,
                "total_buffered": self._total_buffered,
                "flush_failures": self._flush_failures,
                "last_flush_attempt": (
                    self._last_flush_attempt.isoformat()
                    if self._last_flush_attempt
                    else None
                ),
            }

    def clear(self) -> int:
        """버퍼 비우기 (테스트용). 삭제된 엔트리 수 반환."""
        with self._buffer_lock:
            count = len(self._buffer)
            self._buffer.clear()
            return count


def get_inmemory_audit_buffer() -> InMemoryAuditBuffer:
    """Get the in-memory audit buffer instance."""
    return InMemoryAuditBuffer.get_instance()


def get_audit_buffer() -> AuditBufferProtocol:
    """
    Audit Buffer 팩토리.

    환경변수로 구현 선택:
    - BALDUR_BUFFER_TYPE=memory (기본, 기존 휘발성 버퍼)
    - BALDUR_BUFFER_TYPE=disk (영속 버퍼, Pod 재시작에도 데이터 보존)

    Returns:
        InMemoryAuditBuffer 또는 DiskBufferAdapter 인스턴스

    Note:
        DiskBufferAdapter는 InMemoryAuditBuffer와 동일한 인터페이스를 제공합니다.
        - add(entry): 엔트리 추가
        - try_flush(callback): WAL로 플러시
        - get_stats(): 통계 조회
    """
    import os

    buffer_type = os.environ.get("BALDUR_BUFFER_TYPE", "memory")

    if buffer_type == "disk":
        # Direct import to the concrete module — disk_buffer re-exports
        # DiskBufferAdapter via lazy __getattr__ which mypy sees as plain `type`,
        # losing the `get_instance` classmethod.
        from baldur.audit.persistence.disk_buffer_adapter import DiskBufferAdapter

        return DiskBufferAdapter.get_instance()
    return InMemoryAuditBuffer.get_instance()


__all__ = ["InMemoryAuditBuffer", "get_inmemory_audit_buffer", "get_audit_buffer"]
