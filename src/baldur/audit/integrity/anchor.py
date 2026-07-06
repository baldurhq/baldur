"""
Daily Hash Anchor.

Contains:
- DailyHashAnchor: Daily checkpoint for efficient chain verification
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import structlog

from baldur.audit.integrity.models import compute_hash
from baldur.settings.audit_integrity import get_audit_integrity_settings
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_anchor_retention_days() -> int:
    """Get anchor retention days from settings."""
    return get_audit_integrity_settings().anchor_retention_days


class DailyHashAnchor:
    """
    Daily hash anchor system for efficient chain verification.

    Problem:
        Verifying a hash chain with millions of entries is expensive.
        Full verification requires reading and hashing every entry.

    Solution - Daily Anchors:
        Store the chain state (sequence + hash) at the end of each day.
        Verification can then start from any anchor instead of from GENESIS.

        Example:
        - Instead of verifying entries 1-1,000,000
        - Verify: yesterday's anchor + today's entries (maybe 10,000)

    Redis Key Structure:
        {prefix}audit:hash_chain:anchor:{YYYY-MM-DD} -> {sequence, hash, timestamp}

    Retention:
        Anchors are automatically deleted after 90 days (configurable via AuditIntegritySettings).
        This provides sufficient history for audits while limiting storage.
    """

    ANCHOR_KEY_PREFIX = "audit:hash_chain:anchor:"
    STATE_KEY = "audit:hash_chain:state"

    # Legacy constant for backward compatibility
    DEFAULT_RETENTION_DAYS = 90

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        retention_days: int | None = None,
    ):
        """
        Initialize DailyHashAnchor.

        Args:
            redis_client: Redis client instance
            key_prefix: Key prefix for Redis keys
            retention_days: Number of days to retain anchors (default from AuditIntegritySettings)
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._retention_days = (
            retention_days
            if retention_days is not None
            else _get_anchor_retention_days()
        )

    def _get_anchor_key(self, date: str) -> str:
        """Build Redis key for anchor."""
        return f"{self._key_prefix}{self.ANCHOR_KEY_PREFIX}{date}"

    def _get_state_key(self) -> str:
        """Build Redis key for current state."""
        return f"{self._key_prefix}{self.STATE_KEY}"

    def create_anchor(
        self,
        date: str | None = None,
        sequence: int | None = None,
        hash_value: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a daily anchor checkpoint.

        If sequence/hash not provided, reads from current Redis state.

        Args:
            date: Date string (YYYY-MM-DD), defaults to today
            sequence: Sequence number at anchor point
            hash_value: Hash value at anchor point

        Returns:
            Created anchor data dictionary
        """
        if date is None:
            date = utc_now().strftime("%Y-%m-%d")

        # Get current state if not provided
        if sequence is None or hash_value is None:
            state = self._get_current_state()
            if sequence is None:
                sequence = state.get("sequence", 0)
            if hash_value is None:
                hash_value = state.get("previous_hash", "GENESIS")

        anchor_key = self._get_anchor_key(date)
        anchor_data = {
            "date": date,
            "sequence": str(sequence),
            "hash": hash_value,
            "created_at": utc_now().isoformat(),
        }

        try:
            self._redis.hset(anchor_key, mapping=anchor_data)
            # Set TTL for automatic cleanup
            self._redis.expire(anchor_key, self._retention_days * 86400)

            logger.info(
                "daily_anchor.created_anchor",
                date=date,
                sequence=sequence,
            )
            return anchor_data

        except Exception as e:
            logger.exception(
                "daily_anchor.create_anchor_failed",
                date=date,
                error=e,
            )
            return {"error": str(e), "date": date}

    def _get_current_state(self) -> dict[str, Any]:
        """Get current hash chain state from Redis."""
        try:
            state_key = self._get_state_key()
            state = self._redis.hgetall(state_key)

            if not state:
                return {"sequence": 0, "previous_hash": "GENESIS"}

            # Decode bytes
            sequence = state.get(b"sequence", state.get("sequence", 0))
            previous_hash = state.get(
                b"previous_hash", state.get("previous_hash", "GENESIS")
            )

            if isinstance(sequence, bytes):
                sequence = int(sequence.decode("utf-8"))
            else:
                sequence = int(sequence) if sequence else 0

            if isinstance(previous_hash, bytes):
                previous_hash = previous_hash.decode("utf-8")

            return {"sequence": sequence, "previous_hash": previous_hash}

        except Exception as e:
            logger.exception(
                "daily_anchor.get_current_state_failed",
                error=e,
            )
            return {"sequence": 0, "previous_hash": "GENESIS"}

    def get_anchor(self, date: str) -> dict[str, Any] | None:
        """
        Get anchor for a specific date.

        Args:
            date: Date string (YYYY-MM-DD)

        Returns:
            Anchor data dictionary, or None if not found
        """
        try:
            anchor_key = self._get_anchor_key(date)
            data = self._redis.hgetall(anchor_key)

            if not data:
                return None

            # Decode all values
            result = {}
            for key, value in data.items():
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                val_str = value.decode("utf-8") if isinstance(value, bytes) else value
                result[key_str] = val_str

            # Convert sequence to int
            if "sequence" in result:
                result["sequence"] = int(result["sequence"])

            return result

        except Exception as e:
            logger.exception(
                "daily_anchor.get_anchor_failed",
                date=date,
                error=e,
            )
            return None

    def verify_from_anchor(
        self,
        entries: list[dict[str, Any]],
        anchor_date: str,
    ) -> tuple[bool, str | None]:
        """
        Verify chain integrity starting from an anchor.

        Instead of verifying entire chain from GENESIS, verify only from anchor.
        Much faster for large audit logs.

        Args:
            entries: Log entries to verify (must be after anchor date)
            anchor_date: Date of anchor to start verification from

        Returns:
            Tuple of (is_valid, error_message)
        """
        anchor = self.get_anchor(anchor_date)
        if not anchor:
            return False, f"Anchor not found for {anchor_date}"

        if not entries:
            return True, None

        # First entry's previous_hash must match anchor's hash
        first_entry = entries[0]
        first_prev_hash = first_entry.get("integrity", {}).get("previous_hash", "")
        anchor_hash = anchor.get("hash", "")

        if first_prev_hash != anchor_hash:
            return False, (
                f"Chain broken at anchor boundary: "
                f"expected previous_hash={anchor_hash[:16]}..., "
                f"found={first_prev_hash[:16]}..."
            )

        # Verify remaining chain using custom verification from anchor point
        return self._verify_chain_from_point(entries, anchor)

    def _verify_chain_from_point(
        self,
        entries: list[dict[str, Any]],
        anchor: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """
        Verify chain integrity from an anchor point.

        Unlike HashChainVerifier.verify_chain(), this allows chains
        starting from any sequence number (not just 1).

        Args:
            entries: Log entries to verify
            anchor: Anchor data with sequence and hash

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not entries:
            return True, None

        previous_hash = anchor.get("hash", "GENESIS")
        expected_sequence = anchor.get("sequence", 0) + 1

        for entry in entries:
            integrity = entry.get("integrity", {})

            # Check sequence continuity
            seq = integrity.get("sequence", 0)
            if seq != expected_sequence:
                return (
                    False,
                    f"Missing entry: expected sequence {expected_sequence}, found {seq}",
                )

            # Check previous hash linkage
            prev_hash = integrity.get("previous_hash", "")
            if prev_hash != previous_hash:
                return False, f"Chain broken at sequence {seq}: previous_hash mismatch"

            # Verify current hash
            stored_hash = integrity.get("current_hash", "")

            # Create a copy without current_hash for verification
            entry_copy = json.loads(json.dumps(entry))
            if "integrity" in entry_copy and "current_hash" in entry_copy["integrity"]:
                del entry_copy["integrity"]["current_hash"]

            computed_hash = compute_hash(entry_copy)

            if stored_hash != computed_hash:
                return False, f"Entry modified at sequence {seq}: hash mismatch"

            previous_hash = stored_hash
            expected_sequence += 1

        return True, None

    def list_anchors(self, days: int = 7) -> list[dict[str, Any]]:
        """
        List recent anchors.

        Args:
            days: Number of days to look back

        Returns:
            List of anchor data dictionaries
        """
        anchors = []

        for i in range(days):
            date = (utc_now() - timedelta(days=i)).strftime("%Y-%m-%d")
            anchor = self.get_anchor(date)
            if anchor:
                anchors.append(anchor)

        return anchors

    def delete_anchor(self, date: str) -> bool:
        """
        Delete an anchor (for cleanup or testing).

        Args:
            date: Date string (YYYY-MM-DD)

        Returns:
            True if deleted
        """
        try:
            anchor_key = self._get_anchor_key(date)
            self._redis.delete(anchor_key)
            return True
        except Exception:
            return False


__all__ = ["DailyHashAnchor"]
