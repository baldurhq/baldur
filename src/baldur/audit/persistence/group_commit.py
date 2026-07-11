"""Group commit writer for the disk-persistent buffer.

Batches multiple entries and flushes them in a single LMDB
transaction to minimise fsync overhead.
"""

from __future__ import annotations

import struct
import time
from typing import Any

import structlog

from baldur.audit.persistence.checksum import compute_checksum
from baldur.audit.persistence.config import DiskBufferSettings
from baldur.utils.serialization import fast_dumps_str
from baldur.utils.time import utc_now

__all__ = [
    "GroupCommitWriter",
]

logger = structlog.get_logger()


class GroupCommitWriter:
    """Batched writer that coalesces multiple puts into a single fsync.

    Args:
        env: LMDB environment handle.
        entries_db: LMDB entries database handle.
        meta_db: LMDB meta database handle.
        settings: Disk buffer settings.
        stats: Mutable stats dict shared with the parent buffer.
        get_sequence: Callable returning the current sequence number.
        set_sequence: Callable to update the sequence number.
    """

    def __init__(
        self,
        *,
        env: Any,
        entries_db: Any,
        meta_db: Any,
        settings: DiskBufferSettings,
        stats: dict[str, int],
        get_sequence: Any,
        set_sequence: Any,
    ) -> None:
        self._env = env
        self._entries_db = entries_db
        self._meta_db = meta_db
        self._settings = settings
        self._stats = stats
        self._get_sequence = get_sequence
        self._set_sequence = set_sequence

        self._group_buffer: list[dict[str, Any]] = []
        self._last_flush_time: float = time.time()
        # Re-entrancy guard: a signal handler firing while the owning
        # thread is inside flush()'s open write txn re-enters flush()
        # on the SAME thread (the buffer RLock re-entry succeeds), and
        # the nested env.begin(write=True) would deadlock on LMDB's
        # non-recursive writer mutex. A plain bool suffices — signal
        # handlers run between bytecodes of the same thread, and
        # cross-thread exclusion is provided by the buffer RLock held
        # by every flush() caller.
        self._flush_in_progress: bool = False

    # ── Internal helpers ──────────────────────────────────

    @property
    def pending_count(self) -> int:
        """Number of buffered entries awaiting flush.

        ``len`` on a list is GIL-atomic, so this is safe to read from
        signal context without holding the buffer lock. Exposes only the
        count — never the live buffer — so callers cannot drop or
        reorder pre-durability audit entries.
        """
        return len(self._group_buffer)

    def _generate_key(self) -> bytes:
        """Generate a unique key (timestamp + sequence)."""
        seq = self._get_sequence() + 1
        self._set_sequence(seq)
        timestamp = time.time()
        return f"{timestamp:.6f}:{seq:08d}".encode()

    def _time_since_last_flush_ms(self) -> float:
        """Elapsed time since the last flush in milliseconds."""
        return (time.time() - self._last_flush_time) * 1000

    def _save_sequence(self, txn: Any) -> None:
        """Persist the current sequence number into the meta database."""
        txn.put(
            b"last_sequence",
            struct.pack(">Q", self._get_sequence()),
            db=self._meta_db,
        )

    # ── Public API ────────────────────────────────────────

    def put(self, entry: dict[str, Any]) -> bytes:
        """Buffer an entry for group commit.

        The entry is appended to the in-memory group buffer and
        flushed when either the max-entries or interval threshold
        is reached.

        Args:
            entry: Event data dict.

        Returns:
            The generated key for the entry.
        """
        key = self._generate_key()

        entry_with_meta = {
            **entry,
            "_stored_at": utc_now().isoformat(),
            "_buffer_key": key.decode(),
        }

        self._group_buffer.append(
            {
                "key": key,
                "entry": entry_with_meta,
            }
        )

        # Flush conditions
        should_flush = (
            len(self._group_buffer) >= self._settings.group_commit_max_entries
            or self._time_since_last_flush_ms()
            >= self._settings.group_commit_interval_ms
        )

        if should_flush:
            self.flush()

        return key

    def flush(self) -> None:
        """Flush the group buffer in a single LMDB transaction.

        A re-entrant call made while a flush is in progress returns
        immediately: the interrupted flush resumes and commits the same
        entries after the caller returns. Skipping is safe — keys are
        fixed per item at put time, so even a re-flush would be an
        overwrite of identical data, and later arrivals are owned by
        the shutdown teardown.
        """
        if not self._group_buffer:
            return

        if self._flush_in_progress:
            return

        self._flush_in_progress = True
        try:
            with self._env.begin(write=True, db=self._entries_db) as txn:
                for item in self._group_buffer:
                    key = item["key"]
                    entry = item["entry"]

                    data = fast_dumps_str(entry, default=str)
                    data_bytes = data.encode("utf-8")
                    checksum = compute_checksum(data_bytes)
                    value = struct.pack(">I", checksum) + data_bytes

                    txn.put(key, value)
                    self._stats["total_puts"] += 1

                self._save_sequence(txn)

            # Periodic sync when sync_on_write is disabled
            if not self._settings.sync_on_write:
                self._env.sync()

            self._stats["group_commit_flushes"] += 1
            self._group_buffer.clear()
            self._last_flush_time = time.time()
        finally:
            self._flush_in_progress = False

    def flush_all(self) -> None:
        """Force flush all pending entries (for graceful shutdown)."""
        self.flush()
