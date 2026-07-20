"""
CB state snapshot - backed by shared memory (mmap).

Stores Circuit Breaker state in shared memory to provide low-latency lookups
on the order of ~10us.

Key characteristics:
- mmap-based shared memory
- binary struct serialization
- lock-free reads (atomic write)
- periodic snapshot updates (100ms)

Memory layout:
- Header (24 bytes): magic, version, timestamp, cb_count
- CB Entry (72 bytes each): cb_id (32), state (4), failure_count (4),
                            success_count (4), last_failure (8),
                            last_success (8), failure_threshold (4),
                            recovery_timeout (8)

Usage:
    from baldur.adapters.ipc.cb_state_snapshot import (
        CBStateSnapshot,
        get_cb_state_snapshot,
    )

    snapshot = get_cb_state_snapshot()
    snapshot.start()

    # State lookup (~10us)
    state = snapshot.get_state("payment_service")

    snapshot.stop()
"""

from __future__ import annotations

import mmap
import os
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin

if TYPE_CHECKING:
    from io import BufferedRandom  # noqa: F401

    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()


class CBState(IntEnum):
    """Circuit Breaker state."""

    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


# =============================================================================
# Memory layout constants
# =============================================================================

# Magic Number: "CBSS" in ASCII
MAGIC_NUMBER = 0x43425353

# Version
VERSION = 1

# Header size (24 bytes)
# - magic: 4 bytes (uint32)
# - version: 4 bytes (uint32)
# - timestamp: 8 bytes (double)
# - cb_count: 4 bytes (uint32)
# - reserved: 4 bytes
HEADER_SIZE = 24
HEADER_FORMAT = "!IIdII"

# CB entry size (72 bytes)
# - cb_id: 32 bytes (32s, UTF-8)
# - state: 4 bytes (uint32)
# - failure_count: 4 bytes (uint32)
# - success_count: 4 bytes (uint32)
# - last_failure_ts: 8 bytes (double)
# - last_success_ts: 8 bytes (double)
# - failure_threshold: 4 bytes (uint32)
# - recovery_timeout_ms: 8 bytes (double)
CB_ENTRY_SIZE = 72
CB_ENTRY_FORMAT = "!32sIIIddId"

# Maximum number of CBs
MAX_CB_COUNT = 1000

# Total memory size
TOTAL_SIZE = HEADER_SIZE + (CB_ENTRY_SIZE * MAX_CB_COUNT)

# Default file path
if os.name == "nt":
    DEFAULT_SHM_PATH = r"\\.\pipe\baldur_cb_state"
else:
    DEFAULT_SHM_PATH = "/dev/shm/baldur_cb_state"


@dataclass
class CBStateEntry(SerializableMixin):
    """CB state entry."""

    cb_id: str
    state: CBState
    failure_count: int
    success_count: int
    last_failure_ts: float
    last_success_ts: float
    failure_threshold: int
    recovery_timeout_ms: float

    @property
    def is_open(self) -> bool:
        """Whether the state is Open."""
        return self.state == CBState.OPEN

    @property
    def is_closed(self) -> bool:
        """Whether the state is Closed."""
        return self.state == CBState.CLOSED

    @property
    def is_half_open(self) -> bool:
        """Whether the state is Half-Open."""
        return self.state == CBState.HALF_OPEN

    @property
    def last_failure(self) -> datetime | None:
        """Time of the last failure."""
        if self.last_failure_ts <= 0:
            return None
        return datetime.fromtimestamp(self.last_failure_ts, tz=UTC)

    @property
    def last_success(self) -> datetime | None:
        """Time of the last success."""
        if self.last_success_ts <= 0:
            return None
        return datetime.fromtimestamp(self.last_success_ts, tz=UTC)

    def should_allow(self) -> bool:
        """
        Decide whether a request is allowed.

        Returns:
            Whether the request is allowed
        """
        if self.state == CBState.CLOSED:
            return True
        if self.state == CBState.HALF_OPEN:
            return True  # Half-Open allows trial requests
        # OPEN
        # Check whether the recovery timeout has elapsed
        if self.last_failure_ts <= 0:
            return True
        elapsed_ms = (time.time() - self.last_failure_ts) * 1000
        return elapsed_ms >= self.recovery_timeout_ms

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        return {
            "cb_id": self.cb_id,
            "state": self.state.name,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure": self.last_failure.isoformat()
            if self.last_failure
            else None,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout_ms": self.recovery_timeout_ms,
        }


