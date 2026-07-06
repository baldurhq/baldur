"""DLQ Domain Value Types.

OSS-tier value types for Dead-Letter Queue configuration and operation
results. Runtime-instantiated DTOs that must be available on OSS-only
installs (e.g., DLQConfig.from_settings() is called by adapter glue,
DLQEntryResult is returned by the OSS audit fallback path, CleanupStats
is consumed by both OSS interfaces.statistics and the PRO DLQ service).

The DLQ orchestrator and repository classes remain PRO-tier — OSS
callers reach them via the Protocols in :mod:`baldur.interfaces.dlq`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from baldur.settings import get_dlq_settings


@dataclass
class DLQConfig:
    """DLQ runtime configuration.

    Loaded from RuntimeConfigManager when available (PRO tier with hot
    reload), falling back to the static DLQSettings (Pydantic) defaults
    on OSS-only installs.
    """

    enabled: bool = True
    retention_days: int = 30
    max_replay_attempts: int = 2
    retry_delay: int = 60
    expiry_hours: int = 72
    batch_size: int = 10

    @classmethod
    def from_settings(cls) -> DLQConfig:
        """Load configuration from RuntimeConfigManager (preferred) or DLQSettings."""
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is not None:
                runtime_config = manager.get_dlq_config()
                return cls(
                    enabled=runtime_config.get("enabled", True),
                    retention_days=runtime_config.get("retention_days", 30),
                    max_replay_attempts=runtime_config.get("max_replay_attempts", 2),
                    retry_delay=runtime_config.get("retry_delay", 60),
                    expiry_hours=runtime_config.get("expiry_hours", 72),
                    batch_size=runtime_config.get("batch_size", 10),
                )
        except Exception:
            pass

        dlq_settings = get_dlq_settings()
        return cls(
            enabled=dlq_settings.enabled,
            retention_days=dlq_settings.retention_days,
            max_replay_attempts=dlq_settings.max_replay_attempts,
            retry_delay=dlq_settings.retry_delay,
            expiry_hours=dlq_settings.expiry_hours,
            batch_size=dlq_settings.batch_size,
        )


@dataclass
class DLQEntryResult:
    """Outcome of a single DLQ store/push operation."""

    success: bool
    dlq_id: str | None = None
    error: str | None = None
    fallback_path: str | None = None
    """Local fallback path when the DB write failed but data was preserved."""

    @classmethod
    def created(cls, dlq_id: str) -> DLQEntryResult:
        return cls(success=True, dlq_id=dlq_id)

    @classmethod
    def failed(cls, error: str) -> DLQEntryResult:
        return cls(success=False, error=error)

    @classmethod
    def fallback(cls, error: str, fallback_path: str) -> DLQEntryResult:
        return cls(success=False, error=error, fallback_path=fallback_path)

    @property
    def is_fallback(self) -> bool:
        return self.fallback_path is not None


@dataclass
class CleanupStats:
    """Statistics for DLQ cleanup operations."""

    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    resolved_older_than_30_days: int = 0
    archived_older_than_90_days: int = 0

    @property
    def can_archive(self) -> int:
        return self.resolved_older_than_30_days

    @property
    def can_purge(self) -> int:
        return self.archived_older_than_90_days


__all__ = ["CleanupStats", "DLQConfig", "DLQEntryResult"]


# Suppress ruff F401 — Any is reserved for forward-compatible field annotations.
_ = Any
