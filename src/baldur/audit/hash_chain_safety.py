# verified-by: test_sequence_persists_across_restarts
"""Hash Chain Safety Components.

Provides zero-data-loss and integrity guarantees for distributed hash chain:

- MonotonicTimer: Clock-skew resistant TTL using time.monotonic()
- MonotonicTimestamp: Always-increasing timestamps for temporal ordering
- HashChainWAL: Write-Ahead Log for crash recovery
- AtomicMergeSwap: Global lock preventing concurrent reconciliation
- ShardedDateLock: Per-date locks for parallel date processing
- IntegrityAuditTrail: Forensic log of all integrity events
- HashChainSafetyManager: Unified access to all safety components
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from baldur.settings.hash_chain import get_hash_chain_settings
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def _get_merge_swap_timeout() -> int:
    """Get merge swap timeout from settings."""
    return get_hash_chain_settings().merge_swap_timeout_seconds


def _get_merge_swap_blocking_timeout() -> float:
    """Get merge swap blocking timeout from settings."""
    return get_hash_chain_settings().merge_swap_blocking_timeout_seconds


def _get_date_lock_timeout() -> int:
    """Get date lock timeout from settings."""
    return get_hash_chain_settings().date_lock_timeout_seconds


def _get_date_lock_blocking_timeout() -> float:
    """Get date lock blocking timeout from settings."""
    return get_hash_chain_settings().date_lock_blocking_timeout_seconds


def _get_integrity_trail_max_entries() -> int:
    """Get max Redis entries for integrity trail from settings."""
    return get_hash_chain_settings().integrity_trail_max_redis_entries


# =============================================================================
# Monotonic Timer for ClockSkew Protection
# =============================================================================


class MonotonicTimer:
    """
    Monotonic clock-based timer for ClockSkew protection.

    Problem:
        System time can be manipulated (NTP sync, manual change, VM snapshot).
        Using datetime.now() for TTL checks can lead to:
        - Premature expiration (clock jumps forward)
        - Never expiring (clock jumps backward)

    Solution:
        Use time.monotonic() which only moves forward and is immune
        to system time changes. This ensures consistent TTL behavior
        regardless of clock manipulation.

    Usage:
        timer = MonotonicTimer(ttl_seconds=30)
        timer.start()

        # Later...
        if timer.is_expired():
            handle_expiration()
    """

    def __init__(self, ttl_seconds: float):
        """
        Initialize monotonic timer.

        Args:
            ttl_seconds: Time-to-live in seconds
        """
        self.ttl_seconds = ttl_seconds
        self._start_time: float | None = None
        self._is_started = False

    def start(self) -> "MonotonicTimer":
        """Start the timer using monotonic clock."""
        self._start_time = time.monotonic()
        self._is_started = True
        return self

    def is_started(self) -> bool:
        """Check if timer has been started."""
        return self._is_started

    def elapsed_seconds(self) -> float:
        """Get elapsed time since start in seconds."""
        if not self._is_started or self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    def remaining_seconds(self) -> float:
        """Get remaining time before expiration."""
        return max(0.0, self.ttl_seconds - self.elapsed_seconds())

    def is_expired(self) -> bool:
        """Check if timer has expired."""
        if not self._is_started:
            return False
        return self.elapsed_seconds() >= self.ttl_seconds

    def reset(self) -> "MonotonicTimer":
        """Reset timer to start fresh."""
        self._start_time = time.monotonic()
        return self


class MonotonicTimestamp:
    """
    Generates monotonically increasing timestamps for hash chain entries.

    Problem:
        If system clock jumps backward, entries could have earlier timestamps
        than previous entries, breaking the assumption of temporal ordering.

    Solution:
        Track the last timestamp and ensure new timestamps are always
        greater than the previous one, even if system clock goes backward.

    Usage:
        ts = MonotonicTimestamp()

        entry["timestamp"] = ts.now()  # Always >= previous timestamp
    """

    def __init__(self):
        """Initialize monotonic timestamp generator."""
        self._last_timestamp: datetime | None = None
        self._monotonic_offset: float = 0.0
        self._lock = threading.Lock()

    def now(self) -> str:
        """
        Get current timestamp, guaranteed to be >= previous timestamp.

        Returns:
            ISO format timestamp string
        """
        with self._lock:
            current = utc_now()

            if self._last_timestamp is not None:
                if current <= self._last_timestamp:
                    # Clock went backward - adjust to maintain monotonicity
                    self._monotonic_offset += 0.001  # Add 1ms
                    current = self._last_timestamp + timedelta(
                        seconds=self._monotonic_offset
                    )
                    logger.warning(
                        "monotonic_timestamp.clock_skew_detected_adjusted",
                        monotonic_offset=self._monotonic_offset,
                    )
                else:
                    # Clock is normal - reset offset
                    self._monotonic_offset = 0.0

            self._last_timestamp = current
            return current.isoformat()

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about clock adjustments."""
        return {
            "total_offset_seconds": self._monotonic_offset,
            "last_timestamp": (
                self._last_timestamp.isoformat() if self._last_timestamp else None
            ),
        }


