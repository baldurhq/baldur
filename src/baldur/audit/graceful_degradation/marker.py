"""
Degraded Entry Marker.

Contains:
- DegradedEntryInfo: Information about a degraded entry
- DegradedEntryMarker: Marker for entries recorded during degraded operation
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class DegradedEntryInfo:
    """Information about a degraded entry."""

    sequence: int
    original_tier: str
    degraded_reason: str
    degraded_at: str
    pod_id: str
    reconciled: bool = False
    reconciled_at: str | None = None
    new_sequence: int | None = None


class DegradedEntryMarker:
    """
    Marker for entries recorded during degraded operation.

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Tracks all entries that were written during failures for later
    reconciliation when the system recovers.

    Pattern source:
        audit/integrity.py#L484-503

    Usage:
        marker = DegradedEntryMarker()
        entry = marker.mark_degraded(entry, "redis_timeout")
        # Later during recovery:
        entries = marker.get_unreconciled_entries()
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        key_prefix: str = "baldur:",
    ):
        """
        Initialize degraded entry marker.

        Args:
            redis_client: Redis client for distributed tracking
            key_prefix: Prefix for Redis keys
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._lock = threading.RLock()

        # Local tracking (backup if Redis unavailable)
        self._local_degraded: dict[int, DegradedEntryInfo] = {}

        # Stats
        self._marked_count = 0
        self._reconciled_count = 0

    def mark_degraded(
        self,
        entry: dict[str, Any],
        reason: str,
        tier: str = "unknown",
    ) -> dict[str, Any]:
        """
        Mark an entry as degraded.

        Adds degraded metadata to the entry and tracks it for reconciliation.

        Args:
            entry: Entry to mark
            reason: Reason for degradation
            tier: Which tier the entry was written to

        Returns:
            Entry with degraded marking
        """
        timestamp = utc_now().isoformat()
        pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

        if "integrity" not in entry:
            entry["integrity"] = {}

        entry["integrity"]["degraded"] = True
        entry["integrity"]["degraded_reason"] = reason
        entry["integrity"]["degraded_at"] = timestamp
        entry["integrity"]["degraded_tier"] = tier
        entry["integrity"]["degraded_pod_id"] = pod_id

        # Track locally
        sequence = entry["integrity"].get("sequence", -1)
        with self._lock:
            self._local_degraded[sequence] = DegradedEntryInfo(
                sequence=sequence,
                original_tier=tier,
                degraded_reason=reason,
                degraded_at=timestamp,
                pod_id=pod_id,
            )
            self._marked_count += 1

        # Track in Redis if available
        if self._redis:
            try:
                self._track_in_redis(sequence, entry["integrity"])
            except Exception as e:
                logger.debug(
                    "degraded_marker.redis_tracking_failed",
                    error=e,
                )

        return entry

    def _track_in_redis(self, sequence: int, integrity: dict[str, Any]) -> None:
        """Track degraded entry in Redis."""
        assert self._redis is not None  # mark_degraded() caller guards on truthy check
        key = f"{self._key_prefix}audit:hash_chain:degraded:{sequence}"
        self._redis.hset(
            key,
            mapping={
                "sequence": str(sequence),
                "reason": integrity.get("degraded_reason", "unknown"),
                "tier": integrity.get("degraded_tier", "unknown"),
                "degraded_at": integrity.get("degraded_at", ""),
                "pod_id": integrity.get("degraded_pod_id", "unknown"),
                "reconciled": "false",
            },
        )
        # TTL for cleanup (7 days)
        self._redis.expire(key, 7 * 24 * 3600)

    def mark_reconciled(self, original_sequence: int, new_sequence: int) -> bool:
        """
        Mark a degraded entry as reconciled.

        Args:
            original_sequence: Original degraded sequence
            new_sequence: New sequence after reconciliation

        Returns:
            True if successfully marked
        """
        timestamp = utc_now().isoformat()

        with self._lock:
            if original_sequence in self._local_degraded:
                self._local_degraded[original_sequence].reconciled = True
                self._local_degraded[original_sequence].reconciled_at = timestamp
                self._local_degraded[original_sequence].new_sequence = new_sequence
                self._reconciled_count += 1

        # Update Redis if available
        if self._redis:
            try:
                key = f"{self._key_prefix}audit:hash_chain:degraded:{original_sequence}"
                self._redis.hset(
                    key,
                    mapping={
                        "reconciled": "true",
                        "reconciled_at": timestamp,
                        "new_sequence": str(new_sequence),
                    },
                )
                return True
            except Exception as e:
                logger.debug(
                    "degraded_marker.redis_update_failed",
                    error=e,
                )

        return True

    def get_unreconciled_entries(self) -> list[DegradedEntryInfo]:
        """Get all entries that haven't been reconciled."""
        with self._lock:
            return [
                info for info in self._local_degraded.values() if not info.reconciled
            ]

    def get_unreconciled_count(self) -> int:
        """Get count of unreconciled entries."""
        with self._lock:
            return sum(
                1 for info in self._local_degraded.values() if not info.reconciled
            )

    def clear_reconciled(self) -> int:
        """Remove reconciled entries from tracking."""
        with self._lock:
            to_remove = [
                seq for seq, info in self._local_degraded.items() if info.reconciled
            ]
            for seq in to_remove:
                del self._local_degraded[seq]
            return len(to_remove)

    def get_stats(self) -> dict[str, Any]:
        """Get marker statistics."""
        return {
            "marked_count": self._marked_count,
            "reconciled_count": self._reconciled_count,
            "unreconciled_count": self.get_unreconciled_count(),
            "tracking_size": len(self._local_degraded),
        }


__all__ = ["DegradedEntryInfo", "DegradedEntryMarker"]
