"""
WAL 데이터 모델 및 예외 클래스.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from baldur.core.exceptions import AuditError


class WALState(str, Enum):
    """WAL 상태."""

    ACTIVE = "active"
    ROTATING = "rotating"
    CLOSED = "closed"
    CORRUPTED = "corrupted"
    DISK_FULL_FAILOPEN = "disk_full_failopen"


@dataclass
class WALEntry:
    """WAL 엔트리."""

    sequence: int
    timestamp: float
    data: dict[str, Any]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        """딕셔너리로 변환."""
        return {
            "seq": self.sequence,
            "ts": self.timestamp,
            "data": self.data,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WALEntry:
        """딕셔너리에서 생성."""
        return cls(
            sequence=d["seq"],
            timestamp=d["ts"],
            data=d["data"],
            checksum=d.get("checksum", ""),
        )


@dataclass
class WALConfig:
    """WAL 설정."""

    wal_dir: str = "/var/log/audit/wal"
    max_file_size_mb: int = 100
    sync_on_write: bool = True
    max_files: int = 10
    file_prefix: str = "audit_wal"

    # Group Commit 설정
    group_commit_enabled: bool = False
    group_commit_max_entries: int = 100
    group_commit_max_wait_ms: int = 10

    # Disk Full Fail-Open 설정
    fail_open_on_disk_full: bool = True
    disk_recovery_threshold: float = 0.1

    # Best-Effort Recovery 설정
    best_effort_recovery: bool = True

    # Parallel Recovery 설정
    recovery_max_workers: int = 4
    recovery_batch_size: int = 1000

    # Priority-based Purge 설정
    priority_based_purge: bool = True
    purge_priority_order: tuple[str, ...] = (
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    )
    critical_retention_min_mb: int = 100

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@dataclass
class WALStats:
    """WAL 통계."""

    state: WALState
    current_file: str | None
    current_size_bytes: int
    total_entries: int
    total_files: int
    last_sequence: int
    last_write_time: float | None
    corrupted_entries: int
    recovered_entries: int
    group_commit_flushes: int = 0
    group_commit_buffered: int = 0


class WALError(AuditError):
    """WAL-related error."""

    pass


class WALCorruptionError(WALError):
    """WAL corruption error."""

    def __init__(self, message: str, sequence: int, expected: str, computed: str):
        super().__init__(message)
        self.sequence = sequence
        self.expected = expected
        self.computed = computed

    def extra_context(self) -> dict[str, Any]:
        ctx = super().extra_context()
        ctx["sequence"] = self.sequence
        ctx["expected_checksum"] = self.expected
        ctx["computed_checksum"] = self.computed
        return ctx
