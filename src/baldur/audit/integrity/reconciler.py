"""
Hash Chain Reconciler.

Contains:
- HashChainReconciler: Merges degraded/fallback entries back into main hash chain
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.models import compute_hash
from baldur.utils.time import utc_now

logger = structlog.get_logger()


class HashChainReconciler:
    """
    Merges degraded/fallback entries back into the main hash chain.

    Problem:
        When Redis is unavailable, audit logs are written with:
        - degraded: true (flag indicating fallback mode)
        - Local-only sequence numbers (not globally coordinated)

        These entries exist in local files but are not part of the
        verified global hash chain.

    Solution - Reconciliation:
        When Redis recovers, this reconciler:
        1. Scans local files for entries with "degraded: true"
        2. Assigns them new global sequence numbers (continuing main chain)
        3. Recomputes their hashes with proper previous_hash linkage
        4. Updates Redis state to include these entries
        5. Marks entries as "reconciled: true" (no longer degraded)

    When to Run:
        - Automatically after Redis recovery
        - Manually via admin command
        - On startup if degraded entries are detected
    """

    SEQUENCE_KEY = "audit:hash_chain:seq"
    STATE_KEY = "audit:hash_chain:state"

    def __init__(
        self,
        redis_client: Any,
        log_dir: Path,
        key_prefix: str = "baldur:",
    ):
        """
        Initialize HashChainReconciler.

        Args:
            redis_client: Redis client instance
            log_dir: Directory containing audit log files
            key_prefix: Key prefix for Redis keys
        """
        self._redis = redis_client
        self._log_dir = Path(log_dir)
        self._key_prefix = key_prefix
        self._last_reconciliation: datetime | None = None

    def reconcile(self) -> dict[str, Any]:
        """
        Perform reconciliation of degraded entries.

        Returns:
            Result dictionary with reconciliation details
        """
        result = {
            "status": "success",
            "degraded_entries_found": 0,
            "entries_merged": 0,
            "new_sequence_start": 0,
            "new_sequence_end": 0,
            "reconciled_at": utc_now().isoformat(),
        }

        try:
            # Step 1: Collect degraded entries from local files
            degraded_entries = self._collect_degraded_entries()
            result["degraded_entries_found"] = len(degraded_entries)

            if not degraded_entries:
                result["status"] = "no_degraded_entries"
                logger.info("integrity_reconciler.no_degraded_entries")
                return result

            # Step 2: Get current Redis chain state
            current_seq, current_hash = self._get_redis_state()
            result["new_sequence_start"] = current_seq + 1

            # Step 3: Merge entries into main chain
            merged_count = self._merge_entries_to_chain(
                degraded_entries,
                start_sequence=current_seq + 1,
                previous_hash=current_hash,
            )
            result["entries_merged"] = merged_count
            result["new_sequence_end"] = current_seq + merged_count

            # Step 4: Log reconciliation event
            self._log_reconciliation_event(result)

            self._last_reconciliation = utc_now()

            logger.info(
                "reconciler.completed_merged_entries_seq",
                merged_count=merged_count,
                new_sequence_start=result["new_sequence_start"],
                new_sequence_end=result["new_sequence_end"],
            )

            return result

        except Exception as e:
            logger.exception(
                "reconciler.reconciliation_failed",
                error=e,
            )
            result["status"] = "error"
            result["error"] = str(e)
            return result

    def _collect_degraded_entries(self) -> list[dict[str, Any]]:
        """
        Collect all degraded entries from local log files.

        Returns:
            List of degraded entry dictionaries, sorted by timestamp
        """
        degraded = []

        if not self._log_dir.exists():
            return degraded

        log_files = sorted(self._log_dir.glob("audit_*.jsonl"))

        for log_file in log_files:
            try:
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            entry = json.loads(line)
                            integrity = entry.get("integrity", {})

                            # Collect degraded entries not yet reconciled
                            if integrity.get("degraded") is True and not integrity.get(
                                "reconciled"
                            ):
                                degraded.append(entry)

                        except json.JSONDecodeError:
                            continue

            except Exception as e:
                logger.warning(
                    "reconciler.error_reading",
                    log_file=log_file,
                    error=e,
                )
                continue

        # Sort by timestamp for proper ordering
        degraded.sort(key=lambda e: e.get("integrity", {}).get("timestamp", ""))

        return degraded

    def _get_redis_state(self) -> tuple[int, str]:
        """
        Get current sequence and hash from Redis.

        Returns:
            Tuple of (sequence, previous_hash)
        """
        seq_key = f"{self._key_prefix}{self.SEQUENCE_KEY}"
        state_key = f"{self._key_prefix}{self.STATE_KEY}"

        seq = self._redis.get(seq_key)
        seq = int(seq) if seq else 0

        prev_hash = self._redis.hget(state_key, "previous_hash")
        if isinstance(prev_hash, bytes):
            prev_hash = prev_hash.decode("utf-8")
        prev_hash = prev_hash or "GENESIS"

        return seq, prev_hash

    def _merge_entries_to_chain(
        self,
        entries: list[dict[str, Any]],
        start_sequence: int,
        previous_hash: str,
    ) -> int:
        """
        Merge entries into the main hash chain with new integrity info.

        Args:
            entries: Degraded entries to merge
            start_sequence: Starting sequence number for merged entries
            previous_hash: Previous hash to chain from

        Returns:
            Number of entries merged
        """
        if not entries:
            return 0

        seq_key = f"{self._key_prefix}{self.SEQUENCE_KEY}"
        state_key = f"{self._key_prefix}{self.STATE_KEY}"

        current_seq = start_sequence
        current_prev_hash = previous_hash
        merged_count = 0

        for entry in entries:
            # Update integrity with new chain info
            entry["integrity"]["sequence"] = current_seq
            entry["integrity"]["previous_hash"] = current_prev_hash
            entry["integrity"]["degraded"] = False  # No longer degraded
            entry["integrity"]["reconciled"] = True
            entry["integrity"]["reconciled_at"] = utc_now().isoformat()

            # Remove old hash before recomputing
            if "current_hash" in entry["integrity"]:
                del entry["integrity"]["current_hash"]

            # Compute new hash
            current_hash = compute_hash(entry)
            entry["integrity"]["current_hash"] = current_hash

            current_prev_hash = current_hash
            current_seq += 1
            merged_count += 1

        # Update Redis state
        if merged_count > 0:
            pipe = self._redis.pipeline()
            pipe.set(seq_key, current_seq - 1)
            pipe.hset(
                state_key,
                mapping={
                    "previous_hash": current_prev_hash,
                    "sequence": str(current_seq - 1),
                    "updated_at": utc_now().isoformat(),
                    "last_reconciliation": utc_now().isoformat(),
                    "reconciled_entries": str(merged_count),
                },
            )
            pipe.execute()

        return merged_count

    def _log_reconciliation_event(self, result: dict[str, Any]) -> None:
        """
        Log reconciliation event for audit trail.

        Args:
            result: Reconciliation result dictionary
        """
        try:
            # Try to use self_audit if available
            from baldur.audit.self_audit import SelfAuditEvent, self_audit

            self_audit().log(
                SelfAuditEvent.RECOVERY_COMPLETED,
                f"Hash chain reconciliation: merged {result['entries_merged']} entries",
                {
                    "action": "hash_chain_reconciliation",
                    "result": result,
                },
            )
        except (ImportError, AttributeError):
            # self_audit not available or event not found, just log
            logger.info(
                "reconciler.reconciliation_event",
                reconciliation_result=result,
            )

    def get_stats(self) -> dict[str, Any]:
        """
        Get reconciler statistics.

        Returns:
            Statistics dictionary
        """
        return {
            "last_reconciliation": (
                self._last_reconciliation.isoformat()
                if self._last_reconciliation
                else None
            ),
            "log_dir": str(self._log_dir),
        }


__all__ = ["HashChainReconciler"]
