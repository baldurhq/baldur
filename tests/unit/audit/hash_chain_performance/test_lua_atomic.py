"""
Tests for LuaAtomicHashChain.

Atomic sequence operations via Lua scripts.
"""

from .conftest import MockRedisClient


class TestLuaAtomicHashChainBehavior:
    """Tests for LuaAtomicHashChain."""

    def test_reserve_sequence_atomic(self):
        """Test atomic sequence reservation."""
        from baldur.audit.performance import LuaAtomicHashChain

        redis = MockRedisClient()
        lua_chain = LuaAtomicHashChain(redis, key_prefix="test:")

        success, seq, error = lua_chain.reserve_sequence_atomic(
            expected_hash="hash123",
            previous_hash="GENESIS",
        )

        assert success is True
        assert seq == 1
        assert error == ""

    def test_reserve_sequence_increments(self):
        """Test that sequence increments correctly."""
        from baldur.audit.performance import LuaAtomicHashChain

        redis = MockRedisClient()
        lua_chain = LuaAtomicHashChain(redis, key_prefix="test:")

        _, seq1, _ = lua_chain.reserve_sequence_atomic("hash1", "GENESIS")
        _, seq2, _ = lua_chain.reserve_sequence_atomic("hash2", "hash1")
        _, seq3, _ = lua_chain.reserve_sequence_atomic("hash3", "hash2")

        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3

    def test_commit_sequence_atomic(self):
        """Test atomic sequence commit."""
        from baldur.audit.performance import LuaAtomicHashChain

        redis = MockRedisClient()
        lua_chain = LuaAtomicHashChain(redis, key_prefix="test:")

        # Reserve first
        lua_chain.reserve_sequence_atomic("expected_hash", "GENESIS")

        # Mock pending entry
        pending_key = "test:audit:{hash_chain}:pending:1"
        redis._hashes[pending_key] = {"expected_hash": "expected_hash"}

        # Commit
        success, error = lua_chain.commit_sequence_atomic(1, "expected_hash")

        assert success is True
        assert error == ""

    def test_commit_nonexistent_fails(self):
        """Test commit of non-existent pending fails."""
        from baldur.audit.performance import LuaAtomicHashChain

        redis = MockRedisClient()
        lua_chain = LuaAtomicHashChain(redis, key_prefix="test:")

        success, error = lua_chain.commit_sequence_atomic(999, "some_hash")

        assert success is False
        assert "NOT_FOUND" in error or error != ""

    def test_redis_failure_handling(self):
        """Test handling of Redis failures."""
        from baldur.audit.performance import LuaAtomicHashChain

        redis = MockRedisClient(should_fail=True)
        lua_chain = LuaAtomicHashChain(redis, key_prefix="test:")

        success, seq, error = lua_chain.reserve_sequence_atomic("hash", "prev")

        assert success is False
        assert "connection" in error.lower() or error != ""
