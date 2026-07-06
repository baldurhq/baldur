"""
mmap 기반 Disk-Persistent Buffer.

표준 라이브러리만 사용 (외부 의존성 없음).
LMDB가 설치되지 않은 환경에서 대안으로 사용합니다.

제한사항:
- 고정 크기 파일
- 삭제 없음 (순환 버퍼 방식)
- 단순 순차 접근

사용법:
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
    mmap 기반 간단한 영속 버퍼.

    파일 구조:
    - Header (16 bytes): magic(4) + version(2) + entry_count(4) + write_pos(4) + reserved(2)
    - Entries: [length(4) + data(variable)] ...

    제한사항:
    - 고정 크기 파일
    - 삭제 없음 (순환 버퍼 방식으로 덮어쓰기)
    - 단순 순차 접근
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
        MmapBuffer 초기화.

        Args:
            file_path: 버퍼 파일 경로
            size_mb: 버퍼 크기 (MB)
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
        """스토리지 초기화."""
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

        # 파일 생성 또는 열기
        if not self._file_path.exists():
            self._create_new_file()
        else:
            self._open_existing_file()

    def _create_new_file(self) -> None:
        """새 파일 생성."""
        with open(self._file_path, "wb") as f:
            # Header 쓰기
            header = struct.pack(
                ">4sHIIxx",  # magic(4) + version(2) + count(4) + pos(4) + reserved(2)
                self.MAGIC,
                self.VERSION,
                0,  # entry_count
                self.HEADER_SIZE,  # write_pos
            )
            f.write(header)
            # 나머지 공간 0으로 채우기
            f.write(b"\x00" * (self._size_bytes - self.HEADER_SIZE))

        self._open_existing_file()
        logger.info(
            "mmap_buffer.created_new_file",
            file_path=self._file_path,
        )

    def _open_existing_file(self) -> None:
        """기존 파일 열기."""
        self._file = open(self._file_path, "r+b")  # noqa: SIM115
        self._mmap = mmap.mmap(self._file.fileno(), 0)

        # Header 검증
        magic = self._mmap[:4]
        if magic != self.MAGIC:
            raise MmapBufferError(f"Invalid magic: {magic!r}")

        logger.info(
            "mmap_buffer.opened",
            file_path=self._file_path,
        )

    def _read_header(self) -> tuple[int, int]:
        """헤더 읽기: (entry_count, write_pos)."""
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")
        data = struct.unpack(">4sHIIxx", self._mmap[: self.HEADER_SIZE])
        return data[2], data[3]

    def _write_header(self, entry_count: int, write_pos: int) -> None:
        """헤더 쓰기."""
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
        엔트리 저장.

        Args:
            entry: 이벤트 데이터

        Returns:
            저장 성공 여부
        """
        if self._mmap is None:
            raise MmapBufferError("Buffer not initialized")

        with self._lock:
            entry_count, write_pos = self._read_header()

            # JSON 직렬화
            data = fast_dumps(entry, default=str)
            record_size = 4 + len(data)  # length(4) + data

            # 공간 확인 (순환 버퍼 방식)
            if write_pos + record_size > self._size_bytes:
                logger.warning("mmap_buffer.buffer_full_wrapping_around")
                write_pos = self.HEADER_SIZE
                entry_count = 0

            # 레코드 쓰기
            self._mmap[write_pos : write_pos + 4] = struct.pack(">I", len(data))
            self._mmap[write_pos + 4 : write_pos + record_size] = data

            # 헤더 업데이트
            self._write_header(entry_count + 1, write_pos + record_size)
            self._total_added += 1

            return True

    def iter_entries(self) -> list[dict[str, Any]]:
        """
        모든 엔트리 읽기.

        Returns:
            엔트리 목록
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
        """현재 엔트리 수."""
        with self._lock:
            entry_count, _ = self._read_header()
            return entry_count

    def clear(self) -> int:
        """버퍼 초기화. Returns number of cleared entries."""
        with self._lock:
            entry_count, _ = self._read_header()
            self._write_header(0, self.HEADER_SIZE)
            return entry_count

    def get_stats(self) -> dict[str, Any]:
        """통계 반환."""
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
        """버퍼 종료."""
        if self._mmap:
            self._mmap.close()
            self._mmap = None
        if self._file:
            self._file.close()
            self._file = None
        logger.info("mmap_buffer.closed")

    def __enter__(self) -> MmapBuffer:
        """Context manager 진입."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager 종료."""
        self.close()


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

from baldur.utils.singleton import CLEANUP_CLOSE, make_singleton_factory  # noqa: E402

get_mmap_buffer, configure_mmap_buffer, reset_mmap_buffer = make_singleton_factory(
    "mmap_buffer", MmapBuffer, cleanup_fn=CLEANUP_CLOSE
)
