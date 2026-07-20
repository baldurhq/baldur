"""
WAL record serialization.

Unifies the serialization/deserialization logic that was repeated three
times across the existing code.
- json.dumps + CRC32 + struct.pack pattern
- fsync + rotation pattern
"""

from __future__ import annotations

import os
import struct
import zlib
from typing import Any

from baldur.utils.serialization import fast_dumps


def compute_checksum(data: bytes) -> str:
    """Compute a CRC32 checksum."""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    return f"{crc:08x}"


def verify_checksum(data: bytes, expected: str) -> bool:
    """Verify a CRC32 checksum."""
    computed = compute_checksum(data)
    return computed.lower() == expected.lower()


def serialize_entry(entry: dict[str, Any]) -> tuple[bytes, str]:
    """
    Serialize a WAL entry into a byte record.

    Unifies the pattern that was repeated three times across
    _direct_write, _flush_buffer, and batch_write_entries.

    Args:
        entry: WAL entry dictionary (seq, ts, data)

    Returns:
        (record_bytes, checksum) tuple
    """
    entry_bytes = fast_dumps(entry)
    checksum = compute_checksum(entry_bytes)
    record = (
        struct.pack(">I", len(entry_bytes)) + checksum.encode("ascii") + entry_bytes
    )
    return record, checksum


def sync_and_maybe_rotate(handle, config, rotate_fn) -> None:
    """
    Sync the file and rotate it if needed.

    Unifies the pattern that was repeated three times across
    _direct_write, _flush_buffer, and batch_write_entries.

    Args:
        handle: File handle
        config: WALConfig
        rotate_fn: Rotation callback
    """
    if config.sync_on_write:
        handle.flush()
        os.fsync(handle.fileno())

    if handle.tell() > config.max_file_size_bytes:
        rotate_fn()
