"""
Batch Flush Writer (n×fsync → 1×fsync).

Provides batched file writing with reduced fsync overhead.
"""

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from baldur.utils.serialization import fast_dumps_str

logger = structlog.get_logger()


@dataclass
class BatchFlushConfig:
    """Configuration for batch flush writer."""

    batch_size: int = 100
    flush_interval_seconds: float = 10.0
    sync_on_flush: bool = True


class BatchFlushWriter:
    """
    Batched file writer with reduced fsync overhead.

    Problem:
        Each fsync() call takes 1-10ms depending on disk.
        Writing 1000 entries/sec = 1000-10000ms of blocking per second.

    Solution:
        Buffer entries and flush in batches with single fsync.
        100 entries per batch = 99% reduction in fsync calls.

    Pattern source:
        audit/config.py#L69-73 (batch_size, batch_flush_interval_seconds)

    Usage:
        writer = BatchFlushWriter(path, BatchFlushConfig(batch_size=100))
        writer.write(entry)  # Buffered
        # Auto-flush when batch_size reached or interval elapsed
    """

    def __init__(
        self,
        file_path: Path,
        config: BatchFlushConfig | None = None,
    ):
        """
        Initialize batch flush writer.

        Args:
            file_path: Path to output file
            config: Batch configuration
        """
        self._file_path = Path(file_path)
        self._config = config or BatchFlushConfig()
        self._buffer: list[str] = []
        self._lock = threading.RLock()
        self._last_flush = time.monotonic()
        self._file_handle: Any = None  # lazy-opened TextIOWrapper
        self._entries_written = 0
        self._flushes_performed = 0

    def write(self, entry: dict[str, Any]) -> bool:
        """
        Write entry to buffer, flush if needed.

        Args:
            entry: Dictionary to write as JSON line

        Returns:
            True if successfully buffered/written
        """
        with self._lock:
            try:
                line = fast_dumps_str(entry, default=str)
                self._buffer.append(line)

                # Check flush conditions
                should_flush = (
                    len(self._buffer) >= self._config.batch_size
                    or time.monotonic() - self._last_flush
                    >= self._config.flush_interval_seconds
                )

                if should_flush:
                    return self._flush()

                return True

            except Exception as e:
                logger.exception(
                    "batch_flush_writer.write_failed",
                    error=e,
                )
                return False

    def _flush(self) -> bool:
        """
        Flush buffer to file with single fsync.

        Returns:
            True if flush successful
        """
        if not self._buffer:
            return True

        try:
            self._ensure_file_open()

            # Capture count before clearing
            flushed_count = len(self._buffer)

            # Write all buffered entries
            content = "\n".join(self._buffer) + "\n"
            self._file_handle.write(content)

            # Always flush to ensure data is written to OS buffer
            self._file_handle.flush()

            # fsync for durability (optional, expensive)
            if self._config.sync_on_flush:
                os.fsync(self._file_handle.fileno())

            self._entries_written += flushed_count
            self._flushes_performed += 1
            self._buffer.clear()
            self._last_flush = time.monotonic()

            logger.debug(
                "batch_flush_writer.flushed_entries_total",
                flushed_count=flushed_count,
                entries_written=self._entries_written,
            )
            return True

        except Exception as e:
            logger.exception(
                "batch_flush_writer.flush_failed",
                error=e,
            )
            return False

    def _ensure_file_open(self) -> None:
        """Ensure file is open for writing."""
        if self._file_handle is None:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(self._file_path, "a", encoding="utf-8")  # noqa: SIM115

    def force_flush(self) -> bool:
        """Force immediate flush of buffer."""
        with self._lock:
            return self._flush()

    def close(self) -> None:
        """Flush and close file."""
        with self._lock:
            self._flush()
            if self._file_handle:
                self._file_handle.close()
                self._file_handle = None

    def get_stats(self) -> dict[str, Any]:
        """Get writer statistics."""
        return {
            "entries_written": self._entries_written,
            "flushes_performed": self._flushes_performed,
            "buffer_size": len(self._buffer),
            "avg_entries_per_flush": (
                self._entries_written / self._flushes_performed
                if self._flushes_performed > 0
                else 0
            ),
        }
