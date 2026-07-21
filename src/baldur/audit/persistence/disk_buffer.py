"""LMDB-based Disk-Persistent Buffer.

Pod-restart-safe persistent buffer that replaces InMemoryAuditBuffer,
eliminating data volatility.

Key features:
- LMDB-based high-performance persistence
- CRC32 checksum integrity verification
- Group Commit I/O optimisation
- Fail-Open mode on disk full
- Dead Letter DB for poison-pill isolation
- Graceful Shutdown support
"""

from __future__ import annotations

import os
import shutil
import struct
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.persistence.checksum import compute_checksum
from baldur.audit.persistence.config import (
    DiskBufferSettings,
    get_disk_buffer_settings,
)
from baldur.audit.persistence.dead_letter_store import DeadLetterStore
from baldur.audit.persistence.disk_buffer_models import (
    BufferEntry,
    BufferState,
    DiskBufferError,
)
from baldur.audit.persistence.disk_buffer_shutdown import (
    _register_signal_handlers,  # noqa: F401 — backward compat re-export
    register_disk_buffer_shutdown,
)
from baldur.audit.persistence.disk_space_monitor import DiskSpaceMonitor
from baldur.audit.persistence.group_commit import GroupCommitWriter
from baldur.utils.fs import resolve_writable_dir
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# Lazy import to break circular dependency (disk_buffer_adapter -> disk_buffer)
def __getattr__(name: str) -> type:  # noqa: N807
    if name == "DiskBufferAdapter":
        from baldur.audit.persistence.disk_buffer_adapter import (
            DiskBufferAdapter,
        )

        return DiskBufferAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BufferEntry",
    "BufferState",
    "DiskBufferAdapter",
    "DiskBufferError",
    "DiskPersistentBuffer",
    "_register_signal_handlers",
    "get_disk_buffer",
    "reset_disk_buffer",
]