class CBStateSnapshot:
    """
    CB state snapshot - backed by shared memory.

    Uses mmap to store CB state in shared memory so it can be read with very
    low latency (~10us).
    """

    def __init__(
        self,
        shm_path: str = DEFAULT_SHM_PATH,
        *,
        update_interval_ms: float = 100.0,
        is_writer: bool = False,
    ):
        """
        Initialize the snapshot.

        Args:
            shm_path: shared memory file path
            update_interval_ms: update interval (milliseconds)
            is_writer: whether to open in write mode
        """
        self.shm_path = shm_path
        self.update_interval_ms = update_interval_ms
        self.is_writer = is_writer

        self._mmap: mmap.mmap | None = None
        self._file: BufferedRandom | None = None
        self._running = False
        self._update_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Statistics
        self._read_count = 0
        self._write_count = 0
        self._last_update_ts = 0.0

        logger.debug(
            "cb_state_snapshot.initialized",
            shm_path=shm_path,
            is_writer=is_writer,
        )

    def start(self) -> None:
        """Start the snapshot."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        if self._running:
            return

        try:
            self._open_shm()
            self._running = True

            if self.is_writer:
                self._spawn_update_thread()
                assert (
                    self._update_thread is not None
                )  # _spawn_update_thread postcondition
                self._handle = DaemonWorkerHandle(
                    thread=self._update_thread,
                    tick_interval_seconds=self.update_interval_ms / 1000.0,
                    restart_callback=self._spawn_update_thread,
                )
                register_daemon_worker("CBStateSnapshotWriter", self._handle)

            logger.info(
                "cb_state_snapshot.started_mode",
                access_mode="writer" if self.is_writer else "reader",
            )

        except Exception as e:
            logger.exception(
                "cb_state_snapshot.start_failed",
                error=e,
            )
            raise

    def _spawn_update_thread(self) -> None:
        """Construct + start a fresh update thread (impl 489 D9)."""
        self._update_thread = threading.Thread(
            target=self._update_loop_with_crash_capture,
            daemon=True,
            name="CBStateSnapshotWriter",
        )
        self._update_thread.start()
        if self._handle is not None:
            self._handle.thread = self._update_thread

    def _update_loop_with_crash_capture(self) -> None:
        try:
            self._update_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop(self) -> None:
        """Stop the snapshot."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        if self._handle is not None:
            self._handle.is_stopping = True
        self._running = False

        if self._update_thread is not None:
            self._update_thread.join(timeout=1.0)
            unregister_daemon_worker("CBStateSnapshotWriter")
            if self._update_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="CBStateSnapshotWriter",
                    join_timeout_seconds=1.0,
                )
            self._update_thread = None

        self._close_shm()
        logger.info("cb_state_snapshot.stopped")

    def _open_shm(self) -> None:
        """Open shared memory."""
        if os.name == "nt":
            # Windows: use a regular file-backed mmap
            self._open_shm_windows()
        else:
            # Unix: /dev/shm or a file-backed mmap
            self._open_shm_unix()

    def _open_shm_windows(self) -> None:
        """Open shared memory on Windows."""
        # On Windows, use a file-backed mmap instead of named shared memory
        shm_path = self.shm_path.replace(r"\\.\pipe\\", "")
        shm_file = Path(os.environ.get("TEMP", "C:\\Temp")) / f"{shm_path}.shm"

        if self.is_writer:
            # Write mode: create the file
            shm_file.parent.mkdir(parents=True, exist_ok=True)
            fh = open(shm_file, "w+b")  # noqa: SIM115
            fh.write(b"\x00" * TOTAL_SIZE)
            fh.flush()
            self._file = fh
            self._mmap = mmap.mmap(
                fh.fileno(),
                TOTAL_SIZE,
                access=mmap.ACCESS_WRITE,
            )
            self._write_header()
        else:
            # Read mode: open the existing file
            if not shm_file.exists():
                raise FileNotFoundError(f"SHM file not found: {shm_file}")
            fh = open(shm_file, "r+b")  # noqa: SIM115
            self._file = fh
            self._mmap = mmap.mmap(
                fh.fileno(),
                TOTAL_SIZE,
                access=mmap.ACCESS_READ,
            )

    def _open_shm_unix(self) -> None:
        """Open shared memory on Unix."""
        shm_path = Path(self.shm_path)

        if self.is_writer:
            # Write mode: create the file
            shm_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(shm_path, "w+b")  # noqa: SIM115
            fh.write(b"\x00" * TOTAL_SIZE)
            fh.flush()
            self._file = fh
            self._mmap = mmap.mmap(
                fh.fileno(),
                TOTAL_SIZE,
                access=mmap.ACCESS_WRITE,
            )
            self._write_header()
        else:
            # Read mode: open the existing file
            if not shm_path.exists():
                raise FileNotFoundError(f"SHM file not found: {shm_path}")
            fh = open(shm_path, "r+b")  # noqa: SIM115
            self._file = fh
            self._mmap = mmap.mmap(
                fh.fileno(),
                TOTAL_SIZE,
                access=mmap.ACCESS_READ,
            )

    def _close_shm(self) -> None:
        """Close shared memory."""
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None

        if self._file is not None:
            self._file.close()
            self._file = None

    def _write_header(self) -> None:
        """Write the header."""
        if self._mmap is None:
            return

        header = struct.pack(
            HEADER_FORMAT,
            MAGIC_NUMBER,
            VERSION,
            time.time(),
            0,  # cb_count (initial value)
            0,  # reserved
        )
        self._mmap.seek(0)
        self._mmap.write(header)
        self._mmap.flush()

    def _read_header(self) -> tuple[int, int, float, int]:
        """
        Read the header.

        Returns:
            (magic, version, timestamp, cb_count) tuple
        """
        if self._mmap is None:
            raise RuntimeError("SHM not open")

        self._mmap.seek(0)
        header_data = self._mmap.read(HEADER_SIZE)
        magic, version, timestamp, cb_count, _ = struct.unpack(
            HEADER_FORMAT, header_data
        )

        return magic, version, timestamp, cb_count

    def get_state(self, cb_id: str) -> CBStateEntry | None:
        """
        Look up CB state.

        Args:
            cb_id: Circuit Breaker ID

        Returns:
            CB state entry, or None
        """
        if self._mmap is None:
            return None

        try:
            self._read_count += 1

            # Read the header
            magic, version, timestamp, cb_count = self._read_header()

            if magic != MAGIC_NUMBER:
                logger.warning("cb_state_snapshot.invalid_magic_number")
                return None

            # Search the CB entries
            cb_id_bytes = cb_id.encode("utf-8")[:32].ljust(32, b"\x00")

            for i in range(cb_count):
                offset = HEADER_SIZE + (i * CB_ENTRY_SIZE)
                self._mmap.seek(offset)
                entry_data = self._mmap.read(CB_ENTRY_SIZE)

                (
                    entry_cb_id,
                    state,
                    failure_count,
                    success_count,
                    last_failure_ts,
                    last_success_ts,
                    failure_threshold,
                    recovery_timeout_ms,
                ) = struct.unpack(CB_ENTRY_FORMAT, entry_data)

                if entry_cb_id == cb_id_bytes:
                    return CBStateEntry(
                        cb_id=entry_cb_id.rstrip(b"\x00").decode("utf-8"),
                        state=CBState(state),
                        failure_count=failure_count,
                        success_count=success_count,
                        last_failure_ts=last_failure_ts,
                        last_success_ts=last_success_ts,
                        failure_threshold=failure_threshold,
                        recovery_timeout_ms=recovery_timeout_ms,
                    )

            return None

        except Exception as e:
            logger.exception(
                "cb_state_snapshot.get_state_error",
                error=e,
            )
            return None

    def get_all_states(self) -> list[CBStateEntry]:
        """
        Look up every CB state.

        Returns:
            List of CB state entries
        """
        if self._mmap is None:
            return []

        try:
            # Read the header
            magic, version, timestamp, cb_count = self._read_header()

            if magic != MAGIC_NUMBER:
                logger.warning("cb_state_snapshot.invalid_magic_number")
                return []

            entries = []
            for i in range(cb_count):
                offset = HEADER_SIZE + (i * CB_ENTRY_SIZE)
                self._mmap.seek(offset)
                entry_data = self._mmap.read(CB_ENTRY_SIZE)

                (
                    entry_cb_id,
                    state,
                    failure_count,
                    success_count,
                    last_failure_ts,
                    last_success_ts,
                    failure_threshold,
                    recovery_timeout_ms,
                ) = struct.unpack(CB_ENTRY_FORMAT, entry_data)

                entries.append(
                    CBStateEntry(
                        cb_id=entry_cb_id.rstrip(b"\x00").decode("utf-8"),
                        state=CBState(state),
                        failure_count=failure_count,
                        success_count=success_count,
                        last_failure_ts=last_failure_ts,
                        last_success_ts=last_success_ts,
                        failure_threshold=failure_threshold,
                        recovery_timeout_ms=recovery_timeout_ms,
                    )
                )

            return entries

        except Exception as e:
            logger.exception(
                "cb_state_snapshot.get_all_states_error",
                error=e,
            )
            return []

    def update_state(self, entry: CBStateEntry) -> bool:
        """
        Update CB state.

        Args:
            entry: CB state entry

        Returns:
            Whether the update succeeded
        """
        if not self.is_writer or self._mmap is None:
            return False

        try:
            with self._lock:
                self._write_count += 1

                # Read the header
                magic, version, timestamp, cb_count = self._read_header()

                if magic != MAGIC_NUMBER:
                    self._write_header()
                    cb_count = 0

                # Find the existing entry or allocate a new slot
                cb_id_bytes = entry.cb_id.encode("utf-8")[:32].ljust(32, b"\x00")
                target_index = -1

                for i in range(cb_count):
                    offset = HEADER_SIZE + (i * CB_ENTRY_SIZE)
                    self._mmap.seek(offset)
                    existing_id = self._mmap.read(32)

                    if existing_id == cb_id_bytes:
                        target_index = i
                        break

                if target_index == -1:
                    # Append a new entry
                    if cb_count >= MAX_CB_COUNT:
                        logger.warning("cb_state_snapshot.max_cb_count_reached")
                        return False
                    target_index = cb_count
                    cb_count += 1

                # Write the entry
                offset = HEADER_SIZE + (target_index * CB_ENTRY_SIZE)
                entry_data = struct.pack(
                    CB_ENTRY_FORMAT,
                    cb_id_bytes,
                    entry.state.value,
                    entry.failure_count,
                    entry.success_count,
                    entry.last_failure_ts,
                    entry.last_success_ts,
                    entry.failure_threshold,
                    entry.recovery_timeout_ms,
                )
                self._mmap.seek(offset)
                self._mmap.write(entry_data)

                # Update the header
                header = struct.pack(
                    HEADER_FORMAT,
                    MAGIC_NUMBER,
                    VERSION,
                    time.time(),
                    cb_count,
                    0,
                )
                self._mmap.seek(0)
                self._mmap.write(header)
                self._mmap.flush()

                self._last_update_ts = time.time()
                return True

        except Exception as e:
            logger.exception(
                "cb_state_snapshot.update_state_error",
                error=e,
            )
            return False

    def _update_loop(self) -> None:
        """Periodic update loop."""
        while self._running:
            iter_start = time.monotonic()
            try:
                self._sync_from_registry()
            except Exception as e:
                logger.exception(
                    "cb_state_snapshot.update_loop_error",
                    error=e,
                )

            if self._handle is not None:
                self._handle.observe_iteration(time.monotonic() - iter_start)
                self._handle.heartbeat()

            time.sleep(self.update_interval_ms / 1000.0)

    def _sync_from_registry(self) -> None:
        """Sync state from the CB service."""
        try:
            from baldur.services import get_circuit_breaker_service

            cb_service = get_circuit_breaker_service()
            if cb_service is None:
                return

            # Pull every service state from the CB service and sync it
            # Use get_all_states() when present, otherwise skip
            get_all_states = getattr(cb_service, "get_all_states", None)
            if get_all_states is None:
                return

            states = get_all_states()
            for state_info in states:
                cb_id = state_info.get("service_name", "")
                if not cb_id:
                    continue

                state_str = state_info.get("state", "closed").upper()
                state_enum = (
                    CBState[state_str]
                    if state_str in CBState.__members__
                    else CBState.CLOSED
                )

                entry = CBStateEntry(
                    cb_id=cb_id,
                    state=state_enum,
                    failure_count=state_info.get("failure_count", 0),
                    success_count=state_info.get("success_count", 0),
                    last_failure_ts=state_info.get("last_failure_ts", 0.0),
                    last_success_ts=state_info.get("last_success_ts", 0.0),
                    failure_threshold=state_info.get("failure_threshold", 5),
                    recovery_timeout_ms=int(
                        state_info.get("recovery_timeout", 30) * 1000
                    ),
                )
                self.update_state(entry)

        except ImportError:
            # Service module not present
            pass
        except Exception as e:
            logger.debug(
                "cb_state_snapshot.sync_service_error",
                error=e,
            )

    def get_stats(self) -> dict[str, Any]:
        """
        Return statistics.

        Returns:
            Statistics dictionary
        """
        return {
            "read_count": self._read_count,
            "write_count": self._write_count,
            "last_update_ts": self._last_update_ts,
            "is_running": self._running,
            "is_writer": self.is_writer,
        }


# =============================================================================
# Singleton instance
# =============================================================================

from baldur.utils.singleton import CLEANUP_STOP, make_singleton_factory

get_cb_state_snapshot, configure_cb_state_snapshot, reset_cb_state_snapshot = (
    make_singleton_factory(
        "cb_state_snapshot",
        CBStateSnapshot,
        cleanup_fn=CLEANUP_STOP,
    )
)