# =============================================================================
# Hash Chain WAL Integration
# =============================================================================


@dataclass
class HashChainSafetyWALEntry:
    """WAL entry for hash chain operations."""

    sequence: int
    operation: str  # "WRITE", "ANCHOR", "RECONCILE"
    entry_data: dict[str, Any]
    expected_hash: str
    timestamp: str
    status: str = "PENDING"  # "PENDING", "COMMITTED", "ABORTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.sequence,
            "op": self.operation,
            "data": self.entry_data,
            "hash": self.expected_hash,
            "ts": self.timestamp,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HashChainSafetyWALEntry":
        return cls(
            sequence=d["seq"],
            operation=d["op"],
            entry_data=d["data"],
            expected_hash=d["hash"],
            timestamp=d["ts"],
            status=d.get("status", "PENDING"),
        )


class HashChainWAL:  # verified-by: test_sequence_persists_across_restarts
    """
    Write-Ahead Log specifically for hash chain operations.

    Provides zero-data-loss guarantee by:
    1. Writing to WAL before Redis/file operations
    2. Recovering uncommitted entries on startup
    3. Replaying failed operations

    Integration with existing WAL:
        Reuses the WriteAheadLog class from audit/wal.py
        but with hash-chain specific entry format and recovery logic.
    """

    def __init__(
        self,
        wal_dir: Path,
        max_file_size_mb: int = 10,
        sync_on_write: bool = True,
    ):
        """
        Initialize hash chain WAL.

        Args:
            wal_dir: Directory for WAL files
            max_file_size_mb: Max size before rotation
            sync_on_write: Whether to fsync after each write
        """
        from baldur.audit.wal._jsonl import JSONLWriter

        self._wal_dir = Path(wal_dir)
        self._wal_dir.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self._timestamp_gen = MonotonicTimestamp()
        self._writer = JSONLWriter(
            file_path=self._get_wal_file(),
            fsync=sync_on_write,
            max_size_bytes=max_file_size_mb * 1024 * 1024,
        )

        # Load state
        self._load_sequence()

    def _get_wal_file(self) -> Path:
        """Get current WAL file path."""
        return self._wal_dir / "hash_chain_wal.jsonl"

    def _load_sequence(self) -> None:
        """Load last sequence from WAL file."""
        from baldur.audit.wal._jsonl import JSONLReader

        try:
            for entry in JSONLReader.iter_entries(self._get_wal_file()):
                seq = entry.get("seq", 0)
                if seq > self._sequence:
                    self._sequence = seq
        except Exception as e:
            logger.warning(
                "hash_chain_wal.load_sequence_failed",
                error=e,
            )

    def write_pending(
        self,
        operation: str,
        entry_data: dict[str, Any],
        expected_hash: str,
    ) -> int:
        """
        Write a PENDING entry to WAL.

        Args:
            operation: Operation type (WRITE, ANCHOR, RECONCILE)
            entry_data: The audit entry data
            expected_hash: Expected hash after operation

        Returns:
            WAL sequence number
        """
        with self._writer.lock:
            self._sequence += 1

            wal_entry = HashChainSafetyWALEntry(
                sequence=self._sequence,
                operation=operation,
                entry_data=entry_data,
                expected_hash=expected_hash,
                timestamp=self._timestamp_gen.now(),
                status="PENDING",
            )

            self._writer.append(wal_entry.to_dict())
            return self._sequence

    def mark_committed(self, sequence: int) -> bool:
        """
        Mark a WAL entry as committed.

        Instead of modifying the entry, appends a COMMITTED marker.
        Recovery will scan for uncommitted entries.
        """
        self._writer.append(
            {
                "_marker": "COMMIT",
                "seq": sequence,
                "wal_sequence": sequence,
                "status": "COMMITTED",
                "timestamp": self._timestamp_gen.now(),
            }
        )
        return True

    def mark_aborted(self, sequence: int, reason: str) -> bool:
        """Mark a WAL entry as aborted."""
        self._writer.append(
            {
                "seq": sequence,
                "status": "ABORTED",
                "reason": reason,
                "aborted_at": self._timestamp_gen.now(),
            }
        )
        return True

    def get_uncommitted_entries(self) -> list[HashChainSafetyWALEntry]:
        """
        Get all uncommitted WAL entries for recovery.

        Scans WAL file and returns entries that were not committed or aborted.

        Returns:
            List of uncommitted entries, oldest first
        """
        from baldur.audit.wal._jsonl import JSONLReader

        wal_file = self._get_wal_file()
        if not wal_file.exists():
            return []

        entries: dict[int, HashChainSafetyWALEntry] = {}
        committed_seqs: set[int] = set()
        aborted_seqs: set[int] = set()

        try:
            for data in JSONLReader.iter_entries(wal_file):
                seq = data.get("seq", 0)
                status = data.get("status", "")

                if status == "COMMITTED" or data.get("_marker") == "COMMIT":
                    committed_seqs.add(seq)
                elif status == "ABORTED":
                    aborted_seqs.add(seq)
                elif "op" in data:
                    entries[seq] = HashChainSafetyWALEntry.from_dict(data)

            uncommitted = []
            for seq, entry in sorted(entries.items()):
                if seq not in committed_seqs and seq not in aborted_seqs:
                    uncommitted.append(entry)

            return uncommitted

        except Exception as e:
            logger.exception(
                "hash_chain_wal.get_uncommitted_entries_failed",
                error=e,
            )
            return []

    def compact(self, keep_sequences_after: int = 0) -> int:
        """
        Compact WAL by removing old committed/aborted entries.

        Args:
            keep_sequences_after: Keep entries with seq > this value

        Returns:
            Number of entries removed
        """
        from baldur.audit.wal._cleanup import cleanup_by_sequence

        with self._writer.lock:
            self._writer.close()
            removed = cleanup_by_sequence(self._get_wal_file(), keep_sequences_after)
            if removed > 0:
                logger.info(
                    "hash_chain_wal.compacted_removed_entries",
                    removed_count=removed,
                )
            return removed

    def close(self) -> None:
        """Close WAL file."""
        self._writer.close()


