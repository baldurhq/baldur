"""
Tests for PipelineBatchQuery.

Batch state retrieval via Redis pipeline.
"""

from .conftest import MockRedisClient


class TestPipelineBatchQuery:
    """Tests for PipelineBatchQuery."""

    def test_get_multiple_chain_states(self):
        """Test batch retrieval of chain states."""
        from baldur.audit.performance import PipelineBatchQuery

        redis = MockRedisClient()

        # Setup chain states
        redis.hset(
            "test:audit:hash_chain:state:chain1",
            mapping={
                "sequence": "10",
                "previous_hash": "hash_a",
                "updated_at": "2026-01-18T10:00:00Z",
            },
        )
        redis.hset(
            "test:audit:hash_chain:state:chain2",
            mapping={
                "sequence": "20",
                "previous_hash": "hash_b",
            },
        )

        batch = PipelineBatchQuery(redis, key_prefix="test:")
        states = batch.get_multiple_chain_states(["chain1", "chain2"])

        assert "chain1" in states
        assert "chain2" in states
        assert states["chain1"]["sequence"] == 10
        assert states["chain2"]["sequence"] == 20

    def test_missing_chain_returns_default(self):
        """Test that missing chains return default values."""
        from baldur.audit.performance import PipelineBatchQuery

        redis = MockRedisClient()
        batch = PipelineBatchQuery(redis, key_prefix="test:")

        states = batch.get_multiple_chain_states(["missing1", "missing2"])

        assert states["missing1"]["sequence"] == 0
        assert states["missing1"]["previous_hash"] == "GENESIS"

    def test_empty_list_returns_empty(self):
        """Test empty input returns empty result."""
        from baldur.audit.performance import PipelineBatchQuery

        redis = MockRedisClient()
        batch = PipelineBatchQuery(redis, key_prefix="test:")

        states = batch.get_multiple_chain_states([])

        assert states == {}

    def test_batch_check_pending(self):
        """Test batch checking of pending sequences."""
        from baldur.audit.performance import PipelineBatchQuery

        redis = MockRedisClient()

        # Setup some pending entries
        redis._hashes["test:audit:hash_chain:pending:1"] = {"data": "test"}
        redis._hashes["test:audit:hash_chain:pending:3"] = {"data": "test"}

        batch = PipelineBatchQuery(redis, key_prefix="test:")
        results = batch.batch_check_pending([1, 2, 3, 4])

        assert results[1] is True
        assert results[2] is False
        assert results[3] is True
        assert results[4] is False
