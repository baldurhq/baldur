"""Checkpoint storage strategy base classes and data models.

Contains the UnifiedCheckpointData dataclass, exception hierarchy,
CheckpointStorageStrategy ABC, and Prometheus counter singletons.

Version: 1.0.0
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from baldur.core.exceptions import BaldurError
from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

__all__ = [
    "UNINITIALIZED",
    "CheckpointCorruptedError",
    "CheckpointError",
    "CheckpointStorageStrategy",
    "UnifiedCheckpointData",
    "get_load_failures_counter",
    "get_save_failures_counter",
]

_UNINITIALIZED: Any = object()
_CHECKPOINT_SAVE_FAILURES: Any = _UNINITIALIZED
_CHECKPOINT_LOAD_FAILURES: Any = _UNINITIALIZED

# Public alias so tests/other modules can reference/reset the sentinel
UNINITIALIZED = _UNINITIALIZED


def get_save_failures_counter():
    """Checkpoint save failure Counter singleton."""
    global _CHECKPOINT_SAVE_FAILURES
    if _CHECKPOINT_SAVE_FAILURES is _UNINITIALIZED:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CHECKPOINT_SAVE_FAILURES = get_or_create_counter(
                "baldur_checkpoint_save_failures_total",
                "Number of checkpoint save failures",
                ["storage_type"],
            )
        except ImportError:
            _CHECKPOINT_SAVE_FAILURES = None
    return _CHECKPOINT_SAVE_FAILURES


def get_load_failures_counter():
    """Checkpoint load failure Counter singleton."""
    global _CHECKPOINT_LOAD_FAILURES
    if _CHECKPOINT_LOAD_FAILURES is _UNINITIALIZED:
        try:
            from baldur.metrics.registry import get_or_create_counter

            _CHECKPOINT_LOAD_FAILURES = get_or_create_counter(
                "baldur_checkpoint_load_failures_total",
                "Number of checkpoint load failures",
                ["storage_type"],
            )
        except ImportError:
            _CHECKPOINT_LOAD_FAILURES = None
    return _CHECKPOINT_LOAD_FAILURES


# =============================================================================
# Unified data model
# =============================================================================


@dataclass
class UnifiedCheckpointData(SerializableMixin):
    """
    Unified checkpoint data.

    Merges legacy CheckpointData and KafkaCheckpointData.
    Kafka fields are Optional so the model works with File mode too.
    """

    # Required fields (used by all strategies)
    wal_sequence: int
    """Last processed WAL sequence."""

    timestamp: str = field(default_factory=lambda: utc_now().isoformat())
    """Checkpoint time (ISO 8601)."""

    version: int = 1
    """Checkpoint version."""

    # Kafka-only fields (Optional - used by Kafka strategy only)
    kafka_topic: str | None = None
    """Kafka topic."""

    kafka_partition: int | None = None
    """Kafka partition."""

    kafka_offset: int | None = None
    """Kafka offset."""

    checksum: str | None = None
    """WAL entry checksum (for verification)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnifiedCheckpointData:
        """Create from dictionary."""
        return cls(
            wal_sequence=data.get("wal_sequence", data.get("last_sequence", 0)),
            timestamp=data.get("timestamp", utc_now().isoformat()),
            version=data.get("version", 1),
            kafka_topic=data.get("kafka_topic"),
            kafka_partition=data.get("kafka_partition"),
            kafka_offset=data.get("kafka_offset"),
            checksum=data.get("checksum"),
        )

    @classmethod
    def from_legacy_checkpoint_data(cls, data: dict[str, Any]) -> UnifiedCheckpointData:
        """
        Convert from legacy CheckpointData format.

        Legacy format has last_sequence, timestamp(float), version fields.
        """
        timestamp = data.get("timestamp", 0.0)
        if isinstance(timestamp, (int, float)):
            timestamp = datetime.fromtimestamp(timestamp, tz=UTC).isoformat()

        return cls(
            wal_sequence=data.get("last_sequence", 0),
            timestamp=timestamp,
            version=data.get("version", 1),
        )


class CheckpointError(BaldurError):
    """Checkpoint related error."""

    pass


class CheckpointCorruptedError(CheckpointError):
    """
    Checkpoint corruption error.

    Raised when checksum verification fails.
    """

    def __init__(self, message: str, expected: str, computed: str):
        super().__init__(message)
        self.expected = expected
        self.computed = computed

    def extra_context(self) -> dict[str, Any]:
        """Return checksum mismatch details."""
        ctx = super().extra_context()
        ctx["expected"] = self.expected
        ctx["computed"] = self.computed
        return ctx


# =============================================================================
# Storage strategy abstract interface
# =============================================================================


class CheckpointStorageStrategy(ABC):
    """
    Checkpoint storage strategy interface.

    Defines common save(), load(), commit() etc. contract regardless of
    storage method. Implementations choose File, Redis, Kafka+Redis etc.
    based on infrastructure environment.
    """

    @abstractmethod
    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """
        Save checkpoint.

        Args:
            namespace: Namespace (multi-tenant support)
            data: Unified checkpoint data
        """
        pass

    @abstractmethod
    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        """
        Load checkpoint.

        Args:
            namespace: Namespace

        Returns:
            UnifiedCheckpointData or None
        """
        pass

    @abstractmethod
    def commit(self, namespace: str) -> None:
        """
        Commit checkpoint (transaction complete).

        Ensures atomic commit in Redis or distributed environments.

        Args:
            namespace: Namespace
        """
        pass

    @abstractmethod
    def delete(self, namespace: str) -> bool:
        """
        Delete checkpoint.

        Args:
            namespace: Namespace

        Returns:
            Whether deletion succeeded
        """
        pass

    @abstractmethod
    def exists(self, namespace: str) -> bool:
        """
        Check checkpoint existence.

        Args:
            namespace: Namespace

        Returns:
            Whether checkpoint exists
        """
        pass

    def get_wal_sequence(self, namespace: str = "default") -> int:
        """
        Get last WAL sequence (convenience method).

        Args:
            namespace: Namespace

        Returns:
            Last WAL sequence (0 if not found)
        """
        data = self.load(namespace)
        return data.wal_sequence if data else 0