# =============================================================================
# Atomic Merge Swap (Global Lock for Reconciliation)
# =============================================================================


class AtomicMergeSwap:
    """
    Provides atomic swap for hash chain reconciliation.

    Problem:
        Multiple pods may attempt reconciliation simultaneously.
        Without coordination, this causes:
        - Duplicate sequence numbers
        - Chain divergence
        - Data corruption

    Solution:
        Use a global Redis lock during the entire reconciliation process.
        Only one pod can reconcile at a time.

    Usage:
        with AtomicMergeSwap(redis, "myprefix:") as swap:
            if swap.acquired:
                do_reconciliation()
    """

    LOCK_KEY = "audit:hash_chain:reconcile:global_lock"

    # Legacy constant for backward compatibility
    DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        timeout_seconds: float | None = None,
        blocking_timeout: float | None = None,
    ):
        """
        Initialize atomic merge swap.

        Args:
            redis_client: Redis client instance
            key_prefix: Prefix for Redis keys
            timeout_seconds: Lock auto-expire time (default from HashChainSettings)
            blocking_timeout: Max time to wait for lock (default from HashChainSettings)
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else _get_merge_swap_timeout()
        )
        self._blocking_timeout = (
            blocking_timeout
            if blocking_timeout is not None
            else _get_merge_swap_blocking_timeout()
        )
        self._lock_token: str | None = None
        self.acquired = False

    def _get_lock_key(self) -> str:
        return f"{self._key_prefix}{self.LOCK_KEY}"

    def __enter__(self) -> "AtomicMergeSwap":
        """Acquire global lock."""
        import uuid

        lock_key = self._get_lock_key()
        self._lock_token = str(uuid.uuid4())

        try:
            # Try to acquire lock with blocking
            start_time = time.monotonic()
            while time.monotonic() - start_time < self._blocking_timeout:
                result = self._redis.set(
                    lock_key,
                    self._lock_token,
                    nx=True,
                    ex=int(self._timeout),
                )

                if result:
                    self.acquired = True
                    logger.info("atomic_merge_swap.global_lock_acquired")
                    return self

                time.sleep(0.1)

            logger.warning("atomic_merge_swap.acquire_global_lock_failed")
            self.acquired = False
            return self

        except Exception as e:
            logger.exception(
                "atomic_merge_swap.lock_acquisition_error",
                error=e,
            )
            self.acquired = False
            return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Release global lock."""
        if not self.acquired or not self._lock_token:
            return

        lock_key = self._get_lock_key()

        try:
            # Atomic check-and-delete using Lua script
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """

            result = self._redis.eval(lua_script, 1, lock_key, self._lock_token)

            if result:
                logger.info("atomic_merge_swap.global_lock_released")
            else:
                logger.warning("atomic_merge_swap.lock_already_released_stolen")

        except Exception as e:
            logger.exception(
                "atomic_merge_swap.lock_release_error",
                error=e,
            )

        self.acquired = False
        self._lock_token = None


# =============================================================================
# Sharded Date Locks (Distributed Lock Contention)
# =============================================================================


class ShardedDateLock:
    """
    Date-sharded distributed lock for parallel reconciliation.

    Problem:
        A single global lock prevents parallel processing of different dates.
        If reconciliation takes 5 minutes and there are 10 days of backlog,
        it takes 50 minutes sequentially.

    Solution:
        Use date-specific locks so different pods can reconcile
        different dates in parallel.

    Usage:
        with ShardedDateLock(redis, "2024-01-15") as lock:
            if lock.acquired:
                reconcile_date("2024-01-15")
    """

    LOCK_KEY_PREFIX = "audit:hash_chain:reconcile:date_lock:"

    # Legacy constant for backward compatibility
    DEFAULT_TIMEOUT_SECONDS = 120  # 2 minutes per date

    def __init__(
        self,
        redis_client: Any,
        date: str,
        key_prefix: str = "baldur:",
        timeout_seconds: float | None = None,
        blocking_timeout: float | None = None,
    ):
        """
        Initialize date-specific lock.

        Args:
            redis_client: Redis client instance
            date: Date string (YYYY-MM-DD)
            key_prefix: Prefix for Redis keys
            timeout_seconds: Lock auto-expire time (default from HashChainSettings)
            blocking_timeout: Max time to wait (default from HashChainSettings)
        """
        self._redis = redis_client
        self._date = date
        self._key_prefix = key_prefix
        self._timeout = (
            timeout_seconds if timeout_seconds is not None else _get_date_lock_timeout()
        )
        self._blocking_timeout = (
            blocking_timeout
            if blocking_timeout is not None
            else _get_date_lock_blocking_timeout()
        )
        self._lock_token: str | None = None
        self.acquired = False

    def _get_lock_key(self) -> str:
        return f"{self._key_prefix}{self.LOCK_KEY_PREFIX}{self._date}"

    def __enter__(self) -> "ShardedDateLock":
        """Try to acquire date-specific lock."""
        import uuid

        lock_key = self._get_lock_key()
        self._lock_token = str(uuid.uuid4())

        try:
            start_time = time.monotonic()
            while time.monotonic() - start_time < self._blocking_timeout:
                result = self._redis.set(
                    lock_key,
                    self._lock_token,
                    nx=True,
                    ex=int(self._timeout),
                )

                if result:
                    self.acquired = True
                    logger.debug(
                        "sharded_date_lock.lock_acquired",
                        date=self._date,
                    )
                    return self

                time.sleep(0.05)

            # Don't log warning - normal for another pod to have lock
            self.acquired = False
            return self

        except Exception as e:
            logger.exception(
                "sharded_date_lock.lock_error",
                date=self._date,
                error=e,
            )
            self.acquired = False
            return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Release date-specific lock."""
        if not self.acquired or not self._lock_token:
            return

        lock_key = self._get_lock_key()

        try:
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            self._redis.eval(lua_script, 1, lock_key, self._lock_token)
            logger.debug(
                "sharded_date_lock.lock_released",
                date=self._date,
            )

        except Exception as e:
            logger.exception(
                "sharded_date_lock.release_error",
                date=self._date,
                error=e,
            )

        self.acquired = False
        self._lock_token = None


