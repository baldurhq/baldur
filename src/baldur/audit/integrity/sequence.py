"""
Pending Sequence Manager.

Contains:
- PendingSequenceManager: Tracks incomplete hash chain writes to ensure atomicity

Reference:
    92_CONFIG_IMPLEMENTATION_GUIDE.md Week 4 [25] AuditIntegritySettings 참조.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.settings.audit_integrity import get_audit_integrity_settings

logger = structlog.get_logger()


def _get_pending_ttl_seconds() -> int:
    """Get pending TTL from settings."""
    return get_audit_integrity_settings().pending_ttl_seconds


def _get_orphan_ttl_seconds() -> int:
    """Get orphan TTL from settings."""
    return get_audit_integrity_settings().orphan_ttl_seconds


class PendingSequenceManager:
    """
    Tracks incomplete hash chain writes to ensure atomicity.

    Problem Solved:
        When writing an audit log entry, two operations must succeed:
        1. Update Redis with new sequence/hash (distributed state)
        2. Write entry to local file (persistent storage)

        If step 2 fails after step 1 succeeds, the hash chain has a "ghost"
        sequence - a number that was assigned but never written to disk.

    Solution - Write-Ahead Checkpoint:
        1. Before file write: Mark sequence as PENDING in Redis
        2. On file write success: Remove PENDING marker (committed)
        3. On file write failure: Move to ORPHANED for later recovery

    Redis Key Structure:
        {prefix}audit:hash_chain:pending:{sequence} -> expected_hash (TTL: 30s)
        {prefix}audit:hash_chain:orphaned:{sequence} -> hash (TTL: 24h)

    Auto-Cleanup:
        PENDING keys have short TTL (30s) for crash recovery.
        If a process crashes mid-write, the PENDING key expires
        and StartupHashChainSync will handle cleanup.
    """

    PENDING_KEY_PREFIX = "audit:hash_chain:pending:"
    ORPHANED_KEY_PREFIX = "audit:hash_chain:orphaned:"

    # Legacy constants for backward compatibility
    DEFAULT_PENDING_TTL_SECONDS = 30
    DEFAULT_ORPHAN_TTL_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        pending_ttl_seconds: int | None = None,
        orphan_ttl_seconds: int | None = None,
    ):
        """
        Initialize PendingSequenceManager.

        Args:
            redis_client: Redis client instance
            key_prefix: Key prefix for Redis keys
            pending_ttl_seconds: TTL for PENDING keys (default from AuditIntegritySettings)
            orphan_ttl_seconds: TTL for ORPHANED keys (default from AuditIntegritySettings)
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._pending_ttl = (
            pending_ttl_seconds
            if pending_ttl_seconds is not None
            else _get_pending_ttl_seconds()
        )
        self._orphan_ttl = (
            orphan_ttl_seconds
            if orphan_ttl_seconds is not None
            else _get_orphan_ttl_seconds()
        )
        self._local_lock = threading.RLock()

    def _get_pending_key(self, sequence: int) -> str:
        """Build Redis key for PENDING state."""
        return f"{self._key_prefix}{self.PENDING_KEY_PREFIX}{sequence}"

    def _get_orphaned_key(self, sequence: int) -> str:
        """Build Redis key for ORPHANED state."""
        return f"{self._key_prefix}{self.ORPHANED_KEY_PREFIX}{sequence}"

    def reserve_sequence(self, sequence: int, expected_hash: str) -> bool:
        """
        Reserve a sequence as PENDING before file write.

        Stores the expected hash for later verification if recovery needed.
        Uses atomic SET with NX to prevent duplicate reservations.

        Args:
            sequence: The sequence number being written
            expected_hash: The computed hash for this entry

        Returns:
            True if reservation successful, False if already reserved
        """
        try:
            pending_key = self._get_pending_key(sequence)

            # Store expected hash with TTL for auto-cleanup
            # Use SET NX (only if not exists) for atomicity
            result = self._redis.set(
                pending_key,
                expected_hash,
                nx=True,  # Only set if key doesn't exist
                ex=self._pending_ttl,  # Expire after TTL
            )

            if result:
                logger.debug(
                    "pending_seq.reserved_sequence",
                    sequence=sequence,
                )
                return True
            logger.warning(
                "pending_seq.sequence_already_reserved",
                sequence=sequence,
            )
            return False

        except Exception as e:
            logger.exception(
                "pending_seq.reserve_sequence_failed",
                sequence=sequence,
                error=e,
            )
            return False

    def commit_sequence(self, sequence: int) -> bool:
        """
        Commit a sequence after successful file write.

        Removes the PENDING key, marking the transaction as complete.

        Args:
            sequence: The sequence number that was successfully written

        Returns:
            True if committed (key was deleted), False otherwise
        """
        try:
            pending_key = self._get_pending_key(sequence)
            deleted = self._redis.delete(pending_key)

            if deleted:
                logger.debug(
                    "pending_seq.committed_sequence",
                    sequence=sequence,
                )
                return True
            # Key may have expired (TTL) - still considered success
            logger.debug(
                "pending_seq.sequence_already_committed_expired",
                sequence=sequence,
            )
            return True

        except Exception as e:
            logger.exception(
                "pending_seq.commit_sequence_failed",
                sequence=sequence,
                error=e,
            )
            return False

    def abort_sequence(self, sequence: int) -> bool:
        """
        Abort a sequence after failed file write.

        Moves the sequence from PENDING to ORPHANED for later reconciliation.
        The expected hash is preserved for recovery verification.

        Args:
            sequence: The sequence number that failed to write

        Returns:
            True if aborted successfully
        """
        try:
            pending_key = self._get_pending_key(sequence)
            orphaned_key = self._get_orphaned_key(sequence)

            # Get expected hash before deleting PENDING
            expected_hash = self._redis.get(pending_key)
            if isinstance(expected_hash, bytes):
                expected_hash = expected_hash.decode("utf-8")

            # Atomic transition: PENDING -> ORPHANED
            pipe = self._redis.pipeline()
            pipe.delete(pending_key)
            pipe.set(
                orphaned_key,
                expected_hash or "unknown",
                ex=self._orphan_ttl,
            )
            pipe.execute()

            logger.warning(
                "pending_seq.aborted_sequence_orphaned",
                sequence=sequence,
            )
            return True

        except Exception as e:
            logger.exception(
                "pending_seq.abort_sequence_failed",
                sequence=sequence,
                error=e,
            )
            return False

    def get_pending_sequences(self) -> list[int]:
        """
        Get all sequences currently in PENDING state.

        Used during startup to identify incomplete transactions.

        Returns:
            Sorted list of pending sequence numbers
        """
        try:
            pattern = f"{self._key_prefix}{self.PENDING_KEY_PREFIX}*"
            keys = self._redis.keys(pattern)

            sequences = []
            for key in keys:
                try:
                    key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                    # Extract sequence number from key
                    seq_str = key_str.split(":")[-1]
                    sequences.append(int(seq_str))
                except (ValueError, IndexError):
                    continue

            return sorted(sequences)

        except Exception as e:
            logger.exception(
                "pending_seq.get_pending_sequences_failed",
                error=e,
            )
            return []

    def get_orphaned_sequences(self) -> list[int]:
        """
        Get all sequences currently in ORPHANED state.

        Used during reconciliation to find entries needing recovery.

        Returns:
            Sorted list of orphaned sequence numbers
        """
        try:
            pattern = f"{self._key_prefix}{self.ORPHANED_KEY_PREFIX}*"
            keys = self._redis.keys(pattern)

            sequences = []
            for key in keys:
                try:
                    key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                    seq_str = key_str.split(":")[-1]
                    sequences.append(int(seq_str))
                except (ValueError, IndexError):
                    continue

            return sorted(sequences)

        except Exception as e:
            logger.exception(
                "pending_seq.get_orphaned_sequences_failed",
                error=e,
            )
            return []

    def get_expected_hash(self, sequence: int) -> str | None:
        """
        Get the expected hash for a PENDING or ORPHANED sequence.

        Args:
            sequence: Sequence number to lookup

        Returns:
            Expected hash string, or None if not found
        """
        try:
            # Check PENDING first, then ORPHANED
            for key in [
                self._get_pending_key(sequence),
                self._get_orphaned_key(sequence),
            ]:
                value = self._redis.get(key)
                if value:
                    return value.decode("utf-8") if isinstance(value, bytes) else value
            return None

        except Exception as e:
            logger.exception(
                "pending_seq.get_expected_hash_failed",
                sequence=sequence,
                error=e,
            )
            return None

    def cleanup_stale_pending(self, max_age_seconds: int | None = None) -> int:
        """
        Cleanup stale PENDING entries.

        Note: TTL handles automatic cleanup, but this provides explicit cleanup
        option during startup or maintenance.

        Args:
            max_age_seconds: Max age before considering stale (uses TTL by default)

        Returns:
            Number of entries cleaned up
        """
        # With TTL-based expiration, Redis handles this automatically
        # This method exists for explicit cleanup scenarios
        pending = self.get_pending_sequences()

        if not pending:
            return 0

        cleaned = 0
        for seq in pending:
            # Move old PENDING to ORPHANED for safety
            self.abort_sequence(seq)
            cleaned += 1

        if cleaned:
            logger.info(
                "pending_seq.cleaned_up_stale_pending",
                cleaned=cleaned,
            )

        return cleaned

    def clear_orphaned(self, sequence: int) -> bool:
        """
        Clear an ORPHANED sequence after successful reconciliation.

        Args:
            sequence: The reconciled sequence number

        Returns:
            True if cleared
        """
        try:
            orphaned_key = self._get_orphaned_key(sequence)
            self._redis.delete(orphaned_key)
            logger.debug(
                "pending_seq.cleared_orphaned_sequence",
                sequence=sequence,
            )
            return True

        except Exception as e:
            logger.exception(
                "pending_seq.clear_orphaned_failed",
                sequence=sequence,
                error=e,
            )
            return False


__all__ = ["PendingSequenceManager"]
