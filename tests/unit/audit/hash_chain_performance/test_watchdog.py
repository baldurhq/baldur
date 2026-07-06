"""
Tests for PendingSequenceWatchdog.

Self-cleanup daemon for pending sequences.
"""

import time

from .conftest import MockRedisClient


class TestPendingSequenceWatchdog:
    """Tests for PendingSequenceWatchdog."""

    def test_register_and_commit(self):
        """Test registering and committing sequences."""
        from baldur.audit.performance import PendingSequenceWatchdog

        redis = MockRedisClient()
        watchdog = PendingSequenceWatchdog(redis, key_prefix="test:")

        watchdog.register_pending(1)
        watchdog.register_pending(2)

        assert 1 in watchdog._local_pending
        assert 2 in watchdog._local_pending

        watchdog.mark_committed(1)

        assert 1 not in watchdog._local_pending
        assert 2 in watchdog._local_pending

    def test_mark_failed_cleans_redis(self):
        """Test that mark_failed cleans up Redis."""
        from baldur.audit.performance import PendingSequenceWatchdog

        redis = MockRedisClient()
        redis._hashes["test:audit:hash_chain:pending:5"] = {"data": "test"}

        watchdog = PendingSequenceWatchdog(redis, key_prefix="test:")
        watchdog.register_pending(5)

        watchdog.mark_failed(5)

        # Should be removed from both local and Redis
        assert 5 not in watchdog._local_pending
        assert "test:audit:hash_chain:pending:5" not in redis._hashes

    def test_watchdog_lifecycle(self):
        """Test watchdog start/stop lifecycle."""
        from baldur.audit.performance import PendingSequenceWatchdog

        redis = MockRedisClient()
        watchdog = PendingSequenceWatchdog(
            redis,
            key_prefix="test:",
            check_interval_seconds=0.1,
        )

        assert watchdog._is_running is False

        watchdog.start()
        assert watchdog._is_running is True

        time.sleep(0.2)

        watchdog.stop()
        assert watchdog._is_running is False

    def test_stats(self):
        """Test statistics retrieval."""
        from baldur.audit.performance import PendingSequenceWatchdog

        redis = MockRedisClient()
        watchdog = PendingSequenceWatchdog(redis, key_prefix="test:")

        watchdog.register_pending(1)
        watchdog.register_pending(2)
        watchdog.mark_committed(1)

        stats = watchdog.get_stats()

        assert stats["local_pending_count"] == 1
        assert stats["is_running"] is False
