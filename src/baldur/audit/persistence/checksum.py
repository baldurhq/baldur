"""CRC32 checksum utility for disk buffer integrity verification."""

from __future__ import annotations

import zlib

__all__ = [
    "compute_checksum",
]


def compute_checksum(data: bytes) -> int:
    """CRC32 checksum computation."""
    return zlib.crc32(data) & 0xFFFFFFFF
