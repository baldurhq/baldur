"""
Redis-based Distributed Hash Chain Manager.

Contains:
- RedisHashChainManager: Distributed hash chain manager for multi-pod environments
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.integrity.models import compute_hash
from baldur.audit.integrity.verifier import HashChainVerifier
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.integrity.local_manager import HashChainManager


logger = structlog.get_logger()


class RedisHashChainManager:
    """
    Redis-based distributed hash chain manager for multi-pod environments.

    Ensures a single global hash chain across all application instances by:
    - Using Redis INCR for atomic sequence numbering
    - Acquiring distributed lock before hash computation to prevent race conditions
    - Falling back to local HashChainManager if Redis becomes unavailable

    Hash Chain Concept:
        Each audit log entry contains a SHA-256 hash of the previous entry,
        creating a tamper-evident chain. Any modification or deletion
        breaks the chain and is detectable during verification.

    Distributed Safety:
        Multiple pods writing audit logs simultaneously could cause:
        - Duplicate sequence numbers
        - Previous hash mismatches (race condition)

        This manager solves these issues by serializing writes through
        Redis locks and using atomic INCR for sequence generation.
    """

    SEQUENCE_KEY = "audit:hash_chain:seq"
    STATE_KEY = "audit:hash_chain:state"
    LOCK_KEY = "audit:hash_chain:lock"
    GENESIS_HASH = "GENESIS"

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        fallback_manager: HashChainManager | None = None,
        lock_timeout_seconds: float = 5.0,
        lock_blocking_timeout: float = 10.0,
    ):
        """
        Initialize Redis-based distributed hash chain manager.

        Args:
            redis_client: Redis client instance (from ResilientStorageBackend or direct)
            key_prefix: Key prefix for Redis keys
            fallback_manager: Local HashChainManager for Redis failure fallback
            lock_timeout_seconds: Lock auto-expire time (prevents deadlocks)
            lock_blocking_timeout: Max time to wait for lock acquisition
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._fallback = fallback_manager
        self._lock_timeout_seconds = lock_timeout_seconds
        self._lock_blocking_timeout = lock_blocking_timeout
        self._local_lock = threading.RLock()

        # Statistics
        self._stats = {
            "redis_writes": 0,
            "fallback_writes": 0,
            "lock_failures": 0,
        }

    def _get_full_key(self, key: str) -> str:
        """Get full Redis key with prefix."""
        return f"{self._key_prefix}{key}"

    def add_integrity(self, entry: dict[str, Any]) -> dict[str, Any]:
        """
        Add integrity fields to a log entry (distributed-safe).

        Uses RedisDistributedLock to ensure 100% Race Condition safety.
        Falls back to local HashChainManager if Redis is unavailable.

        Args:
            entry: Log entry dictionary

        Returns:
            Entry with integrity fields added
        """
        with self._local_lock:
            try:
                return self._add_integrity_redis(entry)
            except Exception as e:
                logger.warning(
                    "redis_hash_chain.redis_failed_using_fallback",
                    error=e,
                )
                self._stats["fallback_writes"] += 1
                return self._add_integrity_fallback(entry)

    def _add_integrity_redis(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using Redis with distributed lock."""
        from datetime import timedelta

        # Import lock class
        from baldur.adapters.cache.redis_adapter import RedisDistributedLock

        seq_key = self._get_full_key(self.SEQUENCE_KEY)
        state_key = self._get_full_key(self.STATE_KEY)
        lock_key = self._get_full_key(self.LOCK_KEY)

        # Acquire distributed lock
        lock = RedisDistributedLock(
            redis_client=self._redis,
            full_key=lock_key,
            timeout=timedelta(seconds=self._lock_timeout_seconds),
            blocking_timeout=self._lock_blocking_timeout,
        )

        if not lock.acquire(blocking=True):
            self._stats["lock_failures"] += 1
            raise RuntimeError("Failed to acquire hash chain distributed lock")

        try:
            # 1. Atomic sequence increment
            sequence = self._redis.incr(seq_key)

            # 2. Get previous hash
            previous_hash = self._redis.hget(state_key, "previous_hash")
            if previous_hash:
                if isinstance(previous_hash, bytes):
                    previous_hash = previous_hash.decode("utf-8")
            else:
                previous_hash = self.GENESIS_HASH

            # 3. Add integrity fields
            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            entry["integrity"] = {
                "sequence": sequence,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": pod_id,
            }

            # 4. Compute current hash
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash

            # 5. Save state for next entry
            self._redis.hset(
                state_key,
                mapping={
                    "previous_hash": current_hash,
                    "sequence": str(sequence),
                    "updated_at": timestamp,
                },
            )

            self._stats["redis_writes"] += 1
            return entry

        finally:
            # Always release lock
            lock.release()

    def _add_integrity_fallback(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using local fallback manager."""
        if self._fallback:
            result = self._fallback.add_integrity(entry)
            # Mark as degraded for later reconciliation
            if "integrity" in result:
                result["integrity"]["degraded"] = True
                result["integrity"]["fallback_source"] = "local"
            return result

        # No fallback available - add minimal integrity info
        entry["integrity"] = {
            "sequence": -1,  # Indicates needs reordering on recovery
            "previous_hash": "DEGRADED",
            "timestamp": utc_now().isoformat(),
            "pod_id": os.environ.get("HOSTNAME", "unknown"),
            "degraded": True,
            "fallback_source": "none",
        }
        entry["integrity"]["current_hash"] = compute_hash(entry)

        return entry

    def get_state(self) -> dict[str, Any]:
        """Get current chain state from Redis."""
        try:
            state_key = self._get_full_key(self.STATE_KEY)
            state = self._redis.hgetall(state_key)

            if not state:
                return {
                    "sequence": 0,
                    "previous_hash": self.GENESIS_HASH,
                    "source": "redis",
                }

            # Decode bytes if needed
            sequence = state.get(b"sequence", state.get("sequence", 0))
            previous_hash = state.get(
                b"previous_hash", state.get("previous_hash", self.GENESIS_HASH)
            )

            if isinstance(sequence, bytes):
                sequence = int(sequence.decode("utf-8"))
            else:
                sequence = int(sequence) if sequence else 0

            if isinstance(previous_hash, bytes):
                previous_hash = previous_hash.decode("utf-8")

            # Truncate hash for display
            display_hash = (
                previous_hash[:16] + "..." if len(previous_hash) > 16 else previous_hash
            )

            return {
                "sequence": sequence,
                "previous_hash": display_hash,
                "source": "redis",
            }

        except Exception as e:
            logger.warning(
                "redis_hash_chain.get_state_redis_failed",
                error=e,
            )

            if self._fallback:
                state = self._fallback.get_state()
                state["source"] = "fallback"
                return state

            return {
                "sequence": 0,
                "previous_hash": self.GENESIS_HASH,
                "source": "unavailable",
                "error": str(e),
            }

    def get_stats(self) -> dict[str, Any]:
        """Get manager statistics."""
        return {
            **self._stats,
            "state": self.get_state(),
        }

    def verify_continuity(
        self, entries: list[dict[str, Any]]
    ) -> tuple[bool, str | None]:
        """
        Verify hash chain continuity.

        Reuses existing HashChainVerifier.

        Args:
            entries: List of log entries with integrity fields

        Returns:
            Tuple of (is_valid, error_message)
        """
        verifier = HashChainVerifier()
        return verifier.verify_chain(entries)

    def reset(self) -> None:
        """
        Reset chain state in Redis.

        WARNING: Use with extreme caution! This breaks the hash chain.
        Only for testing or disaster recovery.
        """
        try:
            seq_key = self._get_full_key(self.SEQUENCE_KEY)
            state_key = self._get_full_key(self.STATE_KEY)

            self._redis.delete(seq_key)
            self._redis.delete(state_key)

            logger.warning("redis_hash_chain.chain_state_reset_redis")

        except Exception as e:
            logger.exception(
                "redis_hash_chain.reset_failed",
                error=e,
            )

        if self._fallback:
            self._fallback.reset()


__all__ = ["RedisHashChainManager"]
