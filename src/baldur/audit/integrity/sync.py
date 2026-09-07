"""
Startup Hash Chain Synchronization.

Contains:
- StartupHashChainSync: Synchronizes hash chain state between Redis and local files at startup
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.integrity.ledger_tail import LedgerTailReader
from baldur.audit.integrity.redis_manager import (
    CHAIN_SEQUENCE_KEY,
    CHAIN_STATE_KEY,
    build_chain_lock,
    write_chain_state,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()

# ``SCAN`` batch size for the boot-time PENDING sweep. redis-py's default
# COUNT of 10 turns one command into a round trip per ten keys examined, and
# this sweep runs inside ``init()`` under the init lock — on a shared Redis
# holding millions of keys that is boot time measured in round trips. Matches
# the batch the event journal's own scan already uses.
PENDING_SCAN_BATCH = 200

# Upper bound on PENDING keys *processed* by one boot sweep. The cleanup is
# best-effort by contract — PENDING keys carry their own short TTL and expire
# unaided — so a capped sweep loses nothing a crash-recovery path depends on.
PENDING_SCAN_MAX_KEYS = 10_000

# Wall-clock ceiling on the same sweep, and the bound that actually protects
# boot time. ``SCAN`` with a ``MATCH`` filters server-side and returns only
# matches, so a key-count cap cannot bound the walk: a Redis shared with an
# application cache can hold millions of keys and zero live PENDING ones, and
# the loop body — where the count is taken — never runs. This sweep executes
# inside ``init()`` under the init lock, so an unbounded walk is start-up time
# in every worker. Only elapsed time bounds it.
PENDING_SCAN_MAX_SECONDS = 2.0


class StartupHashChainSync:
    """
    Synchronizes hash chain state between Redis and local files at startup.

    Why Needed:
        After a restart, Redis and local file state may diverge:
        - Redis restarted and lost data -> File is ahead
        - Process crashed mid-write -> Redis is ahead (pending writes)
        - Both empty -> Fresh start

    Sync Logic:
        1. Read last sequence/hash from local log files
        2. Read current sequence/hash from Redis
        3. Compare and sync:
           - Redis < File: Update Redis to match file (data recovery)
           - Redis > File: Normal (some writes pending, no action needed)
           - Equal: In sync, no action needed
        4. Clean up stale PENDING sequences from previous crashes

    Idempotent:
        sync() is safe to call multiple times. After first sync,
        subsequent calls return immediately without re-syncing.
    """

    SEQUENCE_KEY = CHAIN_SEQUENCE_KEY
    STATE_KEY = CHAIN_STATE_KEY

    def __init__(
        self,
        redis_client: Any,
        log_dir: Path,
        key_prefix: str = "baldur:",
        pending_key_prefix: str | None = None,
        filename_pattern: str = "audit_{date}.jsonl",
        rotate_daily: bool = True,
        ledger: LedgerTailReader | None = None,
    ):
        """
        Initialize StartupHashChainSync.

        The chain's sequence/state keys and its PENDING/ORPHANED keys hang off
        two different roots in production: the chain manager is built with a
        partition-namespaced prefix while ``PendingSequenceManager`` receives
        the bare one. One prefix for both would reconcile a key nothing writes.

        Args:
            redis_client: Redis client instance
            log_dir: Directory containing audit log files
            key_prefix: Prefix for the chain's sequence and state keys — the
                same value the chain manager writes under
            pending_key_prefix: Prefix for the PENDING/ORPHANED keys. Defaults
                to ``key_prefix``, which is correct whenever one prefix governs
                both (every test that injects its own fake Redis).
            filename_pattern: The writing adapter's filename pattern, so this
                step reads exactly the files that adapter produces rather than
                a glob that also matches a partitioned sibling's.
            rotate_daily: The writing adapter's rotation mode. The pattern
                string alone cannot say whether ``{date}`` becomes a date or
                the literal ``all``.
            ledger: A ready-built reader, which :meth:`from_manager` supplies
                straight from the adapter. When ``None`` one is built from
                ``log_dir`` and the two arguments above.
        """
        self._redis = redis_client
        self._log_dir = Path(log_dir)
        self._key_prefix = key_prefix
        self._pending_key_prefix = (
            key_prefix if pending_key_prefix is None else pending_key_prefix
        )
        self._ledger = ledger or LedgerTailReader(
            self._log_dir, filename_pattern, rotate_daily
        )
        self._sync_completed = False

    @classmethod
    def from_manager(
        cls,
        manager: Any,
        ledger: LedgerTailReader,
        pending_key_prefix: str,
    ) -> StartupHashChainSync:
        """Build a sync that reconciles exactly the keys ``manager`` writes.

        Reads the client and the chain prefix off the manager itself rather
        than re-deriving them from settings — a second derivation of one key
        form is how the reconciliation drifted away from the writer in the
        first place. The ledger reader comes from the adapter for the same
        reason: it selects the exact files that adapter writes, which a glob
        over ``audit_*.jsonl`` does not.

        ``pending_key_prefix`` has no default on purpose. The manager cannot
        supply it (it stores only its own composed prefix), and inheriting
        ``__init__``'s fallback here would let a caller silently reconcile the
        wrong namespace. It comes from the adapter that constructed the
        ``PendingSequenceManager``.

        Args:
            manager: A ``RedisHashChainManager``.
            ledger: The writing adapter's own ledger tail reader.
            pending_key_prefix: The bare Redis root the adapter's
                ``PendingSequenceManager`` was built with.

        Returns:
            A sync pinned to that manager's client and key namespaces.
        """
        return cls(
            redis_client=manager._redis,
            log_dir=ledger.log_dir,
            key_prefix=manager._key_prefix,
            pending_key_prefix=pending_key_prefix,
            ledger=ledger,
        )

    def sync(self) -> dict[str, Any]:
        """
        Perform startup synchronization.

        The chain reconciliation and the PENDING sweep are reported together
        but fail apart: a ledger this step cannot read must not cost the
        crash-recovery sweep, which needs nothing from the files.

        Returns:
            Sync result dictionary with action taken and state info
        """
        if self._sync_completed:
            return {"status": "already_synced", "action": "none"}

        result: dict[str, Any] = {
            "status": "success",
            "file_sequence": 0,
            "file_hash": None,
            "redis_sequence": 0,
            "redis_hash": None,
            "action": "none",
            "pending_cleaned": 0,
            "synced_at": utc_now().isoformat(),
        }

        try:
            self._reconcile_chain_state(result)
        except Exception as e:
            # An unreadable ledger reports the error rather than re-anchoring
            # Redis to 0 — a rewind to 0 re-uses every number the files hold.
            logger.exception(
                "startup_sync.failed",
                error=e,
            )
            result["status"] = "error"
            result["error"] = str(e)

        # Step 4: Cleanup stale PENDING sequences — runs whatever step 1-3 did.
        result["pending_cleaned"] = self._cleanup_pending_sequences()

        if result["status"] == "success":
            self._sync_completed = True
            logger.info(
                "startup_sync.completed",
                sync_action=result["action"],
            )

        return result

    def _reconcile_chain_state(self, result: dict[str, Any]) -> None:
        """Compare the ledger's tail with Redis and rewind Redis when behind.

        Runs under the chain's own distributed lock. Unlocked, a peer's
        in-flight mint plus this rewind is a duplicate factory: the peer
        repairs and mints ``T+1`` inside its lock, and before its append lands
        this rewind rolls the counter back to ``T``, so the next write mints
        ``T+1`` a second time and no guard can see it.

        Args:
            result: The sync result dict, filled in place.
        """
        lock = build_chain_lock(self._redis, self._key_prefix)
        if not lock.acquire(blocking=True):
            # The write-time guard repairs the source on the first write, so a
            # missed boot rewind is latency, not safety.
            result["action"] = "lock_unavailable"
            result["status"] = "error"
            logger.warning(
                "startup_sync.chain_lock_unavailable",
                key_prefix=self._key_prefix,
            )
            return

        try:
            # Step 1: Get last state from local files
            file_seq, file_hash = self._get_last_file_state()
            result["file_sequence"] = file_seq
            result["file_hash"] = file_hash[:16] + "..." if file_hash else None

            # Step 2: Get current state from Redis
            redis_seq, redis_hash = self._get_redis_state()
            result["redis_sequence"] = redis_seq
            result["redis_hash"] = redis_hash[:16] + "..." if redis_hash else None

            # Step 3: Compare and sync
            if file_seq == 0 and redis_seq == 0:
                # Both empty - fresh start
                result["action"] = "fresh_start"

            elif redis_seq < file_seq:
                # Redis behind file - sync Redis to file state
                self._sync_redis_to_file(file_seq, file_hash)
                result["action"] = "synced_redis_to_file"
                logger.warning(
                    "startup_sync.redis_sequence_behind_file",
                    redis_seq=redis_seq,
                    file_seq=file_seq,
                )

            elif redis_seq > file_seq:
                # Redis ahead - normal, some writes didn't complete
                result["action"] = "redis_ahead_ok"
                logger.info(
                    "startup_sync.redis_sequence_ahead_file",
                    redis_seq=redis_seq,
                    file_seq=file_seq,
                )

            else:
                # Sequences match
                result["action"] = "in_sync"
        finally:
            try:
                lock.release()
            except Exception as e:
                logger.debug(
                    "startup_sync.chain_lock_release_failed",
                    error=e,
                )

    def _get_last_file_state(self) -> tuple[int, str]:
        """
        Get the last sequence and hash from local log files.

        Returns:
            Tuple of (last_sequence, last_hash). ``(0, "")`` means there is no
            ledger at all — never that one could not be read, which the reader
            raises for and the caller reports as an error.
        """
        tail = self._ledger.read()
        if tail is None:
            return 0, ""
        return tail.sequence, tail.current_hash

    def _get_redis_state(self) -> tuple[int, str]:
        """
        Get current sequence and hash from Redis.

        Returns:
            Tuple of (sequence, hash)
        """
        try:
            seq_key = f"{self._key_prefix}{self.SEQUENCE_KEY}"
            state_key = f"{self._key_prefix}{self.STATE_KEY}"

            # Get sequence
            seq = self._redis.get(seq_key)
            seq = int(seq) if seq else 0

            # Get previous hash
            prev_hash = self._redis.hget(state_key, "previous_hash")
            if isinstance(prev_hash, bytes):
                prev_hash = prev_hash.decode("utf-8")
            prev_hash = prev_hash or ""

            return seq, prev_hash

        except Exception as e:
            logger.exception(
                "startup_sync.get_redis_state_failed",
                error=e,
            )
            return 0, ""

    def _sync_redis_to_file(self, file_seq: int, file_hash: str) -> None:
        """
        Update Redis state to match file state.

        Used when Redis is behind (e.g., after Redis restart).

        Args:
            file_seq: Sequence from file
            file_hash: Hash from file
        """
        try:
            write_chain_state(
                self._redis,
                self._key_prefix,
                file_seq,
                file_hash,
                synced_from="file_recovery",
            )

            logger.info(
                "startup_sync.redis_synced_file",
                file_seq=file_seq,
            )

        except Exception as e:
            logger.exception(
                "startup_sync.sync_redis_file_failed",
                error=e,
            )
            raise

    def _orphan_pending_key(self, key: Any, orphan_ttl: int) -> bool:
        """Move one PENDING reservation to ORPHANED.

        The ORPHANED key follows the PENDING prefix, not the chain prefix:
        orphans are the pending namespace's other half, and the only reader of
        them scans the bare form. An orphan written under the chain prefix is a
        record nothing will ever find.

        Args:
            key: The PENDING key, as the client returned it.
            orphan_ttl: Retention for the ORPHANED marker, in seconds.

        Returns:
            ``True`` when the key was moved. A key whose trailing segment is
            not an integer is skipped rather than raised on — one hand-written
            key must not cost the rest of the crash recovery.
        """
        try:
            key_str = key.decode("utf-8") if isinstance(key, bytes) else key
            seq = int(key_str.split(":")[-1])
        except (ValueError, IndexError):
            return False

        orphan_key = f"{self._pending_key_prefix}audit:hash_chain:orphaned:{seq}"
        expected_hash = self._redis.get(key)

        pipe = self._redis.pipeline()
        pipe.delete(key)
        pipe.set(orphan_key, expected_hash or "startup_cleanup", ex=orphan_ttl)
        pipe.execute()
        return True

    def _sweep_pending_keys(self, orphan_ttl: int) -> tuple[int, int, str]:
        """Walk the PENDING namespace once, within both of its bounds.

        Drives the cursor rather than using ``scan_iter``. The iterator helper
        yields only *matching* keys, so with a MATCH filter it returns control
        once per match — and this pattern normally matches nothing, since
        PENDING keys carry a short TTL and expire on their own. A budget
        checked inside that loop therefore never executes on the one keyspace
        it exists to protect against: a Redis shared with an application cache,
        holding millions of keys and no live reservations. This sweep runs
        inside ``init()`` under the init lock, so an unbounded walk is start-up
        time in every worker. One explicit ``SCAN`` per round trip is what lets
        the deadline bind.

        Args:
            orphan_ttl: Retention for each ORPHANED marker, in seconds.

        Returns:
            ``(cleaned, examined, capped)``. ``capped`` names the bound that
            stopped the walk — ``"max_keys"``, ``"max_seconds"`` — or is empty
            when the walk finished on its own.
        """
        cleaned = 0
        examined = 0
        capped = ""
        deadline = time.monotonic() + PENDING_SCAN_MAX_SECONDS
        pending_pattern = f"{self._pending_key_prefix}audit:hash_chain:pending:*"

        cursor: Any = 0
        while True:
            cursor, batch = self._redis.scan(
                cursor=cursor,
                match=pending_pattern,
                count=PENDING_SCAN_BATCH,
            )

            for key in batch:
                if examined >= PENDING_SCAN_MAX_KEYS:
                    capped = "max_keys"
                    break
                examined += 1
                if self._orphan_pending_key(key, orphan_ttl):
                    cleaned += 1

            if capped:
                break
            if time.monotonic() > deadline:
                capped = "max_seconds"
                break
            # Cursor 0 (or "0" from a client that echoes strings) ends the
            # walk; anything else is another round trip.
            if not cursor or cursor == "0":
                break

        return cleaned, examined, capped

    def _cleanup_pending_sequences(self) -> int:
        """
        Clean up stale PENDING sequences from previous crashes.

        Moves all PENDING to ORPHANED for reconciliation.

        Returns:
            Number of sequences cleaned up
        """
        try:
            from baldur.settings.audit_integrity import (
                get_audit_integrity_settings,
            )

            orphan_ttl = get_audit_integrity_settings().orphan_ttl_seconds
            cleaned, examined, capped = self._sweep_pending_keys(orphan_ttl)

            if capped:
                logger.warning(
                    "startup_sync.pending_scan_capped",
                    limit=capped,
                    examined=examined,
                    cleaned=cleaned,
                )

            if cleaned:
                logger.info(
                    "startup_sync.cleaned_up_pending_sequences",
                    cleaned=cleaned,
                )

            return cleaned

        except Exception as e:
            logger.exception(
                "startup_sync.cleanup_pending_failed",
                error=e,
            )
            return 0


__all__ = [
    "StartupHashChainSync",
    "PENDING_SCAN_BATCH",
    "PENDING_SCAN_MAX_KEYS",
    "PENDING_SCAN_MAX_SECONDS",
]
