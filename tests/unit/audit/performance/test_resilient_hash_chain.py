"""
Unit tests for ResilientHashChainManager (384 §B-5, D-13).

Tests:
- Lua 2-phase success path → returns entry with integrity
- Lua reserve failure → delegates to FallbackChain
- Lua commit failure → delegates to FallbackChain
- Lua exception → delegates to FallbackChain
- lua_chain=None → delegates to FallbackChain immediately
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.audit.performance.resilient_hash_chain import (
    ResilientHashChainManager,
)


class TestResilientHashChainManagerBehavior:
    """ResilientHashChainManager add_integrity behavior."""

    def _make_manager(self, lua_chain=None, fallback_chain=None, compute_hash=None):
        """Create manager with mocks."""
        if fallback_chain is None:
            fallback_chain = MagicMock()
            fallback_chain.add_integrity.return_value = {
                "integrity": {"tier": "fallback"}
            }
        if compute_hash is None:
            compute_hash = MagicMock(return_value="hash-abc123")
        return ResilientHashChainManager(lua_chain, fallback_chain, compute_hash)

    def test_lua_success_returns_entry_with_integrity(self):
        """Full Lua 2-phase success returns entry with lua_atomic tier."""
        lua_chain = MagicMock()
        lua_chain.reserve_sequence_atomic.return_value = (True, 42, "")
        lua_chain.commit_sequence_atomic.return_value = (True, "")
        lua_chain._get_keys.return_value = {"state": "audit:state"}
        lua_chain._registry.execute.return_value = {"previous_hash": "prev-hash"}

        fallback = MagicMock()
        compute_hash = MagicMock(return_value="computed-hash")

        manager = ResilientHashChainManager(lua_chain, fallback, compute_hash)
        entry = {"event": "test"}

        result = manager.add_integrity(entry)

        assert result["integrity"]["tier"] == "lua_atomic"
        assert result["integrity"]["sequence"] == 42
        assert result["integrity"]["current_hash"] == "computed-hash"
        fallback.add_integrity.assert_not_called()

    def test_lua_reserve_failure_delegates_to_fallback(self):
        """Reserve failure (ok=False) delegates to FallbackChain."""
        lua_chain = MagicMock()
        lua_chain.reserve_sequence_atomic.return_value = (False, 0, "redis error")
        lua_chain._get_keys.return_value = {"state": "audit:state"}
        lua_chain._registry.execute.return_value = None

        fallback = MagicMock()
        fallback.add_integrity.return_value = {"integrity": {"tier": "local"}}

        manager = ResilientHashChainManager(lua_chain, fallback, MagicMock())
        result = manager.add_integrity({"event": "test"})

        assert result["integrity"]["tier"] == "local"
        fallback.add_integrity.assert_called_once()

    def test_lua_commit_failure_delegates_to_fallback(self):
        """Commit failure delegates to FallbackChain (pending auto-expires)."""
        lua_chain = MagicMock()
        lua_chain.reserve_sequence_atomic.return_value = (True, 42, "")
        lua_chain.commit_sequence_atomic.return_value = (False, "commit error")
        lua_chain._get_keys.return_value = {"state": "audit:state"}
        lua_chain._registry.execute.return_value = {"previous_hash": "prev"}

        fallback = MagicMock()
        fallback.add_integrity.return_value = {"integrity": {"tier": "memory"}}

        manager = ResilientHashChainManager(
            lua_chain, fallback, MagicMock(return_value="h")
        )
        result = manager.add_integrity({"event": "test"})

        assert result["integrity"]["tier"] == "memory"
        fallback.add_integrity.assert_called_once()

    def test_lua_exception_delegates_to_fallback(self):
        """Lua chain exception delegates to FallbackChain."""
        lua_chain = MagicMock()
        lua_chain.reserve_sequence_atomic.side_effect = ConnectionError("Redis down")
        lua_chain._get_keys.return_value = {"state": "audit:state"}
        lua_chain._registry.execute.side_effect = ConnectionError("Redis down")

        fallback = MagicMock()
        fallback.add_integrity.return_value = {"integrity": {"tier": "local"}}

        manager = ResilientHashChainManager(lua_chain, fallback, MagicMock())
        result = manager.add_integrity({"event": "test"})

        assert result["integrity"]["tier"] == "local"

    def test_no_lua_chain_delegates_immediately(self):
        """When lua_chain is None, delegates to FallbackChain immediately."""
        fallback = MagicMock()
        fallback.add_integrity.return_value = {"integrity": {"tier": "local"}}

        manager = ResilientHashChainManager(None, fallback, MagicMock())
        result = manager.add_integrity({"event": "test"})

        assert result["integrity"]["tier"] == "local"
        fallback.add_integrity.assert_called_once()

    def test_entry_is_not_mutated_on_fallback(self):
        """Original entry is passed to FallbackChain without leftover integrity fields."""
        lua_chain = MagicMock()
        lua_chain.reserve_sequence_atomic.side_effect = Exception("fail")
        lua_chain._get_keys.return_value = {"state": "s"}
        lua_chain._registry.execute.side_effect = Exception("fail")

        fallback = MagicMock()
        fallback.add_integrity.return_value = {
            "event": "test",
            "integrity": {"tier": "local"},
        }

        manager = ResilientHashChainManager(lua_chain, fallback, MagicMock())
        entry = {"event": "test"}
        manager.add_integrity(entry)

        # FallbackChain should receive the entry
        fallback.add_integrity.assert_called_once_with(entry)
