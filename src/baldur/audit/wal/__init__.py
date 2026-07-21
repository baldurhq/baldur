"""
Write-Ahead Log (WAL) with CRC32 Checksum.

Data integrity guarantees:
1. Write to the WAL before recording in memory
2. CRC32 checksum on every entry
3. Checksum verification on recovery

Minimal dependencies: standard library only (struct, json, zlib, os, threading)

Usage:
    from baldur.audit.wal import WriteAheadLog, WALEntry, WALConfig

    wal = WriteAheadLog(wal_dir="/var/log/audit/wal")
    seq = wal.write({"event": "config_change", "key": "max_retries"})
    entries = wal.recover_unprocessed(last_processed_seq=100)
    wal.cleanup_processed(last_processed_seq=500)
"""

from __future__ import annotations

import os
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from baldur.audit.wal._disk_manager import WALDiskManagerMixin
from baldur.audit.wal._models import (
    LEGACY_WAL_DIR_ENV_VAR,
    WAL_DIR_ENV_VAR,
    WALConfig,
    WALCorruptionError,
    WALEntry,
    WALError,
    WALState,
    WALStats,
)
from baldur.audit.wal._reader import WALReaderMixin
from baldur.audit.wal._serialization import compute_checksum, verify_checksum
from baldur.audit.wal._writer import WALWriterMixin
from baldur.core.file_utils import safe_unlink
from baldur.utils.fs import ResolvedDir, resolve_writable_dir

logger = structlog.get_logger()

