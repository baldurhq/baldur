"""Disk buffer domain models and exceptions.

Defines BufferEntry, BufferState, and DiskBufferError used across
the disk-persistent buffer sub-modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from baldur.core.exceptions import BaldurError

__all__ = [
    "BufferEntry",
    "BufferState",
    "DiskBufferError",
]


@dataclass
class BufferEntry:
    """Buffer entry data class."""

    key: bytes
    """Unique key (timestamp + sequence)."""

    data: dict[str, Any]
    """Event data."""

    timestamp: float
    """Stored timestamp (Unix timestamp)."""

    checksum: int
    """CRC32 checksum."""

    @property
    def sequence(self) -> int:
        """Extract sequence number from key."""
        # Key format: b"{timestamp:.6f}:{sequence:08d}"
        try:
            return int(self.key.decode().split(":")[1])
        except (ValueError, IndexError):
            return 0


class DiskBufferError(BaldurError):
    """Disk Buffer error."""

    pass


class BufferState(str, Enum):
    """Buffer state."""

    UNINITIALIZED = "uninitialized"
    ACTIVE = "active"
    DISK_FULL_FAILOPEN = "disk_full_failopen"
    CORRUPTED = "corrupted"
    CLOSED = "closed"
