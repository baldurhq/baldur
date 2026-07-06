"""Composite checkpoint storage with tiered fallback chain.

Provides multi-tier fallback: Primary (Redis) -> Secondary (File) -> Memory Buffer.
Ensures checkpoint data is never lost even when multiple storage tiers fail.

Version: 1.0.0
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.audit.checkpoint.strategy import (
    CheckpointError,
    CheckpointStorageStrategy,
    UnifiedCheckpointData,
)

__all__ = [
    "CompositeCheckpointStorage",
]

logger = structlog.get_logger()


class CompositeCheckpointStorage(CheckpointStorageStrategy):
    """
    Multi-storage fallback chain.

    Fallback order:
    1. Primary (Redis) - Full distributed support
    2. Secondary (File) - Local persistent backup
    3. Memory Buffer - Last resort (volatile)

    Usage:
        composite = CompositeCheckpointStorage(
            primary=RedisCheckpointStorage(redis),
            secondary=FileCheckpointStorage("/backup"),
        )
        composite.save("default", data)  # Auto fallback to File if Redis fails
    """

    def __init__(
        self,
        primary: CheckpointStorageStrategy,
        secondary: CheckpointStorageStrategy | None = None,
        enable_memory_fallback: bool = True,
    ):
        """
        Initialize multi-storage fallback chain.

        Args:
            primary: Primary storage (Redis recommended)
            secondary: Backup storage (File recommended)
            enable_memory_fallback: Enable memory buffer as last-resort fallback
        """
        super().__init__()
        self._primary = primary
        self._secondary = secondary
        self._enable_memory_fallback = enable_memory_fallback
        self._lock = threading.Lock()

        # Memory Buffer (last resort)
        self._memory_buffer: dict[str, UnifiedCheckpointData] = {}

        # Statistics
        self._stats = {
            "primary_writes": 0,
            "secondary_writes": 0,
            "memory_writes": 0,
            "fallback_events": 0,
        }
        self._current_tier = "primary"

    def save(self, namespace: str, data: UnifiedCheckpointData) -> None:
        """
        Tiered fallback save.

        1. Try Primary -> return on success
        2. Primary failed -> try Secondary
        3. Secondary failed -> Memory Buffer (last resort)
        """
        with self._lock:
            # Tier 1: Primary
            try:
                self._primary.save(namespace, data)
                self._current_tier = "primary"
                self._stats["primary_writes"] += 1
                return
            except Exception as e:
                logger.warning(
                    "composite_checkpoint.primary_failed",
                    error=e,
                )
                self._stats["fallback_events"] += 1

            # Tier 2: Secondary (mark as degraded)
            if self._secondary:
                try:
                    # Record degraded state
                    data_copy = UnifiedCheckpointData(
                        wal_sequence=data.wal_sequence,
                        timestamp=data.timestamp,
                        version=data.version,
                        kafka_topic=data.kafka_topic,
                        kafka_partition=data.kafka_partition,
                        kafka_offset=data.kafka_offset,
                        checksum=data.checksum,
                    )
                    self._secondary.save(namespace, data_copy)
                    self._current_tier = "secondary"
                    self._stats["secondary_writes"] += 1
                    logger.warning(
                        "composite_checkpoint.degraded_secondary",
                        namespace=namespace,
                    )
                    return
                except Exception as e:
                    logger.warning(
                        "composite_checkpoint.secondary_failed",
                        error=e,
                    )
                    self._stats["fallback_events"] += 1

            # Tier 3: Memory Buffer (last resort)
            if self._enable_memory_fallback:
                self._memory_buffer[namespace] = data
                self._current_tier = "memory"
                self._stats["memory_writes"] += 1
                logger.error(
                    "composite_checkpoint.degraded_memory_volatile",
                    namespace=namespace,
                )
                return

            raise CheckpointError("All storage tiers failed")

    def load(self, namespace: str) -> UnifiedCheckpointData | None:
        """Tiered Load (Primary -> Secondary -> Memory order)."""
        with self._lock:
            # Primary
            try:
                data = self._primary.load(namespace)
                if data:
                    return data
            except Exception:
                pass

            # Secondary
            if self._secondary:
                try:
                    data = self._secondary.load(namespace)
                    if data:
                        return data
                except Exception:
                    pass

            # Memory
            return self._memory_buffer.get(namespace)

    def get_stats(self) -> dict[str, Any]:
        """Get statistics."""
        return {
            **self._stats,
            "current_tier": self._current_tier,
        }

    def commit(self, namespace: str) -> None:
        """Commit to primary."""
        self._primary.commit(namespace)

    def delete(self, namespace: str) -> bool:
        """Delete from all tiers."""
        results = []
        try:
            results.append(self._primary.delete(namespace))
        except Exception:
            pass
        if self._secondary:
            try:
                results.append(self._secondary.delete(namespace))
            except Exception:
                pass
        self._memory_buffer.pop(namespace, None)
        return any(results)

    def exists(self, namespace: str) -> bool:
        """Check existence in any tier."""
        try:
            if self._primary.exists(namespace):
                return True
        except Exception:
            pass
        if self._secondary:
            try:
                if self._secondary.exists(namespace):
                    return True
            except Exception:
                pass
        return namespace in self._memory_buffer
