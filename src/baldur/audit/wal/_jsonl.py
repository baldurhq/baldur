"""
JSONL WAL shared utilities.

Unifies the common write/read patterns of the four JSONL WAL
implementations (HashChainWAL, HashChainWALRecovery, WALRecoveryMixin).

- JSONLWriter: thread-safe, fsync policy, size-based rotation
- JSONLReader: logged skip + metric for corrupt lines, commit marker parsing
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any, Literal, TypedDict

import structlog

from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()


def _record_corrupted_line(file_path: Path, line_no: int) -> None:
    """Log + meter a skipped corrupt JSONL line (invalid JSON or non-object)."""
    logger.warning(
        "jsonl_reader.corrupted_line_skipped",
        file=str(file_path),
        line_no=line_no,
    )
    try:
        from baldur.metrics.drift_metrics import record_wal_corrupted_line

        record_wal_corrupted_line()
    except ImportError:
        pass


class CommitMarker(TypedDict):
    _marker: Literal["COMMIT"]
    wal_sequence: int
    timestamp: str


class JSONLWriter:
    """JSONL WAL write utility (thread-safe, fsync policy, size rotation)."""

    _serialize = staticmethod(fast_dumps_str)

    def __init__(
        self,
        file_path: Path,
        fsync: bool = True,
        max_size_bytes: int | None = None,
    ):
        self._path = Path(file_path)
        self._handle: IO | None = None
        self._lock = threading.RLock()
        self._fsync = fsync
        self._max_size = max_size_bytes
        self._current_size: int = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def ensure_open(self) -> None:
        """Ensure the WAL file is open, creating it if needed."""
        if self._handle is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
            try:
                self._current_size = self._path.stat().st_size
            except OSError:
                self._current_size = 0

    def append(self, entry: dict[str, Any]) -> None:
        """Append a JSONL line (write + flush + conditional fsync + rotate check)."""
        with self._lock:
            self.ensure_open()
            assert self._handle is not None  # ensure_open() invariant
            line = fast_dumps_str(entry, default=str) + "\n"
            self._handle.write(line)
            self._current_size += len(line.encode("utf-8"))
            if self._fsync:
                self._handle.flush()
                os.fsync(self._handle.fileno())
            self._maybe_rotate()

    def close(self) -> None:
        """Close the WAL file."""
        with self._lock:
            if self._handle:
                try:
                    self._handle.flush()
                    self._handle.close()
                except Exception:
                    pass
                finally:
                    self._handle = None

    def _maybe_rotate(self) -> None:
        """Rotate the file when the size is exceeded (called under RLock)."""
        if self._max_size and self._current_size >= self._max_size:
            if self._handle:
                self._handle.close()
            rotated = self._path.with_suffix(f".{time.time_ns()}.jsonl")
            self._path.rename(rotated)
            self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
            self._current_size = 0


class JSONLReader:
    """JSONL WAL read utility (logged skip + metric for corrupt lines)."""

    @staticmethod
    def iter_entries(file_path: Path) -> Iterator[dict]:
        """Iterate JSONL entries; corrupt lines are skipped (warn log + metric)."""
        if not file_path.exists():
            return

        # errors="replace": a non-UTF-8 byte in a corrupt WAL file degrades to
        # U+FFFD (the line then fails JSON parse and is skipped) rather than
        # raising UnicodeDecodeError mid-iteration and aborting the whole read.
        with open(file_path, encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = fast_loads(line)
                except ValueError:
                    _record_corrupted_line(file_path, line_no)
                    continue
                # A valid-JSON but non-object line (scalar/array) violates the
                # WAL record contract — treat it as corrupt rather than yielding
                # a value that downstream `.get()` consumers would choke on.
                if not isinstance(entry, dict):
                    _record_corrupted_line(file_path, line_no)
                    continue
                yield entry

    @staticmethod
    def parse_with_committed_filter(
        file_path: Path,
        commit_field: str = "status",
        commit_value: str = "COMMITTED",
    ) -> tuple[list[dict], set[int]]:
        """Parse a JSONL file and return (all entries, committed sequence set)."""
        entries: list[dict] = []
        committed_seqs: set[int] = set()

        for entry in JSONLReader.iter_entries(file_path):
            seq = entry.get("seq")
            if seq is None:
                seq = entry.get("wal_sequence")
            if seq is not None:
                status = entry.get(commit_field, "")
                if status == commit_value:
                    committed_seqs.add(seq)
                elif entry.get("_marker") == "COMMIT":
                    committed_seqs.add(entry.get("wal_sequence", seq))
                else:
                    entries.append(entry)

        return entries, committed_seqs


__all__ = ["CommitMarker", "JSONLReader", "JSONLWriter"]
