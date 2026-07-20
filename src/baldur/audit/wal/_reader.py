"""
WAL file read/recovery module.

Unifies _read_wal_file and _read_wal_file_best_effort — structurally identical
in the original code — behind a mode parameter.
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog

from baldur.audit.wal._serialization import (
    compute_checksum,
    verify_checksum,
)
from baldur.core.file_utils import safe_unlink
from baldur.utils.serialization import fast_loads

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from baldur.audit.wal._models import WALConfig, WALCorruptionError

logger = structlog.get_logger()

# Upper bound on a single WAL record's declared length. A length prefix larger
# than this is treated as corruption rather than driving an unbounded f.read()
# (a ~4 GB malloc → OOM), regardless of recovery mode.
_MAX_RECORD_SIZE_BYTES = 10 * 1024 * 1024


def _wal_glob_pattern(file_prefix: str, mode: Literal["runtime", "startup"]) -> str:
    """Glob pattern for WAL files.

    - ``mode="startup"``: matches all PIDs — absorbs orphan WAL files
      from crashed peer workers on process startup.
    - ``mode="runtime"``: matches this worker's PID only — protects
      peer workers' still-active WAL files from concurrent recovery
      writes/deletes during the lazy recovery loop (#470 G3, G4, G5).
    """
    if mode == "runtime":
        return f"{file_prefix}_*_{os.getpid()}.wal"
    return f"{file_prefix}_*.wal"


class WALReaderMixin:
    """WAL file read/recovery methods."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by WriteAheadLog.
        _config: WALConfig
        _wal_dir: Path
        _lock: threading.RLock
        _current_file: Path | None
        _recovered_entries: int
        _corrupted_entries: int
        _on_corruption: Callable[[WALCorruptionError], None] | None

        # File-format constants from WriteAheadLog class body.
        HEADER_SIZE: int
        MAGIC: bytes

        def _record_audit_event(
            self, event_type: str, details: dict[str, Any]
        ) -> None: ...

    def _read_wal_file(self, filepath: Path) -> Iterator[Any]:
        """
        Read a WAL file.

        Args:
            filepath: WAL file path

        Yields:
            WALEntry objects
        """
        yield from self._read_wal_file_impl(filepath, best_effort=False)

    def _read_wal_file_best_effort(self, filepath: Path) -> Iterator[Any]:
        """
        Read a WAL file in best-effort recovery mode.

        Skips corrupted records and recovers as many entries as possible.
        """
        yield from self._read_wal_file_impl(filepath, best_effort=True)

    def _read_wal_file_impl(  # noqa: C901, PLR0912, PLR0915
        self, filepath: Path, best_effort: bool = False
    ) -> Iterator[Any]:
        """
        Unified WAL file read implementation.

        Merges the identical structures of _read_wal_file and
        _read_wal_file_best_effort into one.

        Args:
            filepath: WAL file path
            best_effort: If True, skip corrupted records and keep going

        Yields:
            WALEntry objects
        """
        from baldur.audit.wal._models import WALCorruptionError

        # Drift Detection metrics (optional import)
        try:
            from baldur.metrics.drift_metrics import record_wal_corruption

            has_metrics = True
        except ImportError:
            has_metrics = False

        try:
            with open(filepath, "rb") as f:
                # Read the header
                header = f.read(self.HEADER_SIZE)
                if len(header) < self.HEADER_SIZE:
                    return

                magic = header[:4]
                if magic != self.MAGIC:
                    return

                # Read records
                while True:
                    # Read the length
                    length_bytes = f.read(4)
                    if len(length_bytes) < 4:
                        break

                    length = struct.unpack(">I", length_bytes)[0]

                    # An oversized length prefix (corruption/attack) must never
                    # drive an unbounded f.read() → multi-GB malloc → OOM.
                    if length > _MAX_RECORD_SIZE_BYTES:
                        if best_effort:
                            # Try to resync to the next valid record.
                            if not self._handle_corrupted_record_length(f):
                                break
                            continue
                        # Strict mode stops at the corruption boundary.
                        self._corrupted_entries += 1
                        break

                    # Read the checksum
                    checksum_bytes = f.read(8)
                    if len(checksum_bytes) < 8:
                        break

                    if best_effort:
                        checksum = checksum_bytes.decode("ascii", errors="replace")
                    else:
                        checksum = checksum_bytes.decode("ascii")

                    # Read the data
                    data_bytes = f.read(length)
                    if len(data_bytes) < length:
                        break

                    # Verify the checksum
                    if not verify_checksum(data_bytes, checksum):
                        self._corrupted_entries += 1

                        if best_effort:
                            if self._config.best_effort_recovery:
                                continue
                            break
                        else:
                            computed_cs = compute_checksum(data_bytes)
                            error = WALCorruptionError(
                                f"Checksum mismatch in {filepath}",
                                sequence=-1,
                                expected=checksum,
                                computed=computed_cs,
                            )
                            if has_metrics:
                                record_wal_corruption()
                            self._record_audit_event(
                                event_type="WAL_CORRUPTION_DETECTED",
                                details={
                                    "filepath": str(filepath),
                                    "expected_checksum": checksum,
                                    "computed_checksum": computed_cs,
                                },
                            )
                            if self._on_corruption:
                                self._on_corruption(error)
                            continue

                    # JSON parsing
                    entry = self._parse_wal_record(data_bytes, checksum)
                    if entry is not None:
                        yield entry
                    elif best_effort and not self._config.best_effort_recovery:
                        break

        except Exception:
            pass

    def _handle_corrupted_record_length(self, f) -> bool:
        """Handle a corrupted record length. True if reading can continue."""
        if self._config.best_effort_recovery:
            pos = self._scan_for_valid_record(f)
            return pos != -1
        return False

    def _parse_wal_record(self, data_bytes: bytes, checksum: str):
        """Parse a WAL record. None on failure."""
        from baldur.audit.wal._models import WALEntry

        try:
            entry_dict = fast_loads(data_bytes)
            return WALEntry(
                sequence=entry_dict["seq"],
                timestamp=entry_dict["ts"],
                data=entry_dict["data"],
                checksum=checksum,
            )
        except (ValueError, KeyError, TypeError):
            # TypeError: decoded payload was a valid-JSON scalar/array, not an
            # object — subscripting it must be treated as a corrupt record.
            self._corrupted_entries += 1
            return None

    def _scan_for_valid_record(self, f) -> int:
        """
        Scan forward to the next valid record position.

        Skips the corrupted region and finds the next valid JSON record.
        """
        scan_buffer = bytearray()
        max_scan_bytes = 1024 * 1024  # scan at most 1MB
        scanned = 0

        while scanned < max_scan_bytes:
            byte = f.read(1)
            if not byte:
                return -1

            scan_buffer.append(byte[0])
            scanned += 1

            if len(scan_buffer) > 20:
                try:
                    potential_checksum = bytes(scan_buffer[-8:]).decode("ascii")
                    if all(c in "0123456789abcdef" for c in potential_checksum.lower()):
                        f.seek(f.tell() - 8)
                        f.seek(f.tell() - 4)
                        return f.tell()
                except Exception:
                    pass

                if len(scan_buffer) > 1024:
                    scan_buffer = scan_buffer[-512:]

        return -1

    def recover_unprocessed(
        self,
        last_processed_seq: int = 0,
        mode: Literal["runtime", "startup"] = "startup",
    ) -> list:
        """
        Recover entries with sequence > ``last_processed_seq``.

        Files are read independently in parallel, then merged by
        sequence.

        Args:
            last_processed_seq: Last sequence already processed.
            mode: ``"startup"`` (default) globs all PIDs — absorbs
                orphan files from crashed peers on process startup.
                ``"runtime"`` filters to this worker's PID only — used
                by ``ResilientStorageBackend._do_recovery()`` so peer
                workers' still-active WAL files are not over-replayed
                or deleted during the lazy recovery loop.

        Returns:
            List of unprocessed ``WALEntry`` objects.
        """
        glob_pattern = _wal_glob_pattern(self._config.file_prefix, mode)
        wal_files = sorted(self._wal_dir.glob(glob_pattern))

        if not wal_files:
            return []

        try:
            from baldur.metrics.drift_metrics import record_wal_entries_recovered

            has_metrics = True
        except ImportError:
            has_metrics = False

        # OOM defense: guard on runtime available memory via CgroupResourceMonitor
        estimated_bytes = sum(f.stat().st_size for f in wal_files)
        estimated_memory = estimated_bytes * 3  # JSON parsing + WALEntry overhead

        try:
            from baldur.core.resource_monitor import CgroupResourceMonitor

            available = CgroupResourceMonitor.get_available_memory_bytes()
            if available is not None and estimated_memory > available:
                logger.critical(
                    "wal.recovery_memory_guard_blocked",
                    estimated_mb=estimated_memory // (1024 * 1024),
                    available_mb=available // (1024 * 1024),
                    file_count=len(wal_files),
                )
                return self._recover_chunked(
                    wal_files,
                    last_processed_seq,
                    available,
                )
        except ImportError:
            pass  # non-K8s environment — skip the guard

        max_workers = min(
            self._config.recovery_max_workers,
            len(wal_files),
        )

        if max_workers <= 1:
            # A single file only adds parallelization overhead — serial path
            logger.info(
                "wal.sequential_recovery_started",
                file_count=len(wal_files),
            )
            sorted_entries = self._recover_sequential(wal_files, last_processed_seq)
        else:
            # Independent per-file reads (no lock — read-only, per-file handle)
            logger.info(
                "wal.parallel_recovery_started",
                file_count=len(wal_files),
                max_workers=max_workers,
            )
            all_entries: list = []

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {
                    executor.submit(
                        self._read_file_entries, wal_file, last_processed_seq
                    ): wal_file
                    for wal_file in wal_files
                }

                for future in as_completed(future_to_file):
                    wal_file = future_to_file[future]
                    try:
                        file_entries = future.result()
                        all_entries.extend(file_entries)
                    except Exception:
                        logger.exception(
                            "wal.parallel_recovery_file_error",
                            wal_file=str(wal_file),
                        )

            sorted_entries = sorted(all_entries, key=lambda e: e.sequence)
            self._recovered_entries += len(sorted_entries)

        if has_metrics and sorted_entries:
            record_wal_entries_recovered(len(sorted_entries))

        if sorted_entries:
            self._record_audit_event(
                event_type="WAL_RECOVERED",
                details={
                    "recovered_count": len(sorted_entries),
                    "last_processed_seq": last_processed_seq,
                    "new_last_seq": sorted_entries[-1].sequence,
                    "parallel_workers": max_workers,
                },
            )

        logger.info(
            "wal.parallel_recovery_completed",
            recovered_count=len(sorted_entries),
            parallel_workers=max_workers,
        )

        return sorted_entries

    def _read_file_entries(self, wal_file, last_processed_seq: int) -> list:
        """
        Read unprocessed entries from a single WAL file (parallel-safe).

        Uses best_effort mode so intact entries are recovered as far as
        possible even under partial corruption. On an I/O error it returns
        the partial result up to the error and sends a CRITICAL notification.
        """
        entries = []
        try:
            for entry in self._read_wal_file_best_effort(wal_file):
                if entry.sequence > last_processed_seq:
                    entries.append(entry)
        except OSError as e:
            logger.critical(
                "wal.parallel_recovery_partial_corruption",
                wal_file=str(wal_file),
                recovered_before_error=len(entries),
                error=str(e),
            )
            try:
                from baldur_pro.services.unified_notification import (
                    NotificationCategory,
                    NotificationPayload,
                    NotificationPriority,
                    UnifiedNotificationManager,
                )

                payload = NotificationPayload(
                    title="WAL Recovery Partial Corruption",
                    message=(
                        f"WAL file {wal_file.name} I/O error during recovery. "
                        f"{len(entries)} entries partially recovered. Check disk status."
                    ),
                    priority=NotificationPriority.CRITICAL,
                    category=NotificationCategory.OPERATIONS,
                    source="WALParallelRecovery",
                    dedup_key=f"wal:partial_corruption:{wal_file.name}",
                )
                UnifiedNotificationManager().notify(payload)
            except Exception:
                pass
        return entries  # return even a partial result — WAL minimizes data loss

    def _orphan_wal_files(self, file_prefix: str) -> list[Path]:
        """Non-own-PID (orphan) WAL file paths in the shared ``wal_dir``.

        Computed as ``startup-glob`` (all PIDs) minus ``runtime-glob``
        (this worker's PID) so the result is exactly peer/dead-PID files.
        """
        all_files = set(self._wal_dir.glob(_wal_glob_pattern(file_prefix, "startup")))
        own_files = set(self._wal_dir.glob(_wal_glob_pattern(file_prefix, "runtime")))
        return sorted(all_files - own_files)

    def recover_orphans(self, last_processed_seq: int = 0) -> list:
        """
        Recover unprocessed entries from orphan (non-own-PID) WAL files only.

        Globs ``{file_prefix}_*.wal`` and **excludes this worker's own-PID
        files**, so it returns entries from peer/dead-PID files only —
        disjoint from this worker's own runtime drain
        (``recover_unprocessed(mode="runtime")``). Used once at worker
        startup to absorb a crashed peer's orphan entries to the central
        store.

        Unlike ``recover_unprocessed``, this reads via ``_read_file_entries``
        directly and emits **neither** the ``WAL_RECOVERED`` audit event nor
        the ``wal.parallel_recovery_completed`` log — the caller
        (``AuditSyncWorker.absorb_orphans``) is responsible for its own
        summary event. It also does not advance ``_recovered_entries``.

        The caller MUST NOT advance its own processed-sequence cursor with
        these entries (orphan seqs live in foreign sequence spaces) and MUST
        NOT ``cleanup_processed`` cross-PID — orphan files are reclaimed by
        the WAL's own retention. Re-absorption of an as-yet-unreclaimed
        orphan is deduplicated by the consumer's idempotency guard.

        Args:
            last_processed_seq: Lower bound — only entries with
                ``sequence > last_processed_seq`` are returned. Defaults to
                ``0`` (absorb all orphan entries), since orphan files have no
                coherent per-this-worker cursor.

        Returns:
            List of unprocessed ``WALEntry`` objects from orphan files,
            sorted by sequence.
        """
        orphan_files = self._orphan_wal_files(self._config.file_prefix)
        if not orphan_files:
            return []

        entries: list = []
        for wal_file in orphan_files:
            entries.extend(self._read_file_entries(wal_file, last_processed_seq))

        return sorted(entries, key=lambda e: e.sequence)

    def _recover_sequential(self, wal_files, last_processed_seq: int) -> list:
        """Serial recovery path (a single file, or parallelism disabled)."""
        entries = []
        with self._lock:
            for wal_file in wal_files:
                for entry in self._read_wal_file(wal_file):
                    if entry.sequence > last_processed_seq:
                        entries.append(entry)
                        self._recovered_entries += 1

        return sorted(entries, key=lambda e: e.sequence)

    def _recover_chunked(
        self,
        wal_files,
        last_processed_seq: int,
        available_bytes: int,
    ) -> list:
        """
        Memory-bounded chunked recovery.

        When the OOM guard trips, files are processed one at a time to bound
        memory use. Results are sorted/accumulated right after each file to
        lower the memory peak.
        """
        all_entries: list = []
        consumed = 0

        for wal_file in wal_files:
            file_size = wal_file.stat().st_size
            file_estimated = file_size * 3

            if file_estimated > (available_bytes - consumed):
                logger.warning(
                    "wal.chunked_recovery_file_skipped",
                    wal_file=str(wal_file),
                    file_size_mb=file_size // (1024 * 1024),
                    available_mb=available_bytes // (1024 * 1024),
                )
                continue

            try:
                for entry in self._read_wal_file_best_effort(wal_file):
                    if entry.sequence > last_processed_seq:
                        all_entries.append(entry)
                consumed += file_estimated
            except OSError:
                logger.exception(
                    "wal.chunked_recovery_file_error",
                    wal_file=str(wal_file),
                )

        sorted_entries = sorted(all_entries, key=lambda e: e.sequence)
        self._recovered_entries += len(sorted_entries)

        if sorted_entries:
            self._record_audit_event(
                event_type="WAL_RECOVERED",
                details={
                    "recovered_count": len(sorted_entries),
                    "last_processed_seq": last_processed_seq,
                    "new_last_seq": sorted_entries[-1].sequence,
                    "mode": "chunked",
                },
            )

        return sorted_entries

    def cleanup_processed(
        self,
        last_processed_seq: int,
        mode: Literal["runtime", "startup"] = "startup",
    ) -> int:
        """
        Delete WAL files whose entries are all already processed.

        Optimization: a per-file lightweight scan extracts only the
        ``seq`` field from the JSON record (skips checksum and
        ``WALEntry`` construction) since this is invoked from
        ``sync_worker`` every ``sync_interval``.

        Args:
            last_processed_seq: Last sequence already processed.
            mode: ``"startup"`` (default) globs all PIDs — preserves
                the existing safety contract for callers that drain
                orphan files. ``"runtime"`` filters to this worker's
                PID only — used by
                ``ResilientStorageBackend._do_recovery()`` so a peer
                worker's still-active WAL file is never deleted by
                this worker (#470 G3).

        Returns:
            Number of deleted files.
        """
        deleted_count = 0
        glob_pattern = _wal_glob_pattern(self._config.file_prefix, mode)

        with self._lock:
            wal_files = sorted(self._wal_dir.glob(glob_pattern))

            for wal_file in wal_files:
                if self._current_file and wal_file == self._current_file:
                    continue

                max_seq = self._get_file_max_sequence(wal_file)

                if (
                    max_seq > 0
                    and max_seq <= last_processed_seq
                    and safe_unlink(wal_file)
                ):
                    deleted_count += 1

        return deleted_count

    def _get_file_max_sequence(self, wal_file: Path) -> int:
        """
        Read a WAL file's max sequence number via a lightweight scan.

        Completely bypasses the _read_wal_file() → _parse_wal_record() →
        WALEntry() construction path. Checksum verification is skipped too
        (this only decides deletion, so integrity checking is unnecessary).

        cleanup_processed() is invoked by sync_worker every sync_interval
        (1s), so this lightweight scan cuts hot-path CPU/GC overhead.
        """
        max_seq = 0
        try:
            with open(wal_file, "rb") as f:
                header = f.read(self.HEADER_SIZE)
                if len(header) < self.HEADER_SIZE or header[:4] != self.MAGIC:
                    return 0

                while True:
                    length_bytes = f.read(4)
                    if len(length_bytes) < 4:
                        break
                    length = struct.unpack(">I", length_bytes)[0]

                    if length > _MAX_RECORD_SIZE_BYTES:  # oversized → corruption
                        break

                    f.read(8)  # checksum — skipped
                    data_bytes = f.read(length)
                    if len(data_bytes) < length:
                        break

                    try:
                        parsed = fast_loads(data_bytes)
                        seq = parsed.get("seq", 0) if isinstance(parsed, dict) else 0
                        if isinstance(seq, int) and seq > max_seq:
                            max_seq = seq
                    except ValueError:
                        pass
        except OSError:
            pass
        return max_seq
