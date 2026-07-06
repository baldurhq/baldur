"""Unit tests for baldur.audit.resilience.buffer_protocol."""

from typing import Any

from baldur.audit.resilience.buffer_protocol import (
    AuditBufferProtocol,
    ClearableBuffer,
)


class _ConcreteBuffer:
    """Concrete implementation satisfying AuditBufferProtocol."""

    def __init__(self):
        self._entries = []

    def add(self, entry: dict[str, Any]) -> bool:
        self._entries.append(entry)
        return True

    def count(self) -> int:
        return len(self._entries)

    def get_stats(self) -> dict[str, Any]:
        return {"count": len(self._entries)}


class _ClearableConcreteBuffer(_ConcreteBuffer):
    """Concrete implementation satisfying both protocols."""

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        return n


class _NotABuffer:
    """Does not satisfy AuditBufferProtocol."""

    pass


class TestAuditBufferProtocolContract:
    """AuditBufferProtocol design contract verification."""

    def test_protocol_is_runtime_checkable(self):
        """AuditBufferProtocol is @runtime_checkable."""
        buf = _ConcreteBuffer()
        assert isinstance(buf, AuditBufferProtocol)

    def test_protocol_rejects_non_conforming_class(self):
        """Non-conforming class fails isinstance check."""
        obj = _NotABuffer()
        assert not isinstance(obj, AuditBufferProtocol)

    def test_protocol_has_add_method(self):
        """Protocol defines add() method."""
        assert "add" in dir(AuditBufferProtocol)

    def test_protocol_has_count_method(self):
        """Protocol defines count() method."""
        assert "count" in dir(AuditBufferProtocol)

    def test_protocol_has_get_stats_method(self):
        """Protocol defines get_stats() method."""
        assert "get_stats" in dir(AuditBufferProtocol)


class TestClearableBufferContract:
    """ClearableBuffer design contract verification."""

    def test_clearable_is_runtime_checkable(self):
        """ClearableBuffer is @runtime_checkable."""
        buf = _ClearableConcreteBuffer()
        assert isinstance(buf, ClearableBuffer)

    def test_non_clearable_buffer_does_not_satisfy(self):
        """Buffer without clear() does not satisfy ClearableBuffer."""
        buf = _ConcreteBuffer()
        assert not isinstance(buf, ClearableBuffer)


class TestBufferProtocolBehavior:
    """AuditBufferProtocol concrete behavior verification."""

    def test_add_returns_bool(self):
        """add() returns a bool."""
        buf = _ConcreteBuffer()
        result = buf.add({"event": "test"})
        assert result is True

    def test_count_reflects_added_entries(self):
        """count() returns number of added entries."""
        buf = _ConcreteBuffer()
        buf.add({"a": 1})
        buf.add({"b": 2})
        assert buf.count() == 2

    def test_get_stats_contains_count(self):
        """get_stats() includes count key."""
        buf = _ConcreteBuffer()
        buf.add({"a": 1})
        stats = buf.get_stats()
        assert "count" in stats
        assert stats["count"] == 1


class TestClearableBufferBehavior:
    """ClearableBuffer concrete behavior verification."""

    def test_clear_returns_deleted_count(self):
        """clear() returns number of deleted items."""
        buf = _ClearableConcreteBuffer()
        buf.add({"a": 1})
        buf.add({"b": 2})
        deleted = buf.clear()
        assert deleted == 2
        assert buf.count() == 0

    def test_clear_on_empty_returns_zero(self):
        """clear() on empty buffer returns 0."""
        buf = _ClearableConcreteBuffer()
        assert buf.clear() == 0
