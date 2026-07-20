"""
mmap-based disk-persistent buffer.

Standard library only (no external dependencies).
Used as an alternative where LMDB is not installed.

Limitations:
- Fixed-size file
- No deletion (circular buffer)
- Simple sequential access

Usage:
    buffer = MmapBuffer("/var/lib/baldur/mmap_buffer.dat")
    buffer.put({"event": "test"})

    for entry in buffer.iter_entries():
        print(entry)

    buffer.close()
"""

from __future__ import annotations

import mmap
import os
import struct
import sys
import threading
from pathlib import Path
from typing import Any

import structlog

from baldur.core.exceptions import AuditError
from baldur.utils.serialization import fast_dumps, fast_loads

logger = structlog.get_logger()


class MmapBufferError(AuditError):
    """Mmap Buffer error."""

    pass


class MmapBuffer:
    """
    Simple mmap-based persistent buffer.

    File layout:
    - Header (16 bytes): magic(4) + version(2) + entry_count(4) + write_pos(4) + reserved(2)
    - Entries: [length(4) + data(variable)] ...

    Limitations:
    - Fixed-size file
    - No deletion (overwritten circular-buffer style)
    - Simple sequential access
    """

    MAGIC = b"MMBF"
    VERSION = 1
    HEADER_SIZE = 16
    DEFAULT_SIZE_MB = 100

    def __init__(
        self,
        file_path: str | Path | None = None,
        size_mb: int = DEFAULT_SIZE_MB,
    ):
        """
        Initialize MmapBuffer.

        Args:
            file_path: Buffer file path
            size_mb: Buffer size (MB)
        """
        if file_path is None:
            if sys.platform == "win32":
                default_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "baldur")
            else:
                default_dir = "/var/lib/baldur"
            file_path = os.path.join(default_dir, "mmap_buffer.dat")

        self._file_path = Path(file_path)
        self._size_bytes = size_mb * 1024 * 1024
        self._lock = threading.RLock()
        self._mmap: mmap.mmap | None = None
        self._file: Any = None
        self._total_added: int = 0
        self._total_dropped: int = 0

        self._init_storage()

    def _init_storage(self) -> None:
        """Initialize storage."""
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        # Create or open the file
        if not self._file_path.exists():
            self._create_new_file()
        else:
            self._open_existing_file()

    def _create_new_file(self) -> None:
        """Create a new file."""
        with open(self._file_path, "wb") as f:
            # Write the header
            header = struct.pack(
                ">4sHIIxx",  # magic(4) + version(2) + count(4) + pos(4) + reserved(2)
                self.MAGIC,
                self.VERSION,
                0,  # entry_count
                self.HEADER_SIZE,  # write_pos
            )
            f.write(header)
            # Zero-fill the remaining space
            f.write(b"\x00" * (self._size_bytes - self.HEADER_SIZE))

        self._open_existing_file()
        logger.info(
            "mmap_buffer.created_new_file",
            file_path=self._file_path,
        )

    def _open_existing_file(self) -> None:
        """Open an existing file."""
        self._file = open(self._file_path, "r+b")  # noqa: SIM115
        self._mmap = mmap.mmap(self._file.fileno(), 0)

        # Validate the header
        magic = self._mmap[:4]
        if magic != self.MAGIC:
            raise MmapBufferError(f"Invalid magic: {magic!r}")

        logger.info(
            "mmap_buffer.opened",
            file_path=self._file_path,
        )

    def _read_header(self) -> tuple[int, int]:
        """Read the header: (entry_count, write_pos)."""
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")
        data = struct.unpack(">4sHIIxx", self._mmap[: self.HEADER_SIZE])
        return data[2], data[3]

    def _write_header(self, entry_count: int, write_pos: int) -> None:
        """Write the header."""
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")
        header = struct.pack(
            ">4sHIIxx",
            self.MAGIC,
            self.VERSION,
            entry_count,
            write_pos,
        )
        self._mmap[: self.HEADER_SIZE] = header
        self._mmap.flush()

    def put(self, entry: dict[str, Any]) -> bool:
        """
        Store an entry.

        Args:
            entry: Event data

        Returns:
            Whether the store succeeded
        """
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")

        with self._lock:
            entry_count, write_pos = self._read_header()

            # JSON serialization
            data = fast_dumps(entry, default=str)
            record_size = 4 + len(data)  # length(4) + data

            # Check space (circular buffer)
            if write_pos + record_size > self._size_bytes:
                logger.warning("mmap_buffer.buffer_full_wrapping_around")
                write_pos = self.HEADER_SIZE
                entry_count = 0

            # Write the record
            self._mmap[write_pos : write_pos + 4] = struct.pack(">I", len(data))
            self._mmap[write_pos + 4 : write_pos + record_size] = data

            # Update the header
            self._write_header(entry_count + 1, write_pos + record_size)
            self._total_added += 1

            return True

    def iter_entries(self) -> list[dict[str, Any]]:
        """
        Read all entries.

        Returns:
            List of entries
        """
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")

        entries = []
        with self._lock:
            entry_count, write_pos = self._read_header()
            pos = self.HEADER_SIZE

            while pos < write_pos:
                length_bytes = self._mmap[pos : pos + 4]
                if len(length_bytes) < 4:
                    break

                length = struct.unpack(">I", length_bytes)[0]
                if length == 0:
                    break

                data = self._mmap[pos + 4 : pos + 4 + length]
                try:
                    entry = fast_loads(data)
                    entries.append(entry)
                except ValueError:
                    logger.warning(
                        "mmap_buffer.invalid_json_pos",
                        pos=pos,
                    )

                pos += 4 + length

        return entries

    def count(self) -> int:
        """Current entry count."""
        with self._lock:
            entry_count, _ = self._read_header()
            return entry_count

    def clear(self) -> int:
        """Reset the buffer. Returns number of cleared entries."""
        with self._lock:
            entry_count, _ = self._read_header()
            self._write_header(0, self.HEADER_SIZE)
            return entry_count

    def get_stats(self) -> dict[str, Any]:
        """Return statistics."""
        with self._lock:
            entry_count, write_pos = self._read_header()
            return {
                # Common keys (AuditBufferProtocol)
                "count": entry_count,
                "total_added": self._total_added,
                "total_dropped": self._total_dropped,
                "capacity": None,
                "usage_percent": None,
                # Implementation-specific keys
                "entry_count": entry_count,
                "write_pos": write_pos,
                "file_size": self._size_bytes,
                "used_bytes": write_pos,
                "free_bytes": self._size_bytes - write_pos,
            }

    def close(self) -> None:
        """Close the buffer."""
        if self._mmap:
            self._mmap.close()
            self._mmap = None
        if self._file:
            self._file.close()
            self._file = None
        logger.info("mmap_buffer.closed")

    def __enter__(self) -> MmapBuffer:
        """Context manager entry."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager exit."""
        self.close()


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory  # noqa: E402

get_mmap_buffer, configure_mmap_buffer, reset_mmap_buffer = make_singleton_factory(
    "mmap_buffer", MmapBuffer, cleanup_fn=CLEANUP_CLOSE
)
