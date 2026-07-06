"""
WAL 레코드 직렬화.

기존 코드에서 3회 반복되던 직렬화/역직렬화 로직을 통합합니다.
- json.dumps + CRC32 + struct.pack 패턴
- fsync + rotation 패턴
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import Any

from baldur.utils.serialization import fast_dumps


def compute_checksum(data: bytes) -> str:
    """CRC32 체크섬 계산."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return f"{crc:08x}"


def verify_checksum(data: bytes, expected: str) -> bool:
    """CRC32 체크섬 검증."""
    computed = compute_checksum(data)
    return computed.lower() == expected.lower()


def serialize_entry(entry: dict[str, Any]) -> tuple[bytes, str]:
    """
    WAL 엔트리를 바이트 레코드로 직렬화.

    기존 _direct_write, _flush_buffer, batch_write_entries에서
    3회 반복되던 패턴을 통합합니다.

    Args:
        entry: WAL 엔트리 딕셔너리 (seq, ts, data)

    Returns:
        (record_bytes, checksum) 튜플
    """
    entry_bytes = fast_dumps(entry)
    checksum = compute_checksum(entry_bytes)
    record = (
        struct.pack(">I", len(entry_bytes)) + checksum.encode("ascii") + entry_bytes
    )
    return record, checksum


def sync_and_maybe_rotate(handle, config, rotate_fn) -> None:
    """
    파일 동기화 및 필요 시 로테이션.

    기존 _direct_write, _flush_buffer, batch_write_entries에서
    3회 반복되던 패턴을 통합합니다.

    Args:
        handle: 파일 핸들
        config: WALConfig
        rotate_fn: 로테이션 함수 콜백
    """
    if config.sync_on_write:
        handle.flush()
        os.fsync(handle.fileno())

    if handle.tell() > config.max_file_size_bytes:
        rotate_fn()
