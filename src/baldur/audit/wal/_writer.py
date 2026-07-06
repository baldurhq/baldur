"""
WAL 쓰기 모듈.

직접 기록, 버퍼 기록, 배치 기록을 통합된 직렬화 함수로 처리합니다.
"""

from __future__ import annotations

import threading
import time
from typing import IO, TYPE_CHECKING, Any

import structlog

from baldur.audit.wal._serialization import serialize_entry, sync_and_maybe_rotate

if TYPE_CHECKING:
    from baldur.audit.wal._models import WALConfig, WALState

logger = structlog.get_logger()


class WALWriterMixin:
    """WAL 쓰기 관련 메서드."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided by WriteAheadLog
        # (the assembled class in baldur.audit.wal.__init__).
        _config: WALConfig
        _state: WALState
        _lock: threading.RLock
        _sequence: int
        _current_handle: IO[bytes] | None
        _total_entries: int
        _group_buffer: list[dict[str, Any]]
        _group_commit_flushes: int
        _last_write_time: float | None

        def _ensure_file_open(self) -> None: ...
        def _rotate_file(self) -> None: ...
        def _handle_disk_full(self) -> None: ...

    def write(self, data: dict[str, Any]) -> int:
        """
        WAL에 기록.

        Args:
            data: 기록할 데이터 (딕셔너리)

        Returns:
            시퀀스 번호
        """
        if self._config.group_commit_enabled:
            return self._buffered_write(data)
        return self._direct_write(data)

    def _direct_write(self, data: dict[str, Any]) -> int:
        """직접 기록 (Disk Full Fail-Open 지원)."""
        from baldur.audit.wal._models import WALError, WALState

        with self._lock:
            # Fail-Open 모드면 WAL 기록 스킵
            if self._state == WALState.DISK_FULL_FAILOPEN:
                logger.warning("wal.disk_full_fail_open")
                return -1

            if self._state == WALState.CLOSED:
                raise WALError("WAL is closed")

            self._sequence += 1
            current_seq = self._sequence

            entry = {
                "seq": current_seq,
                "ts": time.time(),
                "data": data,
            }
            record, _checksum = serialize_entry(entry)

            try:
                self._ensure_file_open()

                if self._current_handle:
                    self._current_handle.write(record)
                    sync_and_maybe_rotate(
                        self._current_handle, self._config, self._rotate_file
                    )
                    self._total_entries += 1
                    self._last_write_time = time.time()

            except OSError as e:
                import errno

                if e.errno == errno.ENOSPC:
                    self._handle_disk_full()
                    if self._config.fail_open_on_disk_full:
                        return -1
                    raise
                raise

            # Drift Detection 메트릭 기록
            try:
                from baldur.metrics.drift_metrics import (
                    record_wal_entry_written,
                    update_wal_last_sequence,
                )

                record_wal_entry_written()
                update_wal_last_sequence(current_seq)
            except ImportError:
                pass

            return current_seq

    def _buffered_write(self, data: dict[str, Any]) -> int:
        """
        버퍼링된 기록 (Group Commit).

        여러 엔트리를 모아서 한 번에 fsync 수행.
        """
        from baldur.audit.wal._models import WALError, WALState

        with self._lock:
            if self._state == WALState.CLOSED:
                raise WALError("WAL is closed")

            self._sequence += 1
            current_seq = self._sequence

            buffered_entry = {
                "seq": current_seq,
                "ts": time.time(),
                "data": data,
            }
            self._group_buffer.append(buffered_entry)

            should_flush = (
                len(self._group_buffer) >= self._config.group_commit_max_entries
                or self._time_since_last_flush_ms()
                >= self._config.group_commit_max_wait_ms
            )

            if should_flush:
                self._flush_buffer()

            return current_seq

    def _time_since_last_flush_ms(self) -> float:
        """마지막 플러시 이후 경과 시간 (ms)."""
        return (time.time() - self._last_flush_time) * 1000

    def _flush_buffer(self) -> None:
        """버퍼의 모든 엔트리를 한 번에 기록."""
        if not self._group_buffer:
            return

        self._ensure_file_open()

        if self._current_handle:
            for entry in self._group_buffer:
                record, _checksum = serialize_entry(entry)
                self._current_handle.write(record)
                self._total_entries += 1

            sync_and_maybe_rotate(self._current_handle, self._config, self._rotate_file)
            self._group_commit_flushes += 1
            self._last_write_time = time.time()

        self._group_buffer.clear()
        self._last_flush_time = time.time()

    def flush_group_commit(self) -> None:
        """Group Commit 버퍼 강제 플러시."""
        with self._lock:
            if self._config.group_commit_enabled:
                self._flush_buffer()

    def batch_write_entries(self, entries: list[dict[str, Any]]) -> list[int]:
        """
        여러 엔트리를 한 번에 기록 (단일 fsync).

        Args:
            entries: 기록할 데이터 딕셔너리 목록

        Returns:
            각 엔트리의 시퀀스 번호 목록
        """
        from baldur.audit.wal._models import WALError, WALState

        if not entries:
            return []

        with self._lock:
            if self._state == WALState.CLOSED:
                raise WALError("WAL is closed")

            sequences: list[int] = []
            records: list[bytes] = []

            for data in entries:
                self._sequence += 1
                current_seq = self._sequence
                sequences.append(current_seq)

                entry = {
                    "seq": current_seq,
                    "ts": time.time(),
                    "data": data,
                }
                record, _checksum = serialize_entry(entry)
                records.append(record)

            # 파일에 일괄 기록
            self._ensure_file_open()

            if self._current_handle:
                for record in records:
                    self._current_handle.write(record)
                    self._total_entries += 1

                sync_and_maybe_rotate(
                    self._current_handle, self._config, self._rotate_file
                )
                self._last_write_time = time.time()

            # Drift Detection 메트릭 기록
            try:
                from baldur.metrics.drift_metrics import (
                    record_wal_entry_written,
                    update_wal_last_sequence,
                )

                for _ in sequences:
                    record_wal_entry_written()
                update_wal_last_sequence(self._sequence)
            except ImportError:
                pass

            return sequences
