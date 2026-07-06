"""
Unit tests for MemoryStateBackend.push_limit() pre-trim return value (414).

Verifies that push_limit returns pre-trim length (unified with Redis RPUSH semantics).
"""

from baldur.core.state_backend import MemoryStateBackend

# =============================================================================
# push_limit — Contract Tests
# =============================================================================


class TestMemoryStateBackendPushLimitContract:
    """Contract: push_limit returns pre-trim length (not post-trim)."""

    def test_push_limit_returns_pre_trim_length_when_no_trim(self):
        """When list is within max_len, pre-trim == post-trim == actual length."""
        backend = MemoryStateBackend()
        result = backend.push_limit("k", "a", max_len=5)
        assert result == 1

        result = backend.push_limit("k", "b", max_len=5)
        assert result == 2

    def test_push_limit_returns_pre_trim_length_when_trim_occurs(self):
        """When list exceeds max_len, return value > max_len (pre-trim)."""
        backend = MemoryStateBackend()
        for i in range(3):
            backend.push_limit("k", i, max_len=3)

        # 4th push: list becomes [0,1,2,3] (len=4) before trim → returns 4
        result = backend.push_limit("k", 3, max_len=3)
        assert result == 4  # pre-trim length > max_len

        # After trim, list has 3 elements
        items = backend.list_range("k", 0, -1)
        assert len(items) == 3
        assert items == [1, 2, 3]

    def test_push_limit_pre_trim_greater_than_max_len_detects_trim(self):
        """return > max_len is the exact condition for 'trim occurred'."""
        backend = MemoryStateBackend()
        for i in range(10):
            backend.push_limit("k", i, max_len=10)

        # At capacity: return == max_len (no trim)
        result = backend.push_limit("k", "at_cap", max_len=10)
        assert result == 11  # 11 > 10 → trim occurred

        # One more: still returns > max_len
        result = backend.push_limit("k", "overflow", max_len=10)
        assert result == 11  # always pre-trim = old_len(10) + 1


# =============================================================================
# push_limit — Behavior Tests
# =============================================================================


class TestMemoryStateBackendPushLimitBehavior:
    """Behavior: push_limit trims oldest and stores correctly."""

    def test_push_limit_keeps_newest_entries(self):
        """After trim, the newest entries are preserved (oldest dropped)."""
        backend = MemoryStateBackend()
        for i in range(7):
            backend.push_limit("k", f"item_{i}", max_len=3)

        items = backend.list_range("k", 0, -1)
        assert items == ["item_4", "item_5", "item_6"]

    def test_push_limit_on_non_list_key_creates_new_list(self):
        """push_limit on a key holding a non-list value starts a fresh list."""
        backend = MemoryStateBackend()
        backend.set("k", {"not": "a list"})

        result = backend.push_limit("k", "fresh", max_len=5)
        assert result == 1
        assert backend.list_range("k", 0, -1) == ["fresh"]

    def test_push_limit_max_len_one_keeps_only_latest(self):
        """max_len=1 always keeps only the most recent value."""
        backend = MemoryStateBackend()
        backend.push_limit("k", "old", max_len=1)
        backend.push_limit("k", "new", max_len=1)

        assert backend.list_range("k", 0, -1) == ["new"]