class DiskPersistentBuffer:
    """LMDB-based Disk-Persistent Buffer.

    Features:
    - Pod-restart-safe data persistence
    - ACID transactions
    - Ultra-fast reads/writes
    - CRC32 checksum integrity verification
    - Automatic cleanup (retention-based)

    Legacy InMemoryAuditBuffer-compatible methods:
    - add(entry) -> put(entry)
    - try_flush(callback) -> flush_to(callback)
    - get_all() -> iter_entries()

    Usage::

        buffer = DiskPersistentBuffer()

        # Store
        buffer.put({"event": "dlq_store", "domain": "payment"})

        # Read
        for entry in buffer.iter_entries():
            print(entry.data)

        # Flush
        buffer.flush_to(lambda entries: send_to_kafka(entries))
    """

    # Writable-directory resolution identity
    DIR_PURPOSE = "disk_buffer"
    DIR_ENV_VAR = "BALDUR_DISK_BUFFER_DATA_DIR"

    # Database names
    DB_ENTRIES = b"entries"
    DB_META = b"meta"
    DB_DEAD_LETTER = b"dead_letter"

    def __init__(
        self,
        settings: DiskBufferSettings | None = None,
        db_name: str | None = None,
    ):
        """Initialise DiskPersistentBuffer.

        Args:
            settings: Buffer settings (loaded from env vars when ``None``).
            db_name: Database name (auto-generated when ``None``).
        """
        self._settings = settings or get_disk_buffer_settings()
        self._db_name = db_name or self._generate_db_name()
        self._lock = threading.RLock()

        # State
        self._state = BufferState.UNINITIALIZED

        # Data directory actually used (re-assigned by _init_storage once
        # resolved; a fallback repoints the LMDB dir and the disk monitor
        # together).
        self._resolved_data_path: Path = self._settings.data_path

        # LMDB handles
        self._env: Any = None
        self._entries_db: Any = None
        self._meta_db: Any = None
        self._dead_letter_db: Any = None

        # Sequence number
        self._sequence = 0

        # Statistics
        self._stats: dict[str, int] = {
            "total_puts": 0,
            "total_gets": 0,
            "total_deletes": 0,
            "checksum_errors": 0,
            "group_commit_flushes": 0,
            "dead_letter_moves": 0,
            "disk_full_events": 0,
            "quarantine_events": 0,
        }

        # Sub-components (initialised after storage is ready)
        self._disk_monitor: DiskSpaceMonitor | None = None
        self._group_writer: GroupCommitWriter | None = None
        self._dead_letters: DeadLetterStore | None = None

        self._init_storage()

        # Graceful Shutdown registration
        if self._settings.enable_shutdown_handlers:
            register_disk_buffer_shutdown(self)

    def _generate_db_name(self) -> str:
        """Auto-generate DB name (multi-instance safe).

        Includes hostname and PID to avoid collisions in multi-pod /
        multi-process environments.
        """
        parts = ["audit_buffer"]

        if self._settings.include_hostname_in_db_name:
            hostname = os.environ.get("HOSTNAME", os.environ.get("POD_NAME", "unknown"))
            parts.append(hostname)

        if self._settings.include_pid_in_db_name:
            parts.append(str(os.getpid()))

        return "_".join(parts)

    def _init_storage(self) -> None:
        """Initialise LMDB storage with quarantine support."""
        try:
            import lmdb
        except ImportError as e:
            raise DiskBufferError(
                "lmdb not installed. Install with: pip install lmdb"
            ) from e

        # Resolve the data directory once. Both readers - the LMDB
        # directory here and the free-space monitor below - must follow a
        # fallback together, or the monitor would decide fail-open and
        # priority purge against a filesystem the buffer no longer uses.
        self._resolved_data_path = resolve_writable_dir(
            self._settings.data_path,
            purpose=self.DIR_PURPOSE,
            operator_set="data_dir" in self._settings.model_fields_set,
            env_override_name=self.DIR_ENV_VAR,
        ).path

        db_path = self._resolved_data_path / self._db_name
        db_path.mkdir(parents=True, exist_ok=True)

        try:
            self._env = lmdb.open(
                str(db_path),
                **self._settings.get_lmdb_open_kwargs(),
            )
        except Exception as e:
            # Quarantine on corruption
            if self._settings.quarantine_on_corruption:
                if self._quarantine_corrupt_db(db_path):
                    # Retry with a fresh DB
                    self._env = lmdb.open(
                        str(db_path),
                        **self._settings.get_lmdb_open_kwargs(),
                    )
                else:
                    raise DiskBufferError(
                        f"Failed to recover from corruption: {e}"
                    ) from e
            else:
                raise

        # Open databases
        self._entries_db = self._env.open_db(self.DB_ENTRIES, create=True)
        self._meta_db = self._env.open_db(self.DB_META, create=True)

        if self._settings.enable_dead_letter_db:
            self._dead_letter_db = self._env.open_db(self.DB_DEAD_LETTER, create=True)

        self._recover_sequence()
        self._state = BufferState.ACTIVE

        # Initialise sub-components
        self._disk_monitor = DiskSpaceMonitor(
            path=self._resolved_data_path,
            settings=self._settings,
        )
        self._group_writer = GroupCommitWriter(
            env=self._env,
            entries_db=self._entries_db,
            meta_db=self._meta_db,
            settings=self._settings,
            stats=self._stats,
            get_sequence=lambda: self._sequence,
            set_sequence=self._set_sequence,
        )
        self._dead_letters = DeadLetterStore(
            env=self._env,
            dead_letter_db=self._dead_letter_db,
            settings=self._settings,
            stats=self._stats,
        )

        logger.info(
            "disk_buffer.initialized",
            db_path=db_path,
            sequence=self._sequence,
            db_name=self._db_name,
        )

    def _set_sequence(self, value: int) -> None:
        """Setter for sequence (used by GroupCommitWriter)."""
        self._sequence = value

    def _quarantine_corrupt_db(self, db_path: Path) -> bool:
        """Quarantine a corrupted DB and prepare for a fresh one.

        Moves the corrupted DB files to a separate directory so that
        a new DB can be created in the original path.

        Args:
            db_path: Path to the corrupted database.

        Returns:
            ``True`` on successful quarantine, ``False`` on failure.
        """
        try:
            timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
            corrupt_path = (
                db_path.parent
                / f"{db_path.name}{self._settings.quarantine_suffix}.{timestamp}"
            )

            shutil.move(str(db_path), str(corrupt_path))
            self._stats["quarantine_events"] += 1

            logger.critical(
                "disk_buffer.quarantined_corrupt_db",
                db_path=db_path,
                corrupt_path=corrupt_path,
            )

            # Attempt to send alert
            self._send_corruption_alert(db_path, corrupt_path)

            return True
        except Exception as e:
            logger.exception(
                "disk_buffer.quarantine_failed",
                error=e,
            )
            return False

    def _send_corruption_alert(self, original: Path, quarantined: Path) -> None:
        """Send corruption alert notification."""
        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )

            payload = NotificationPayload(
                title="\U0001f6a8 DiskBuffer DB Corruption Detected",
                message=(
                    f"DB corruption detected and quarantined: "
                    f"{original} -> {quarantined}"
                ),
                priority=NotificationPriority.CRITICAL,
                category=NotificationCategory.OPERATIONS,
                source="DiskPersistentBuffer",
                dedup_key=f"disk_buffer:corruption:{original}",
            )
            UnifiedNotificationManager().notify(payload)
        except Exception as e:
            logger.debug(
                "disk_buffer.alert_send_failed",
                error=e,
            )

    def _recover_sequence(self) -> None:
        """Recover the last sequence number from meta DB."""
        with self._env.begin(db=self._meta_db) as txn:
            value = txn.get(b"last_sequence")
            if value:
                self._sequence = struct.unpack(">Q", value)[0]

    def _save_sequence(self, txn: Any) -> None:
        """Persist current sequence number."""
        txn.put(
            b"last_sequence",
            struct.pack(">Q", self._sequence),
            db=self._meta_db,
        )

    def _generate_key(self) -> bytes:
        """Generate a unique key."""
        self._sequence += 1
        timestamp = time.time()
        return f"{timestamp:.6f}:{self._sequence:08d}".encode()

    # ---------------------------------------------------------
    # Public API: Write
    # ---------------------------------------------------------

    def put(self, entry: dict[str, Any]) -> bytes | None:
        """Store an entry.

        Switches to Fail-Open mode on disk full to keep the service
        running.  When Group Commit is enabled, entries are batched
        for I/O optimisation.

        Args:
            entry: Event data.

        Returns:
            The stored key, or ``None`` in Fail-Open mode.
        """
        with self._lock:
            # Fail-Open mode check
            if self._state == BufferState.DISK_FULL_FAILOPEN:
                logger.warning("disk_buffer.disk_full_fail_open")
                return None

            if self._state == BufferState.CLOSED:
                logger.warning("disk_buffer.buffer_closed_skipping_write")
                return None

            # Disk space check + priority purge
            if not self._check_disk_space():
                if self._settings.fail_open_on_disk_full:
                    return None
                raise DiskBufferError("Disk full and fail_open disabled")

            # Group Commit mode
            if self._settings.group_commit_enabled:
                if self._group_writer is None:
                    raise DiskBufferError("GroupCommitWriter not initialized")
                return self._group_writer.put(entry)

            return self._direct_put(entry)

    def _check_disk_space(self) -> bool:
        """Check disk space and perform priority purge if needed.

        Returns:
            ``True`` when space is sufficient or reclaimed; ``False``
            when disk is full.
        """
        if self._disk_monitor is None:
            raise DiskBufferError("DiskSpaceMonitor not initialized")

        ok, free_ratio = self._disk_monitor.check()

        if not ok:
            # Attempt priority-based purge
            if self._settings.priority_based_purge:
                purged = self._disk_monitor.execute_priority_purge(
                    count_fn=self.count,
                    iter_fn=self.iter_entries,
                    delete_batch_fn=self.delete_batch,
                )
                if purged > 0:
                    logger.warning(
                        "disk_buffer.purged_entries_disk_space",
                        purged=purged,
                    )
                    return True

            # Purge failed — switch to fail-open
            self._state = BufferState.DISK_FULL_FAILOPEN
            self._stats["disk_full_events"] += 1
            logger.critical("disk_buffer.disk_full_switching_fail")
            self._disk_monitor.send_disk_full_alert()
            return False

        # Recovery check
        if (
            self._state == BufferState.DISK_FULL_FAILOPEN
            and self._disk_monitor.should_recover(free_ratio)
        ):
            self._state = BufferState.ACTIVE
            logger.info("disk_buffer.disk_space_recovered_resuming")

        return True

    def _direct_put(self, entry: dict[str, Any]) -> bytes:
        """Direct (non-batched) put."""
        key = self._generate_key()

        entry_with_meta = {
            **entry,
            "_stored_at": utc_now().isoformat(),
            "_buffer_key": key.decode(),
        }

        data = fast_dumps_str(entry_with_meta, default=str)
        data_bytes = data.encode("utf-8")
        checksum = compute_checksum(data_bytes)
        value = struct.pack(">I", checksum) + data_bytes

        with self._env.begin(write=True, db=self._entries_db) as txn:
            txn.put(key, value)
            self._save_sequence(txn)

        self._stats["total_puts"] += 1
        logger.debug(
            "disk_buffer.put",
            buffer_entry_key=key.decode(),
        )
        return key

    def flush_group_commit(self) -> None:
        """Force-flush the group commit buffer (for graceful shutdown).

        No-op when the buffer is CLOSED — symmetric with ``put()``'s
        CLOSED guard, so post-close callers never touch a closed env.
        """
        with self._lock:
            if self._state == BufferState.CLOSED:
                return
            if self._settings.group_commit_enabled and self._group_writer is not None:
                self._group_writer.flush_all()

    # ---------------------------------------------------------
    # Public API: Read
    # ---------------------------------------------------------

    def get(self, key: bytes) -> BufferEntry | None:
        """Retrieve an entry by key.

        Args:
            key: Entry key.

        Returns:
            :class:`BufferEntry` or ``None``.
        """
        with self._env.begin(db=self._entries_db) as txn:
            value = txn.get(key)
            if not value:
                return None

            self._stats["total_gets"] += 1
            return self._parse_entry(key, value)

    def _parse_entry(self, key: bytes, value: bytes) -> BufferEntry | None:
        """Parse a raw LMDB value into a BufferEntry."""
        try:
            # Extract checksum
            stored_checksum = struct.unpack(">I", value[:4])[0]
            data_bytes = value[4:]

            # Verify checksum
            if self._settings.enable_checksum:
                computed_checksum = compute_checksum(data_bytes)
                if stored_checksum != computed_checksum:
                    logger.warning(
                        "disk_buffer.checksum_mismatch",
                        buffer_entry_key=key.decode(),
                    )
                    self._stats["checksum_errors"] += 1
                    return None

            # Parse JSON
            data = fast_loads(data_bytes)

            # Extract timestamp
            timestamp = float(key.decode().split(":")[0])

            return BufferEntry(
                key=key,
                data=data,
                timestamp=timestamp,
                checksum=stored_checksum,
            )
        except Exception as e:
            logger.exception(
                "disk_buffer.parse_error",
                error=e,
            )
            return None

    def iter_entries(
        self,
        limit: int | None = None,
        reverse: bool = False,
    ) -> Iterator[BufferEntry]:
        """Iterate over entries.

        Args:
            limit: Maximum number of entries to yield.
            reverse: Iterate in reverse order.

        Yields:
            :class:`BufferEntry` instances.
        """
        count = 0
        with self._env.begin(db=self._entries_db) as txn:
            cursor = txn.cursor()

            if reverse:
                cursor.last()
                iterator = cursor.iterprev()
            else:
                cursor.first()
                iterator = cursor.iternext()

            for key, value in iterator:
                if limit and count >= limit:
                    break

                entry = self._parse_entry(key, value)
                if entry:
                    count += 1
                    yield entry

    def count(self) -> int:
        """Return the number of entries."""
        if self._env is None:
            return 0
        with self._env.begin(db=self._entries_db) as txn:
            return txn.stat()["entries"]

    # ---------------------------------------------------------
    # Public API: Delete
    # ---------------------------------------------------------

    def delete(self, key: bytes) -> bool:
        """Delete an entry.

        Args:
            key: Entry key.

        Returns:
            ``True`` if the entry was deleted.
        """
        with self._env.begin(write=True, db=self._entries_db) as txn:
            result = txn.delete(key)
            if result:
                self._stats["total_deletes"] += 1
            return result

    def delete_batch(self, keys: list[bytes]) -> int:
        """Batch delete entries.

        Args:
            keys: List of keys to delete.

        Returns:
            Number of entries deleted.
        """
        deleted = 0
        with self._env.begin(write=True, db=self._entries_db) as txn:
            for key in keys:
                if txn.delete(key):
                    deleted += 1

        self._stats["total_deletes"] += deleted
        return deleted

    # ---------------------------------------------------------
    # Public API: Flush
    # ---------------------------------------------------------

    def flush_to(
        self,
        handler: Callable[[list[BufferEntry]], bool],
        batch_size: int | None = None,
    ) -> int:
        """Flush entries via handler, then delete on success.

        Args:
            handler: Batch handler (returns ``True`` on success).
            batch_size: Batch size override.

        Returns:
            Number of flushed entries.
        """
        if self._dead_letters is None:
            raise DiskBufferError("DeadLetterStore not initialized")

        batch_size = batch_size or self._settings.flush_batch_size
        flushed = 0

        while True:
            # Collect batch
            entries = list(self.iter_entries(limit=batch_size))
            if not entries:
                break

            # Invoke handler
            try:
                success = handler(entries)
            except Exception as e:
                logger.exception(
                    "disk_buffer.flush_handler_error",
                    error=e,
                )
                # Poison-pill isolation attempt
                self._handle_flush_failure(entries, e)
                break

            if success:
                # Delete successful entries + clear retry counters
                keys = [e.key for e in entries]
                self._dead_letters.clear_retry_counters(keys)
                deleted = self.delete_batch(keys)
                flushed += deleted

                logger.debug(
                    "disk_buffer.flushed_entries",
                    deleted=deleted,
                )
            else:
                # Increment retry counters and check for poison pills
                for entry in entries:
                    self._dead_letters.increment_retry_counter(entry.key)
                    if self._dead_letters.is_poison_pill(entry.key):
                        self._dead_letters.move(entry, delete_fn=self.delete)
                break

        return flushed

    def _handle_flush_failure(
        self, entries: list[BufferEntry], error: Exception
    ) -> None:
        """Handle flush failure — isolate poison-pill entries."""
        if self._dead_letters is None:
            raise DiskBufferError("DeadLetterStore not initialized")

        for entry in entries:
            self._dead_letters.increment_retry_counter(entry.key)
            if self._dead_letters.is_poison_pill(entry.key):
                self._dead_letters.move(entry, delete_fn=self.delete, error=str(error))

    # ---------------------------------------------------------
    # Public API: Dead Letter management
    # ---------------------------------------------------------

    def get_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        """Retrieve dead-letter entries for operator review.

        Returns:
            List of dead-letter entry dicts.
        """
        if self._dead_letters is None:
            return []
        return self._dead_letters.get_dead_letters(limit=limit)

    def replay_dead_letter(self, key: bytes) -> bool:
        """Replay a dead-letter entry back into the main buffer.

        Args:
            key: Dead-letter entry key.

        Returns:
            ``True`` on success.
        """
        if self._dead_letters is None:
            return False
        return self._dead_letters.replay(key, put_fn=self.put)

    # ---------------------------------------------------------
    # Public API: Cleanup
    # ---------------------------------------------------------

    def cleanup_old_entries(self, max_age_seconds: float | None = None) -> int:
        """Clean up entries older than the retention period.

        Args:
            max_age_seconds: Maximum age in seconds.

        Returns:
            Number of deleted entries.
        """
        max_age = max_age_seconds or (self._settings.retention_hours * 3600)
        cutoff_time = time.time() - max_age

        keys_to_delete = []
        for entry in self.iter_entries():
            if entry.timestamp < cutoff_time:
                keys_to_delete.append(entry.key)

        if keys_to_delete:
            deleted = self.delete_batch(keys_to_delete)
            logger.info(
                "disk_buffer.cleaned_up_old_entries",
                deleted=deleted,
            )
            return deleted

        return 0

    # ---------------------------------------------------------
    # Public API: Status
    # ---------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return buffer statistics."""
        if self._env is None:
            # No "total_dropped" here: this LMDB layer never drops (bounded only
            # by disk). Drop accounting belongs to the adapter that fronts it.
            return {
                "count": 0,
                "total_added": self._stats.get("total_puts", 0),
                "capacity": None,
                "usage_percent": None,
                **self._stats,
                "db_size_bytes": 0,
                "sequence": 0,
            }

        with self._env.begin(db=self._entries_db) as txn:
            stat = txn.stat()

        current = stat["entries"]
        # No "total_dropped" here: this LMDB layer never drops (bounded only by
        # disk). Drop accounting belongs to the adapter that fronts it.
        return {
            "count": current,
            "total_added": self._stats.get("total_puts", 0),
            "capacity": None,
            "usage_percent": None,
            **self._stats,
            "db_size_bytes": stat.get("psize", 0) * stat.get("leaf_pages", 0),
            "sequence": self._sequence,
        }

    def get_health_status(self) -> dict[str, Any]:
        """Return health status for Kubernetes probes.

        Returns:
            Dict with ``healthy``, ``state``, ``entry_count``,
            ``disk_free_ratio``, ``dead_letter_count``, ``errors``.
        """
        errors: list[str] = []
        healthy = True

        # Buffer state
        state = self._state.name
        if self._state == BufferState.DISK_FULL_FAILOPEN:
            healthy = False
            errors.append("Disk full - fail-open mode active")
        elif self._state == BufferState.CORRUPTED:
            healthy = False
            errors.append("DB corrupted")

        # Entry count
        try:
            entry_count = self.count()
        except Exception as e:
            healthy = False
            errors.append(f"Cannot read entry count: {e}")
            entry_count = -1

        # Disk space (via DiskSpaceMonitor — uses recovery_threshold)
        disk_free_ratio = -1.0
        if self._disk_monitor is not None:
            disk_ok, disk_free_ratio, disk_errors = self._disk_monitor.is_healthy()
            errors.extend(disk_errors)
        else:
            errors.append("Disk monitor not initialised")

        # Dead letter count
        dead_letter_count = 0
        if self._settings.enable_dead_letter_db and self._dead_letters is not None:
            try:
                dead_letter_count = self._dead_letters.count()
                if dead_letter_count > 100:
                    errors.append(f"High dead letter count: {dead_letter_count}")
            except Exception:
                pass

        return {
            "healthy": healthy and len(errors) == 0,
            "state": state,
            "entry_count": entry_count,
            "disk_free_ratio": disk_free_ratio,
            "dead_letter_count": dead_letter_count,
            "errors": errors,
        }

    @property
    def state(self) -> BufferState:
        """Current buffer state."""
        return self._state

    # ---------------------------------------------------------
    # Resource management
    # ---------------------------------------------------------

    def close(self) -> None:
        """Close the buffer."""
        with self._lock:
            if self._env:
                # Flush group commit buffer
                if (
                    self._settings.group_commit_enabled
                    and self._group_writer is not None
                    and self._group_writer.pending_count > 0
                ):
                    try:
                        self._group_writer.flush_all()
                    except Exception as e:
                        logger.exception(
                            "disk_buffer.final_flush_failed",
                            error=e,
                        )

                self._env.close()
                self._env = None
                self._state = BufferState.CLOSED
                logger.info("disk_buffer.closed")

    def __enter__(self) -> DiskPersistentBuffer:
        """Context manager entry."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager exit."""
        self.close()


# -----------------------------------------------------------------
# Singleton
# -----------------------------------------------------------------

_buffer: DiskPersistentBuffer | None = None
_buffer_lock = threading.Lock()


def get_disk_buffer() -> DiskPersistentBuffer:
    """Return the DiskPersistentBuffer singleton."""
    global _buffer
    if _buffer is None:
        with _buffer_lock:
            if _buffer is None:
                _buffer = DiskPersistentBuffer()
    return _buffer


def reset_disk_buffer() -> None:
    """Reset the buffer singleton (for testing)."""
    global _buffer
    with _buffer_lock:
        if _buffer is not None:
            _buffer.close()
            _buffer = None
