"""
Hash Chain Fallback Chain.

Contains:
- HashChainFallbackChain: Multi-tier fallback (Redis → Replica → Local → Memory)
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta
from typing import Any

import structlog

from baldur.audit.graceful_degradation.enums import FallbackConfig
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class HashChainFallbackChain:
    """
    Multi-tier fallback chain for hash chain operations.

    .. note::
        **Tier**: Dormant (compliance-grade enhancement, no standalone demand)
        **Status**: Not auto-wired. Available for custom integration engagements.
        PRO Audit (full) uses the basic file-based ``HashChainManager`` in
        ``audit/integrity/`` instead.

    Fallback order:
    1. Redis Primary - Full distributed functionality
    2. Redis Replica - Read-only, degraded writes to local
    3. Local File - Persistent but not distributed
    4. Memory Buffer - Last resort, volatile

    Each fallback level marks entries as degraded for later reconciliation.

    Pattern source:
        adapters/resilient/backend.py#L183-230

    Usage:
        fallback = HashChainFallbackChain(redis_primary, config)
        entry = fallback.add_integrity(entry)  # Auto-fallback on failure
    """

    GENESIS_HASH = "GENESIS"

    def __init__(
        self,
        redis_primary: Any | None = None,
        redis_replica: Any | None = None,
        config: FallbackConfig | None = None,
    ):
        """
        Initialize fallback chain.

        Args:
            redis_primary: Primary Redis client
            redis_replica: Replica Redis client (optional)
            config: Fallback configuration
        """
        self._redis_primary = redis_primary
        self._redis_replica = redis_replica
        self._config = config or FallbackConfig()
        self._lock = threading.RLock()

        # Local fallback state
        self._local_sequence = 0
        self._local_previous_hash = self.GENESIS_HASH
        self._local_file_handle: Any = None  # lazy-opened TextIOWrapper

        # Memory buffer (last resort)
        self._memory_buffer: list[dict[str, Any]] = []
        self._memory_sequence = 0
        self._memory_previous_hash = self.GENESIS_HASH

        # Current tier tracking
        self._current_tier = "redis_primary" if redis_primary else "local"
        self._tier_switch_count = 0

        # Stats
        self._stats = {
            "primary_writes": 0,
            "replica_reads": 0,
            "local_writes": 0,
            "memory_writes": 0,
            "fallback_events": 0,
        }

    def add_integrity(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity fields with automatic fallback.

        Tries each tier in order until one succeeds.
        Failed attempts are logged and the entry is marked as degraded.

        Args:
            entry: Log entry dictionary

        Returns:
            Entry with integrity fields added
        """
        # Try Redis Primary
        if self._redis_primary:
            try:
                result = self._add_integrity_redis_primary(entry)
                self._current_tier = "redis_primary"
                self._stats["primary_writes"] += 1
                return result
            except Exception as e:
                logger.warning(
                    "fallback_chain.primary_failed",
                    error=e,
                )
                self._stats["fallback_events"] += 1

        # Try Redis Replica (read state, write locally)
        if self._redis_replica:
            try:
                result = self._add_integrity_with_replica(entry)
                self._current_tier = "redis_replica"
                self._stats["replica_reads"] += 1
                return result
            except Exception as e:
                logger.warning(
                    "fallback_chain.replica_failed",
                    error=e,
                )
                self._stats["fallback_events"] += 1

        # Try Local File
        try:
            result = self._add_integrity_local(entry)
            self._current_tier = "local"
            self._stats["local_writes"] += 1
            return result
        except Exception as e:
            logger.warning(
                "fallback_chain.local_failed",
                error=e,
            )
            self._stats["fallback_events"] += 1

        # Last resort: Memory Buffer
        result = self._add_integrity_memory(entry)
        self._current_tier = "memory"
        self._stats["memory_writes"] += 1
        return result

    def _add_integrity_redis_primary(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using Redis primary."""
        from baldur.adapters.cache.redis_adapter import RedisDistributedLock

        assert self._redis_primary is not None  # add_integrity() caller guard
        seq_key = f"{self._config.key_prefix}audit:hash_chain:seq"
        state_key = f"{self._config.key_prefix}audit:hash_chain:state"
        lock_key = f"{self._config.key_prefix}audit:hash_chain:lock"

        lock = RedisDistributedLock(
            redis_client=self._redis_primary,
            full_key=lock_key,
            timeout=timedelta(seconds=self._config.redis_timeout_seconds),
            blocking_timeout=self._config.redis_timeout_seconds,
        )

        if not lock.acquire(blocking=True):
            raise RuntimeError("Failed to acquire distributed lock")

        try:
            # Atomic sequence increment
            sequence = self._redis_primary.incr(seq_key)

            # Get previous hash
            previous_hash = self._redis_primary.hget(state_key, "previous_hash")
            if previous_hash:
                if isinstance(previous_hash, bytes):
                    previous_hash = previous_hash.decode("utf-8")
            else:
                previous_hash = self.GENESIS_HASH

            # Add integrity fields
            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            entry["integrity"] = {
                "sequence": sequence,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": pod_id,
                "tier": "redis_primary",
            }

            # Compute current hash
            current_hash = self._compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash

            # Save state for next entry
            self._redis_primary.hset(
                state_key,
                mapping={
                    "previous_hash": current_hash,
                    "sequence": str(sequence),
                    "updated_at": timestamp,
                },
            )

            return entry

        finally:
            lock.release()

    def _add_integrity_with_replica(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity reading from replica, marking as degraded.

        Replica is read-only, so we read state but can't update.
        Entry is marked degraded for later reconciliation.
        """
        assert self._redis_replica is not None  # add_integrity() caller guard
        state_key = f"{self._config.key_prefix}audit:hash_chain:state"

        # Read current state from replica
        state = self._redis_replica.hgetall(state_key)

        if state:
            sequence = int(state.get(b"sequence", state.get("sequence", 0)))
            previous_hash = state.get(
                b"previous_hash", state.get("previous_hash", self.GENESIS_HASH)
            )
            if isinstance(previous_hash, bytes):
                previous_hash = previous_hash.decode("utf-8")
        else:
            sequence = 0
            previous_hash = self.GENESIS_HASH

        # Add integrity fields (sequence is temporary, will be reassigned on reconciliation)
        with self._lock:
            self._local_sequence = max(self._local_sequence, sequence) + 1
            local_seq = self._local_sequence

        timestamp = utc_now().isoformat()
        pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

        entry["integrity"] = {
            "sequence": local_seq,
            "previous_hash": previous_hash,
            "timestamp": timestamp,
            "pod_id": pod_id,
            "tier": "redis_replica",
            "degraded": True,
            "degraded_reason": "redis_primary_unavailable",
            "degraded_at": timestamp,
        }

        current_hash = self._compute_hash(entry)
        entry["integrity"]["current_hash"] = current_hash

        # Update local state for chain continuity
        with self._lock:
            self._local_previous_hash = current_hash

        return entry

    def _add_integrity_local(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using local file fallback."""
        with self._lock:
            self._local_sequence += 1
            sequence = self._local_sequence
            previous_hash = self._local_previous_hash

            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            entry["integrity"] = {
                "sequence": sequence,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": pod_id,
                "tier": "local",
                "degraded": True,
                "degraded_reason": "redis_unavailable",
                "degraded_at": timestamp,
            }

            current_hash = self._compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash
            self._local_previous_hash = current_hash

            # Write to local file if configured
            if self._config.local_file_path:
                self._write_to_local_file(entry)

            return entry

    def _add_integrity_memory(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity using disk-persistent buffer (last resort).

        Uses DiskPersistentBuffer instead of volatile memory buffer.
        Data survives pod restarts.
        """
        # Use DiskPersistentBuffer (auto-switched via BALDUR_BUFFER_TYPE env var)
        try:
            from baldur.audit.persistence.disk_buffer import get_disk_buffer

            disk_buffer = get_disk_buffer()
            use_disk_buffer = True
        except Exception as e:
            logger.warning(
                "fallback_chain.diskbuffer_unavailable_using_memory",
                error=e,
            )
            use_disk_buffer = False

        with self._lock:
            self._memory_sequence += 1
            sequence = self._memory_sequence
            previous_hash = self._memory_previous_hash

            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            # tier and volatile flags depend on whether DiskBuffer is in use
            if use_disk_buffer:
                tier = "disk_buffer"
                volatile = False
            else:
                tier = "memory"
                volatile = True

            entry["integrity"] = {
                "sequence": sequence,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": pod_id,
                "tier": tier,
                "degraded": True,
                "degraded_reason": "all_persistent_storage_unavailable",
                "degraded_at": timestamp,
                "volatile": volatile,
            }

            current_hash = self._compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash
            self._memory_previous_hash = current_hash

            # Store in the DiskBuffer or the memory buffer
            if use_disk_buffer:
                disk_buffer.put(entry.copy())

            # Also keep in memory_buffer (consistency for
            # get_degraded_entries / clear_memory_buffer)
            self._memory_buffer.append(entry.copy())
            if len(self._memory_buffer) > self._config.memory_max_entries:
                removed = self._memory_buffer.pop(0)
                logger.warning(
                    "fallback_chain.memory_buffer_full_dropped",
                    removed=removed.get("integrity", {}).get("sequence"),
                )

            return entry

            return entry

    def _write_to_local_file(self, entry: dict[str, Any]) -> None:
        """Write entry to local fallback file."""
        try:
            if self._local_file_handle is None:
                assert self._config.local_file_path is not None, (
                    "_add_integrity_local() guard ensures non-None"
                )
                self._config.local_file_path.parent.mkdir(parents=True, exist_ok=True)
                self._local_file_handle = open(  # noqa: SIM115
                    self._config.local_file_path, "a", encoding="utf-8"
                )

            line = fast_dumps_str(entry, default=str)
            self._local_file_handle.write(line + "\n")
            self._local_file_handle.flush()

        except Exception as e:
            logger.exception(
                "fallback_chain.local_file_write_failed",
                error=e,
            )

    def _compute_hash(self, entry: dict[str, Any]) -> str:
        """Delegate to the single keyed-aware chain hash (integrity/models).

        Strips ``current_hash`` first (the write-side compute covers the entry
        minus its own hash), then forwards to ``compute_hash`` so this degraded
        write path produces the same keyed hash as the primary path.
        """
        from baldur.audit.integrity.models import compute_hash

        entry_copy = fast_loads(fast_dumps_str(entry, default=str))
        if "integrity" in entry_copy and "current_hash" in entry_copy["integrity"]:
            del entry_copy["integrity"]["current_hash"]
        return compute_hash(entry_copy)

    def get_degraded_entries(self) -> list[dict[str, Any]]:
        """Get all degraded entries from memory buffer."""
        with self._lock:
            return [
                e for e in self._memory_buffer if e.get("integrity", {}).get("degraded")
            ]

    def clear_memory_buffer(self) -> int:
        """Clear memory buffer after successful reconciliation."""
        with self._lock:
            count = len(self._memory_buffer)
            self._memory_buffer.clear()
            return count

    def close(self) -> None:
        """Close file handles."""
        if self._local_file_handle:
            self._local_file_handle.close()
            self._local_file_handle = None

    def __del__(self) -> None:
        """Ensure file handles are closed on garbage collection."""
        self.close()

    @property
    def current_tier(self) -> str:
        """Get current active tier."""
        return self._current_tier

    def get_stats(self) -> dict[str, Any]:
        """Get fallback chain statistics."""
        return {
            **self._stats,
            "current_tier": self._current_tier,
            "memory_buffer_size": len(self._memory_buffer),
            "local_sequence": self._local_sequence,
        }


__all__ = ["HashChainFallbackChain"]