# Drift Detection metrics
try:
    from baldur.metrics.drift_metrics import (
        record_wal_rotation,
        update_wal_sync_lag,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False


class WriteAheadLog(
    WALWriterMixin,
    WALReaderMixin,
    WALDiskManagerMixin,
):
    """
    Write-Ahead Log with CRC32 Checksum.

    Characteristics:
    - Thread-safe
    - Integrity verification via CRC32 checksum
    - File rotation
    - Recovery of unprocessed entries
    - Best-Effort Recovery (marker-based recovery on corruption)
    """

    # File format constants
    MAGIC = b"AWAL"
    VERSION = 1
    HEADER_SIZE = 8
    RECORD_HEADER_SIZE = 12
    RECORD_MAGIC = b"\xab\xcd"
    RECORD_MAGIC_HEADER_SIZE = 14

    def __init__(
        self,
        config: WALConfig | None = None,
        on_rotate: Callable[[str], None] | None = None,
        on_corruption: Callable[[WALCorruptionError], None] | None = None,
        audit_adapter=None,
    ):
        """
        Initialize the WAL.

        Args:
            config: WAL configuration
            on_rotate: Callback on file rotation
            on_corruption: Callback when corruption is found
            audit_adapter: Audit adapter (for event recording)
        """
        self._config = config or WALConfig()
        self._on_rotate = on_rotate
        self._on_corruption = on_corruption
        self._audit_adapter = audit_adapter

        # Re-assigned in _init_or_recover() once the directory is resolved.
        # The mixins read this attribute, never ``config.wal_dir``, so
        # rotation, cleanup, disk-usage and recovery all follow a fallback
        # together.
        self._wal_dir = Path(self._config.wal_dir)
        self._resolved_dir: ResolvedDir | None = None
        self._current_file: Path | None = None
        self._current_handle: Any | None = None
        self._sequence = 0
        self._state = WALState.ACTIVE
        self._lock = threading.RLock()

        # Statistics
        self._total_entries = 0
        self._corrupted_entries = 0
        self._recovered_entries = 0
        self._last_write_time: float | None = None

        # Group Commit buffer
        self._group_buffer: list[dict[str, Any]] = []
        self._last_flush_time: float = time.time()
        self._group_commit_flushes: int = 0

        # Initialization
        self._init_or_recover()

    def _init_or_recover(self) -> None:
        """Create WAL directory and recover this worker's last
        sequence.

        Filters the glob to self-PID files (``_*_<pid>.wal``) — a new
        worker must not inherit a peer worker's sequence number, since
        that peer is still incrementing it (#470 G5). With multiple
        live workers writing into a shared ``wal_dir``, the
        lexicographically-last file is an arbitrary peer's WAL, not
        this worker's. Filtering by PID guarantees that a fresh
        process starts its sequence at 0 and an existing process can
        recover its own last sequence after, e.g., a re-init cycle.

        Raises:
            ConfigurationError: When an operator-chosen ``wal_dir`` is not
                writable, or when no fallback directory is writable.
        """
        self._resolved_dir = resolve_writable_dir(
            self._config.wal_dir,
            purpose=f"wal_{self._config.file_prefix}",
            operator_set=self._config.wal_dir_operator_set,
            env_override_name=self._config.wal_dir_env_var,
        )
        self._wal_dir = self._resolved_dir.path

        own_pid_pattern = f"{self._config.file_prefix}_*_{os.getpid()}.wal"
        wal_files = sorted(self._wal_dir.glob(own_pid_pattern))
        if wal_files:
            last_file = wal_files[-1]
            try:
                for entry in self._read_wal_file(last_file):
                    self._sequence = max(self._sequence, entry.sequence)
            except Exception:
                pass

    @property
    def wal_dir(self) -> Path:
        """Directory WAL files are written to (post-resolution)."""
        return self._wal_dir

    @property
    def resolved_dir(self) -> ResolvedDir | None:
        """Directory-resolution outcome, ``None`` before initialization."""
        return self._resolved_dir

    # =========================================================================
    # File Management
    # =========================================================================

    def _get_current_wal_filename(self) -> str:
        """Build the current WAL filename (includes the PID)."""
        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        return f"{self._config.file_prefix}_{timestamp}_{pid}.wal"

    def _ensure_file_open(self) -> None:
        """Ensure the WAL file is open, creating it when needed."""
        if self._current_handle is None or self._current_file is None:
            self._current_file = self._wal_dir / self._get_current_wal_filename()
            self._current_handle = open(self._current_file, "ab")  # noqa: SIM115

            if self._current_handle.tell() == 0:
                self._write_header()

    def _write_header(self) -> None:
        """Write the WAL file header."""
        if self._current_handle:
            header = self.MAGIC + struct.pack(">HH", self.VERSION, 0)
            self._current_handle.write(header)
            self._current_handle.flush()

    def _rotate_file(self) -> None:
        """Rotate the WAL file."""
        with self._lock:
            old_state = self._state
            self._state = WALState.ROTATING

            try:
                old_file = self._current_file
                old_size = 0

                if self._current_handle:
                    old_size = self._current_handle.tell()
                    self._current_handle.flush()
                    if self._config.sync_on_write:
                        os.fsync(self._current_handle.fileno())
                    self._current_handle.close()
                    self._current_handle = None

                self._current_file = None

                if old_file:
                    if HAS_DRIFT_METRICS:
                        record_wal_rotation()
                    self._record_audit_event(
                        event_type="WAL_ROTATED",
                        details={
                            "old_file": str(old_file),
                            "old_size_bytes": old_size,
                        },
                    )

                if self._on_rotate and old_file:
                    try:
                        self._on_rotate(str(old_file))
                    except Exception:
                        pass

                self._cleanup_old_files()

            finally:
                self._state = (
                    old_state if old_state != WALState.ROTATING else WALState.ACTIVE
                )

    def _cleanup_old_files(self) -> None:
        """Clean up old WAL files."""
        wal_files = sorted(self._wal_dir.glob(f"{self._config.file_prefix}_*.wal"))

        while len(wal_files) > self._config.max_files:
            oldest = wal_files.pop(0)
            safe_unlink(oldest)

    # =========================================================================
    # Stats & Lifecycle
    # =========================================================================

    def get_stats(self) -> WALStats:
        """Read WAL statistics."""
        with self._lock:
            current_size = 0
            if self._current_handle:
                try:
                    current_size = self._current_handle.tell()
                except Exception:
                    pass

            total_files = len(
                list(self._wal_dir.glob(f"{self._config.file_prefix}_*.wal"))
            )

            return WALStats(
                state=self._state,
                current_file=str(self._current_file) if self._current_file else None,
                current_size_bytes=current_size,
                total_entries=self._total_entries,
                total_files=total_files,
                last_sequence=self._sequence,
                last_write_time=self._last_write_time,
                corrupted_entries=self._corrupted_entries,
                recovered_entries=self._recovered_entries,
            )

    def count_unprocessed(self, last_processed_seq: int = 0) -> int:
        """Return the number of unprocessed entries."""
        with self._lock:
            return max(0, self._sequence - last_processed_seq)

    def get_sync_lag(self, last_synced_seq: int = 0) -> int:
        """Compute the sync lag against the central store."""
        with self._lock:
            lag = max(0, self._sequence - last_synced_seq)
            if HAS_DRIFT_METRICS:
                update_wal_sync_lag(lag)
            return lag

    def flush(self) -> None:
        """
        Flush the buffer.

        In Group Commit mode this flushes the buffer; in normal mode it
        syncs the current file.

        NOTE: an earlier version defined flush() twice, so the Group Commit
        flush never ran. Both behaviors are now handled by this single method.
        """
        with self._lock:
            # Flush the Group Commit buffer first, if any
            if self._config.group_commit_enabled and self._group_buffer:
                self._flush_buffer()

            # Sync the current file
            if self._current_handle:
                self._current_handle.flush()
                if self._config.sync_on_write:
                    os.fsync(self._current_handle.fileno())

    def close(self) -> None:
        """Close the WAL."""
        with self._lock:
            self._state = WALState.CLOSED

            if self._current_handle:
                try:
                    self._current_handle.flush()
                    os.fsync(self._current_handle.fileno())
                    self._current_handle.close()
                except Exception:
                    pass
                finally:
                    self._current_handle = None

            self._current_file = None

    def __enter__(self) -> WriteAheadLog:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _record_audit_event(self, event_type: str, details: dict[str, Any]) -> None:
        """Record a WAL meta-event (WAL_ROTATED / WAL_RECOVERED /
        WAL_CORRUPTION_DETECTED) to the audit trail.

        When an ``audit_adapter`` is wired, the meta-event is routed through
        the canonical ``AuditLogAdapter.log()`` contract; the emitting
        component is preserved in ``details["source"]`` since ``AuditEntry``
        has no dedicated source field. With no adapter, it falls through to
        the WAL itself. Both branches are fail-open.
        """
        if self._audit_adapter is not None:
            try:
                from baldur.interfaces.audit_adapter import AuditEntry

                self._audit_adapter.log(
                    AuditEntry(
                        action=event_type,
                        details={**details, "source": "WriteAheadLog"},
                    )
                )
                return
            except Exception:
                pass

        try:
            from baldur_pro.services.audit.base import _write_to_wal

            _write_to_wal(
                event_type=event_type,
                source="WriteAheadLog",
                details=details,
            )
        except ImportError:
            pass
        except Exception:
            pass


# =============================================================================
# Convenience functions
# =============================================================================


def create_wal(
    wal_dir: str = "/var/log/audit/wal",
    max_file_size_mb: int = 100,
    sync_on_write: bool = True,
    wal_dir_operator_set: bool = False,
) -> WriteAheadLog:
    """Helper to create a WAL.

    Args:
        wal_dir: Directory WAL files are written to.
        max_file_size_mb: Rotation threshold per WAL file.
        sync_on_write: Whether to fsync every write.
        wal_dir_operator_set: Set this to ``True`` whenever ``wal_dir``
            comes from operator input. An operator-chosen directory that is
            unwritable raises instead of silently falling back.
    """
    config = WALConfig(
        wal_dir=wal_dir,
        max_file_size_mb=max_file_size_mb,
        sync_on_write=sync_on_write,
        wal_dir_operator_set=wal_dir_operator_set,
    )
    return WriteAheadLog(config=config)


from baldur.audit.wal._cleanup import (
    atomic_rewrite,
    cleanup_by_age,
    cleanup_by_namespace,
    cleanup_by_sequence,
)
from baldur.audit.wal._jsonl import CommitMarker, JSONLReader, JSONLWriter
from baldur.utils.time import utc_now

__all__ = [
    "LEGACY_WAL_DIR_ENV_VAR",
    "WAL_DIR_ENV_VAR",
    "WriteAheadLog",
    "WALEntry",
    "WALConfig",
    "WALStats",
    "WALError",
    "WALCorruptionError",
    "WALState",
    "create_wal",
    "JSONLWriter",
    "JSONLReader",
    "CommitMarker",
    "atomic_rewrite",
    "cleanup_by_sequence",
    "cleanup_by_age",
    "cleanup_by_namespace",
]
