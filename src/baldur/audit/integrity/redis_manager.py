"""
Redis-based Distributed Hash Chain Manager.

Contains:
- RedisHashChainManager: Distributed hash chain manager for multi-pod environments
- write_chain_state: the single writer of the chain's Redis sequence/state pair
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.integrity.models import (
    compute_hash,
    record_source_reset,
    sanitize_integrity_annotations,
)
from baldur.audit.integrity.verifier import HashChainVerifier
from baldur.core.exceptions import HashChainSequenceRefusedError
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.integrity.ledger_tail import LedgerTail, LedgerTailReader
    from baldur.audit.integrity.local_manager import HashChainManager


logger = structlog.get_logger()

# The chain's Redis key suffixes. Named once here because three collaborators
# read or write them — the manager, the boot reconciliation and the write-time
# repair — and a second spelling is how a reconciliation ends up reconciling a
# key nothing writes.
CHAIN_SEQUENCE_KEY = "audit:hash_chain:seq"
CHAIN_STATE_KEY = "audit:hash_chain:state"
CHAIN_LOCK_KEY = "audit:hash_chain:lock"

# Auto-expiry of the chain's distributed lock, and how long a peer waits for
# it. Shared with the boot reconciliation, which rewinds the counter under the
# same lock.
CHAIN_LOCK_TIMEOUT_SECONDS = 5.0
CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS = 10.0

# ``synced_from`` marker for a state write made by the write-time recovery, as
# opposed to the boot reconciliation's ``file_recovery``.
CHAIN_STATE_WRITE_TIME_RECOVERY = "write_time_recovery"


def chain_namespace_prefix(root_prefix: str, partition: str) -> str:
    """Build the Redis key prefix a distributed chain writes under.

    The chain's keys are namespaced by partition so two services sharing one
    Redis keep independent counters. Every reader of those keys derives the
    prefix here rather than re-spelling the expression, because a second
    derivation is how a reconciliation ends up reading a key the writer never
    writes.

    Args:
        root_prefix: The installation-wide Redis key root (e.g. ``"baldur:"``).
        partition: The per-service partition identifier. Empty selects the
            ``default`` namespace, matching the un-partitioned deployment.

    Returns:
        The prefix to hand :class:`RedisHashChainManager` as ``key_prefix``.
    """
    return f"{root_prefix}hashchain:{partition or 'default'}:"


def build_chain_lock(
    redis_client: Any,
    key_prefix: str,
    *,
    timeout_seconds: float = CHAIN_LOCK_TIMEOUT_SECONDS,
    blocking_timeout: float = CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS,
) -> Any:
    """Build the chain's distributed lock for one key namespace.

    Every writer of the sequence/state pair — the per-write mint, the
    write-time repair and the boot rewind — serializes on this one lock, so
    the key it hangs off is derived here rather than re-spelled per caller.

    Args:
        redis_client: The Redis client the chain writes through.
        key_prefix: The chain's partition-namespaced key prefix.
        timeout_seconds: Lock auto-expire time (prevents deadlocks).
        blocking_timeout: Max time to wait for acquisition.

    Returns:
        An unacquired ``RedisDistributedLock``.
    """
    from datetime import timedelta

    from baldur.adapters.cache.redis_adapter import RedisDistributedLock

    return RedisDistributedLock(
        redis_client=redis_client,
        full_key=f"{key_prefix}{CHAIN_LOCK_KEY}",
        timeout=timedelta(seconds=timeout_seconds),
        blocking_timeout=blocking_timeout,
    )


def write_chain_state(
    redis_client: Any,
    key_prefix: str,
    sequence: int,
    previous_hash: str,
    *,
    synced_from: str,
) -> None:
    """Set the chain's Redis counter and state hash together.

    The one writer of that pair's shape. Both the boot reconciliation and the
    write-time repair need to re-anchor Redis to a known point, and two
    spellings of "counter plus state mapping" is how the two drift apart.

    Args:
        redis_client: The Redis client the chain writes through.
        key_prefix: The chain's partition-namespaced key prefix.
        sequence: The sequence to anchor the counter at.
        previous_hash: The hash the next entry must link to.
        synced_from: Provenance marker recorded on the state hash.

    Raises:
        Exception: Whatever the client raises — the caller decides whether
            that is a fallback or a failure.
    """
    pipe = redis_client.pipeline()
    pipe.set(f"{key_prefix}{CHAIN_SEQUENCE_KEY}", sequence)
    pipe.hset(
        f"{key_prefix}{CHAIN_STATE_KEY}",
        mapping={
            "previous_hash": previous_hash,
            "sequence": str(sequence),
            "updated_at": utc_now().isoformat(),
            "synced_from": synced_from,
        },
    )
    pipe.execute()


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

    Sequence-source recovery:
        Redis is the sequence source and the ledger files are what it orders.
        A Redis that restarted without persistence, failed over to a blank
        replica, or evicted the pair under an ``allkeys-*`` policy hands out
        numbers the ledger already holds. With a ledger reader wired, every
        write compares the minted number and the state hash against the
        ledger's own tail inside the same lock that minted them, re-anchors
        Redis when either is behind, and stamps the repair on the entry that
        continues the chain.
    """

    SEQUENCE_KEY = CHAIN_SEQUENCE_KEY
    STATE_KEY = CHAIN_STATE_KEY
    LOCK_KEY = CHAIN_LOCK_KEY
    GENESIS_HASH = "GENESIS"

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        fallback_manager: HashChainManager | None = None,
        lock_timeout_seconds: float = CHAIN_LOCK_TIMEOUT_SECONDS,
        lock_blocking_timeout: float = CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS,
        ledger: LedgerTailReader | None = None,
    ):
        """
        Initialize Redis-based distributed hash chain manager.

        Args:
            redis_client: Redis client instance (from ResilientStorageBackend or direct)
            key_prefix: Key prefix for Redis keys
            fallback_manager: Local HashChainManager for Redis failure fallback
            lock_timeout_seconds: Lock auto-expire time (prevents deadlocks)
            lock_blocking_timeout: Max time to wait for lock acquisition
            ledger: Reader for the ledger this chain's entries land in. The
                adapter that owns the files supplies it; ``None`` leaves the
                write-time recovery inert.
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._fallback = fallback_manager
        self._lock_timeout_seconds = lock_timeout_seconds
        self._lock_blocking_timeout = lock_blocking_timeout
        self._ledger = ledger
        self._local_lock = threading.RLock()

        # Last posture this manager published. ``None`` until the first write,
        # which therefore always publishes — that is what turns a
        # construction-time admission-probe 1 into 0 once Redis answers.
        self._published_degraded: bool | None = None

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

        Raises:
            HashChainSequenceRefusedError: The ledger exists but its tail
                cannot be read, so no sequence can be minted safely. Never
                routed into the fallback — a source that cannot see the ledger
                is exactly the source that must not mint.
        """
        with self._local_lock:
            try:
                result = self._add_integrity_redis(entry)
            except HashChainSequenceRefusedError:
                raise
            except Exception as e:
                # Per-entry traceability lives in the ledger's own ``degraded``
                # stamp; the operator-facing record is the one-per-episode
                # posture transition below.
                logger.debug(
                    "redis_hash_chain.redis_failed_using_fallback",
                    error=e,
                )
                fallback_entry = self._add_integrity_fallback(entry)
                # Counted only once the entry exists: a fallback that ends in a
                # refusal writes nothing and must count nothing.
                self._stats["fallback_writes"] += 1
                self._count_fallback_write()
                self._publish_posture(degraded=True)
                return fallback_entry
            else:
                self._publish_posture(degraded=False)
                return result

    def _read_ledger_tail(self) -> LedgerTail | None:
        """Read the ledger tail, or refuse the write when it cannot be read.

        Raises:
            HashChainSequenceRefusedError: The ledger exists but is
                unreadable. Raised before the ``INCR``, so a refusal consumes
                no sequence.
        """
        if self._ledger is None:
            return None
        try:
            return self._ledger.read()
        except OSError as e:
            raise HashChainSequenceRefusedError(
                manager="redis",
                log_dir=str(self._ledger.log_dir),
                filename_pattern=self._ledger.filename_pattern,
                error=str(e),
            ) from e

    def _add_integrity_redis(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using Redis with distributed lock."""
        seq_key = self._get_full_key(self.SEQUENCE_KEY)
        state_key = self._get_full_key(self.STATE_KEY)

        lock = build_chain_lock(
            self._redis,
            self._key_prefix,
            timeout_seconds=self._lock_timeout_seconds,
            blocking_timeout=self._lock_blocking_timeout,
        )

        if not lock.acquire(blocking=True):
            self._stats["lock_failures"] += 1
            raise RuntimeError("Failed to acquire hash chain distributed lock")

        try:
            # 0. Read the ledger's own tail before touching the source, so a
            #    refusal consumes no sequence.
            tail = self._read_ledger_tail()

            # 1. Atomic sequence increment
            sequence = self._redis.incr(seq_key)

            # 2. Get previous hash
            previous_hash = self._redis.hget(state_key, "previous_hash")
            if isinstance(previous_hash, bytes):
                previous_hash = previous_hash.decode("utf-8")

            # 3. Compare what the source minted against what the ledger holds
            annotations, sequence, previous_hash = self._reconcile_with_ledger(
                tail, sequence, previous_hash, seq_key
            )

            # 4. Add integrity fields — annotations ride *under* the hash
            timestamp = utc_now().isoformat()
            pod_id = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))

            entry["integrity"] = {
                **sanitize_integrity_annotations(annotations),
                "sequence": sequence,
                "previous_hash": previous_hash,
                "timestamp": timestamp,
                "pod_id": pod_id,
            }

            # 5. Compute current hash
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash

            # 6. Save state for next entry
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
            # Always release the lock — but a release that raises (Redis gone
            # after the acquire) must not replace the exception the write
            # actually failed with, least of all a refusal, which would then be
            # routed into the fallback. The key expires on its own PX.
            try:
                lock.release()
            except Exception as e:
                logger.debug(
                    "redis_hash_chain.lock_release_failed",
                    error=e,
                )

    def _reconcile_with_ledger(
        self,
        tail: LedgerTail | None,
        sequence: int,
        previous_hash: str | None,
        seq_key: str,
    ) -> tuple[dict[str, Any], int, str]:
        """Re-anchor Redis to the ledger tail when the source is behind it.

        Runs inside the chain's distributed lock, in the same critical section
        as the ``INCR`` it corrects.

        Args:
            tail: The ledger's highest entry, or ``None`` for a fresh ledger.
            sequence: The number ``INCR`` just returned.
            previous_hash: The state hash, or ``None`` when the field is gone.
            seq_key: The counter key, for the post-repair re-increment.

        Returns:
            ``(annotations, sequence, previous_hash)`` for the entry to carry.
        """
        if tail is None:
            return {}, sequence, previous_hash or self.GENESIS_HASH

        if sequence <= tail.sequence:
            # Sequence arm: a monotonic source never mints at or below a
            # number already on disk, so the source is behind the ledger —
            # reset, rolled back, or bypassed by a fallback episode that
            # appended past it.
            reason = (
                "counter_reset"
                if sequence == 1 and not previous_hash
                else "source_behind_ledger"
            )
            write_chain_state(
                self._redis,
                self._key_prefix,
                tail.sequence,
                tail.current_hash,
                synced_from=CHAIN_STATE_WRITE_TIME_RECOVERY,
            )
            repaired_sequence = self._redis.incr(seq_key)
            annotations = record_source_reset(
                manager="redis",
                reason=reason,
                observed=sequence,
                adopted=tail.sequence,
                ledger_path=str(tail.path),
            )
            return annotations, repaired_sequence, tail.current_hash

        if not previous_hash or (
            sequence == tail.sequence + 1 and previous_hash != tail.current_hash
        ):
            # Hash arm: the counter survived but the state hash did not, or an
            # ``INCR`` succeeded before an ``HSET`` that failed. GENESIS or a
            # stale hash mid-chain is certainly wrong; the tail hash is right
            # on a single ledger and no worse on a multi-host merge.
            reason = "state_hash_lost" if not previous_hash else "state_hash_stale"
            write_chain_state(
                self._redis,
                self._key_prefix,
                sequence,
                tail.current_hash,
                synced_from=CHAIN_STATE_WRITE_TIME_RECOVERY,
            )
            annotations = record_source_reset(
                manager="redis",
                reason=reason,
                observed=sequence,
                adopted=tail.sequence,
                ledger_path=str(tail.path),
            )
            return annotations, sequence, tail.current_hash

        return {}, sequence, previous_hash

    def _publish_posture(self, *, degraded: bool) -> None:
        """Publish the live distributed-chain posture when it changes.

        The gauge is primed at construction by the admission probe and then
        never moved, so a chain that recovered read ``1`` forever and a chain
        that fell back after a healthy admission read ``0``. Publishing on
        every change of outcome — including the first write of a process,
        which has nothing to compare against — is what makes the series answer
        "is this chain distributed right now".
        """
        previous = self._published_degraded
        if previous == degraded:
            return
        self._published_degraded = degraded

        if degraded:
            logger.warning("redis_hash_chain.fallback_entered")
        elif previous is True:
            logger.info("redis_hash_chain.redis_restored")

        try:
            from baldur.metrics.audit_backend_metrics import (
                set_audit_distributed_chain_degraded,
            )

            set_audit_distributed_chain_degraded(degraded)
        except Exception as e:
            logger.debug("redis_hash_chain.degraded_gauge_skipped", error=str(e))

    def _count_fallback_write(self) -> None:
        """Count one fallback-sequenced entry. Fail-open."""
        try:
            from baldur.metrics.audit_backend_metrics import (
                increment_audit_hash_chain_fallback_write,
            )

            increment_audit_hash_chain_fallback_write()
        except Exception as e:
            logger.debug("redis_hash_chain.fallback_metric_skipped", error=str(e))

    def _add_integrity_fallback(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add integrity using local fallback manager.

        The stamps are passed **into** the local manager so they are hashed
        with the entry. Added afterwards they would sit on top of the computed
        hash, and every fallback entry would verify as modified.
        """
        if self._fallback:
            return self._fallback.add_integrity(
                entry,
                annotations={"degraded": True, "fallback_source": "local"},
            )

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

        Clears the source only. With a ledger reader wired, the next write
        re-anchors to the ledger's own tail unless the ledger files are
        removed as well — a reset alone does not restart the chain at 1.
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


__all__ = [
    "CHAIN_LOCK_BLOCKING_TIMEOUT_SECONDS",
    "CHAIN_LOCK_KEY",
    "CHAIN_LOCK_TIMEOUT_SECONDS",
    "CHAIN_SEQUENCE_KEY",
    "CHAIN_STATE_KEY",
    "CHAIN_STATE_WRITE_TIME_RECOVERY",
    "RedisHashChainManager",
    "build_chain_lock",
    "chain_namespace_prefix",
    "write_chain_state",
]