# =============================================================================
# Integrity Audit Trail
# =============================================================================


class IntegrityEventType:
    """Types of integrity-related events."""

    CHAIN_VERIFIED = "chain_verified"
    CHAIN_BROKEN = "chain_broken"
    RECONCILIATION_STARTED = "reconciliation_started"
    RECONCILIATION_COMPLETED = "reconciliation_completed"
    RECONCILIATION_FAILED = "reconciliation_failed"
    ANCHOR_CREATED = "anchor_created"
    ANCHOR_RESTORED = "anchor_restored"
    PENDING_CLEANUP = "pending_cleanup"
    WAL_RECOVERY = "wal_recovery"
    CLOCK_SKEW_DETECTED = "clock_skew_detected"


class IntegrityAuditTrail:
    """
    Records all integrity-related events for forensic analysis.

    Stored separately from the main audit log to avoid circular dependency
    (integrity events about the audit log should not be in the log itself).

    Events are stored in:
    1. Redis list (for recent events)
    2. Dedicated file (for permanent record)
    """

    REDIS_KEY = "audit:hash_chain:integrity_trail"

    # Legacy constant for backward compatibility
    MAX_REDIS_ENTRIES = 1000

    def __init__(
        self,
        redis_client: Any | None = None,
        log_dir: Path | None = None,
        key_prefix: str = "baldur:",
        max_redis_entries: int | None = None,
    ):
        """
        Initialize integrity audit trail.

        Args:
            redis_client: Redis client (optional, for Redis storage)
            log_dir: Directory for file storage (optional)
            key_prefix: Prefix for Redis keys
            max_redis_entries: Max entries in Redis list (default from HashChainSettings)
        """
        self._redis = redis_client
        self._log_dir = Path(log_dir) if log_dir else None
        self._key_prefix = key_prefix
        self._max_redis_entries = (
            max_redis_entries
            if max_redis_entries is not None
            else _get_integrity_trail_max_entries()
        )
        self._lock = threading.Lock()

        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)

    def _get_redis_key(self) -> str:
        return f"{self._key_prefix}{self.REDIS_KEY}"

    def _get_log_file(self) -> Path:
        if not self._log_dir:
            raise ValueError("log_dir not configured")
        return self._log_dir / "integrity_trail.jsonl"

    def record(
        self,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
        severity: str = "INFO",
    ) -> dict[str, Any]:
        """
        Record an integrity event.

        Args:
            event_type: Type of event (from IntegrityEventType)
            message: Human-readable description
            details: Additional structured data
            severity: INFO, WARNING, ERROR, CRITICAL

        Returns:
            The recorded event
        """
        event = {
            "type": event_type,
            "message": message,
            "details": details or {},
            "severity": severity,
            "timestamp": utc_now().isoformat(),
            "hostname": os.environ.get(
                "HOSTNAME", os.environ.get("POD_NAME", "unknown")
            ),
        }

        with self._lock:
            # Write to Redis
            if self._redis:
                try:
                    redis_key = self._get_redis_key()
                    self._redis.lpush(redis_key, fast_dumps_str(event))
                    self._redis.ltrim(redis_key, 0, self._max_redis_entries - 1)
                except Exception as e:
                    logger.warning(
                        "integrity_trail.redis_write_failed",
                        error=e,
                    )

            # Write to file
            if self._log_dir:
                try:
                    log_file = self._get_log_file()
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(fast_dumps_str(event) + "\n")
                except Exception as e:
                    logger.warning(
                        "integrity_trail.file_write_failed",
                        error=e,
                    )

        # Also log to standard logger
        log_method = getattr(logger, severity.lower(), logger.info)
        log_method(f"[IntegrityTrail] {event_type}: {message}")

        return event

    def get_recent_events(self, count: int = 50) -> list[dict[str, Any]]:
        """Get recent events from Redis."""
        if not self._redis:
            return []

        try:
            redis_key = self._get_redis_key()
            entries = self._redis.lrange(redis_key, 0, count - 1)

            events = []
            for entry in entries:
                try:
                    data = entry.decode("utf-8") if isinstance(entry, bytes) else entry
                    events.append(fast_loads(data))
                except (ValueError, AttributeError):
                    continue

            return events

        except Exception as e:
            logger.exception(
                "integrity_trail.get_events_failed",
                error=e,
            )
            return []

    def get_events_by_type(
        self,
        event_type: str,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """Get events of a specific type."""
        all_events = self.get_recent_events(count * 2)  # Over-fetch to filter
        return [e for e in all_events if e.get("type") == event_type][:count]


# =============================================================================
# Integration Helper
# =============================================================================


class HashChainSafetyManager:
    """
    Unified manager for hash chain safety components.

    Provides centralized access to all safety features:
    - timestamp: MonotonicTimestamp for clock-skew-safe timestamps
    - wal: HashChainWAL for crash recovery
    - audit_trail: IntegrityAuditTrail for forensic logging
    - get_atomic_swap(): Global lock for reconciliation
    - get_date_lock(): Per-date lock for parallel processing
    """

    def __init__(
        self,
        redis_client: Any | None = None,
        log_dir: Path | None = None,
        key_prefix: str = "baldur:",
    ):
        """
        Initialize hash chain safety manager.

        Args:
            redis_client: Redis client for distributed coordination
            log_dir: Base directory for logs and WAL
            key_prefix: Prefix for Redis keys
        """
        self._redis = redis_client
        self._log_dir = Path(log_dir) if log_dir else Path("logs/audit")
        self._key_prefix = key_prefix

        # Initialize components
        self.timestamp = MonotonicTimestamp()

        self.wal = HashChainWAL(
            wal_dir=self._log_dir / "hash_chain_wal",
            sync_on_write=True,
        )

        self.audit_trail = IntegrityAuditTrail(
            redis_client=redis_client,
            log_dir=self._log_dir / "integrity",
            key_prefix=key_prefix,
        )

    def get_atomic_swap(self, timeout_seconds: float = 300) -> AtomicMergeSwap:
        """Get atomic merge swap context manager."""
        if not self._redis:
            raise ValueError("Redis client required for atomic swap")
        return AtomicMergeSwap(
            redis_client=self._redis,
            key_prefix=self._key_prefix,
            timeout_seconds=timeout_seconds,
        )

    def get_date_lock(self, date: str, timeout_seconds: float = 120) -> ShardedDateLock:
        """Get date-specific lock context manager."""
        if not self._redis:
            raise ValueError("Redis client required for date lock")
        return ShardedDateLock(
            redis_client=self._redis,
            date=date,
            key_prefix=self._key_prefix,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        """Clean up resources."""
        self.wal.close()
