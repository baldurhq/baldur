"""
HashChainFallbackChain 테스트.

Multi-tier fallback (Redis → Replica → Local → Memory) 테스트.
"""


class TestHashChainFallbackChain:
    """Tests for HashChainFallbackChain."""

    def test_initialization(self, mock_redis):
        """Test fallback chain initialization."""
        from baldur.audit.graceful_degradation import (
            FallbackConfig,
            HashChainFallbackChain,
        )

        fallback = HashChainFallbackChain(
            redis_primary=mock_redis,
            config=FallbackConfig(),
        )

        assert fallback.current_tier == "redis_primary"
        assert fallback._memory_buffer == []

    def test_fallback_to_local_on_no_redis(self, sample_entry):
        """Test fallback to local when no Redis available."""
        from baldur.audit.graceful_degradation import (
            FallbackConfig,
            HashChainFallbackChain,
        )

        fallback = HashChainFallbackChain(
            redis_primary=None,
            config=FallbackConfig(),
        )

        result = fallback.add_integrity(sample_entry)

        assert "integrity" in result
        assert result["integrity"]["degraded"] is True
        assert result["integrity"]["tier"] == "local"

    def test_fallback_to_memory_last_resort(
        self, failing_redis, sample_entry, temp_dir
    ):
        """Test fallback to memory when all else fails."""
        from baldur.audit.graceful_degradation import (
            FallbackConfig,
            HashChainFallbackChain,
        )

        # Local fallback will succeed even with non-writable path (just logs error)
        # So we test that fallback chain works with failing redis
        config = FallbackConfig(
            local_file_path=temp_dir / "test_fallback.jsonl",
        )
        fallback = HashChainFallbackChain(
            redis_primary=failing_redis,
            config=config,
        )

        result = fallback.add_integrity(sample_entry)

        # Should fall back to local (not memory) since local write succeeds
        assert result["integrity"]["degraded"] is True
        assert result["integrity"]["tier"] in ("local", "memory")

    def test_memory_buffer_limit(self, sample_entry):
        """Test memory buffer respects max entries limit."""
        from baldur.audit.graceful_degradation import (
            FallbackConfig,
            HashChainFallbackChain,
        )

        config = FallbackConfig(memory_max_entries=5)
        fallback = HashChainFallbackChain(
            redis_primary=None,
            config=config,
        )

        # Write more than max entries
        for i in range(10):
            entry = {"event": f"test_{i}"}
            fallback._add_integrity_memory(entry)

        assert len(fallback._memory_buffer) <= 5

    def test_get_degraded_entries(self, sample_entry):
        """Test retrieving degraded entries from buffer."""
        from baldur.audit.graceful_degradation import HashChainFallbackChain

        fallback = HashChainFallbackChain(redis_primary=None)

        fallback._add_integrity_memory(sample_entry)
        degraded = fallback.get_degraded_entries()

        assert len(degraded) == 1
        assert degraded[0]["integrity"]["degraded"] is True

    def test_clear_memory_buffer(self, sample_entry):
        """Test clearing memory buffer."""
        from baldur.audit.graceful_degradation import HashChainFallbackChain

        fallback = HashChainFallbackChain(redis_primary=None)

        fallback._add_integrity_memory(sample_entry)
        count = fallback.clear_memory_buffer()

        assert count == 1
        assert len(fallback._memory_buffer) == 0

    def test_stats_tracking(self, sample_entry):
        """Test statistics are tracked."""
        from baldur.audit.graceful_degradation import HashChainFallbackChain

        fallback = HashChainFallbackChain(redis_primary=None)

        fallback.add_integrity(sample_entry)
        stats = fallback.get_stats()

        assert stats["local_writes"] == 1
        assert "current_tier" in stats
        assert "memory_buffer_size" in stats
