"""
Tests for HashChainPerformanceManager.

Unified component access.
"""

import tempfile
from pathlib import Path

import pytest

from .conftest import MockRedisClient


class TestHashChainPerformanceManager:
    """Tests for HashChainPerformanceManager."""

    def test_initialization(self):
        """Test manager initialization."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(
            redis_client=redis,
            key_prefix="test:",
        )

        assert manager._redis is redis
        assert manager._lua_chain is None  # Lazy
        assert manager._sampler is None  # Lazy

    def test_lazy_init_lua_chain(self):
        """Test lazy initialization of lua_chain."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        # First access triggers initialization
        lua_chain = manager.lua_chain

        assert lua_chain is not None
        assert manager._lua_chain is lua_chain

        # Second access returns same instance
        assert manager.lua_chain is lua_chain

    def test_lazy_init_batch_query(self):
        """Test lazy initialization of batch_query."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        batch_query = manager.batch_query

        assert batch_query is not None
        assert manager._batch_query is batch_query

    def test_lazy_init_sampler(self):
        """Test lazy initialization of sampler."""
        from baldur.audit.performance import HashChainPerformanceManager

        manager = HashChainPerformanceManager()

        sampler = manager.sampler

        assert sampler is not None
        assert manager._sampler is sampler

    def test_no_redis_raises_error(self):
        """Test that components requiring Redis raise error."""
        from baldur.audit.performance import HashChainPerformanceManager

        manager = HashChainPerformanceManager()  # No Redis

        with pytest.raises(ValueError, match="Redis client required"):
            _ = manager.lua_chain

        with pytest.raises(ValueError, match="Redis client required"):
            _ = manager.batch_query

    def test_get_batch_writer(self):
        """Test creating batch writer."""
        from baldur.audit.performance import (
            BatchFlushWriter,
            HashChainPerformanceManager,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = HashChainPerformanceManager()
            path = Path(tmpdir) / "test.jsonl"

            writer = manager.get_batch_writer(path)

            assert writer is not None
            assert isinstance(writer, BatchFlushWriter)

    def test_get_async_writer(self):
        """Test creating async writer."""
        from baldur.audit.performance import (
            AsyncAuditWriter,
            HashChainPerformanceManager,
        )

        manager = HashChainPerformanceManager()

        def sync_writer(entry):
            return True

        writer = manager.get_async_writer(sync_writer)

        assert writer is not None
        assert isinstance(writer, AsyncAuditWriter)

    def test_get_watchdog(self):
        """Test getting watchdog."""
        from baldur.audit.performance import (
            HashChainPerformanceManager,
            PendingSequenceWatchdog,
        )

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        watchdog = manager.get_watchdog()

        assert watchdog is not None
        assert isinstance(watchdog, PendingSequenceWatchdog)

    def test_start_watchdog(self):
        """Test starting watchdog."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        manager.start_watchdog()

        assert manager._watchdog is not None
        assert manager._watchdog._is_running is True

        manager.stop_all()

    def test_stop_all(self):
        """Test stopping all components."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        manager.start_watchdog()

        manager.stop_all()

        assert manager._watchdog._is_running is False

    def test_get_all_stats(self):
        """Test getting all statistics."""
        from baldur.audit.performance import HashChainPerformanceManager

        redis = MockRedisClient()
        manager = HashChainPerformanceManager(redis_client=redis)

        manager.start_watchdog()
        manager._watchdog.register_pending(1)

        stats = manager.get_all_stats()

        assert "watchdog" in stats
        assert stats["watchdog"]["local_pending_count"] == 1

        manager.stop_all()
