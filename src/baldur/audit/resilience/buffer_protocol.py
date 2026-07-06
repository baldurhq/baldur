"""Audit Buffer protocols (ISP-based interface segregation)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AuditBufferProtocol(Protocol):
    """Core Buffer interface — all Buffer implementations must satisfy."""

    def add(self, entry: dict[str, Any]) -> bool:
        """Add entry. False = storage failure (capacity exceeded or disk error)."""
        ...

    def count(self) -> int:
        """Current entry count."""
        ...

    def get_stats(self) -> dict[str, Any]:
        """Buffer statistics."""
        ...


@runtime_checkable
class ClearableBuffer(Protocol):
    """Optional clear capability — only for implementations supporting logical reset."""

    def clear(self) -> int:
        """Logical reset. Returns number of deleted items."""
        ...
