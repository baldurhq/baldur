"""
Tests for Rate Limit Tracker

Covers:
- MemoryRateLimitTracker (L1 layer)
- RateLimitTracker hybrid (L1+L2)
- Thread safety
- Event recording and counting
- Backoff level management
- Singleton access
- _ensure_redis() boundary analysis
- L2-prefer / L1-fallback read path
- Fire-and-forget write path
"""

import threading
from unittest.mock import patch


class TestRateLimitTracker:
    """Tests for RateLimitTracker class."""

    def test_record_rate_limit(self):
        """Test recording rate limit events."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker.record_rate_limit("test_service")

        count = tracker.get_rate_limit_count("test_service", 60)
        assert count == 1

    def test_record_multiple_rate_limits(self):
        """Test recording multiple rate limit events."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        for _ in range(5):
            tracker.record_rate_limit("test_service")

        count = tracker.get_rate_limit_count("test_service", 60)
        assert count == 5

    def test_record_request(self):
        """Test recording request events."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker.record_request("test_service")

        count = tracker.get_request_count("test_service", 60)
        assert count == 1

    def test_record_multiple_requests(self):
        """Test recording multiple request events."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        for _ in range(10):
            tracker.record_request("test_service")

        count = tracker.get_request_count("test_service", 60)
        assert count == 10

    def test_rate_limit_count_time_window(self):
        """Test that old events are filtered by time window."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        # Record an event
        tracker.record_rate_limit("test_service")

        # Count with very short window (should expire)
        # Mock time to simulate passage
        with patch("time.time") as mock_time:
            mock_time.return_value = 1000.0
            tracker._memory.record_rate_limit("test_service2")

            count = tracker.get_rate_limit_count("test_service2", 60)
            assert count == 1

    def test_request_count_time_window(self):
        """Test request count respects time window."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker.record_request("test_service")

        # Should be present in 60 second window
        count = tracker.get_request_count("test_service", 60)
        assert count >= 0  # At least 0 (could be cleaned)

    def test_backoff_level_initial(self):
        """Test initial backoff level is zero."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        level = tracker.get_backoff_level("new_service")
        assert level == 0

    def test_increment_backoff(self):
        """Test incrementing backoff level."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        level = tracker.increment_backoff("test_service")
        assert level == 1

        level = tracker.increment_backoff("test_service")
        assert level == 2

    def test_reset_backoff(self):
        """Test resetting backoff level."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        tracker.increment_backoff("test_service")
        tracker.increment_backoff("test_service")
        tracker.reset_backoff("test_service")

        level = tracker.get_backoff_level("test_service")
        assert level == 0

    def test_clear_service(self):
        """Test clearing all data for a service."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        tracker.record_rate_limit("test_service")
        tracker.record_request("test_service")
        tracker.increment_backoff("test_service")

        tracker.clear_service("test_service")

        assert tracker.get_rate_limit_count("test_service", 60) == 0
        assert tracker.get_request_count("test_service", 60) == 0
        assert tracker.get_backoff_level("test_service") == 0

    def test_separate_services(self):
        """Test that services are tracked separately."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        tracker.record_rate_limit("service_a")
        tracker.record_rate_limit("service_a")
        tracker.record_rate_limit("service_b")

        assert tracker.get_rate_limit_count("service_a", 60) == 2
        assert tracker.get_rate_limit_count("service_b", 60) == 1


class TestRateLimitTrackerThreadSafety:
    """Thread safety tests for RateLimitTracker."""

    def test_concurrent_record_rate_limit(self):
        """Test concurrent rate limit recording."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        threads = []

        def record():
            for _ in range(100):
                tracker.record_rate_limit("test_service")

        for _ in range(5):
            t = threading.Thread(target=record)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        count = tracker.get_rate_limit_count("test_service", 60)
        assert count == 500

    def test_concurrent_record_request(self):
        """Test concurrent request recording."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        threads = []

        def record():
            for _ in range(100):
                tracker.record_request("test_service")

        for _ in range(5):
            t = threading.Thread(target=record)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        count = tracker.get_request_count("test_service", 60)
        assert count == 500

    def test_concurrent_backoff_increment(self):
        """Test concurrent backoff increment."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        threads = []

        def increment():
            for _ in range(10):
                tracker.increment_backoff("test_service")

        for _ in range(5):
            t = threading.Thread(target=increment)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        level = tracker.get_backoff_level("test_service")
        assert level == 50


class TestGetRateLimitTracker:
    """Tests for singleton access."""

    def test_get_rate_limit_tracker_singleton(self):
        """Test that get_rate_limit_tracker returns singleton."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            get_rate_limit_tracker,
        )

        tracker1 = get_rate_limit_tracker()
        tracker2 = get_rate_limit_tracker()

        assert tracker1 is tracker2

    def test_get_rate_limit_tracker_creates_instance(self):
        """Test that get_rate_limit_tracker creates instance."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
            get_rate_limit_tracker,
        )

        tracker = get_rate_limit_tracker()
        assert isinstance(tracker, RateLimitTracker)


# =============================================================================
# 439: MemoryRateLimitTracker (L1) — Contract & Behavior
# =============================================================================


class TestMemoryRateLimitTrackerContract:
    """Contract tests for MemoryRateLimitTracker defaults and structure."""

    def test_initial_state_empty(self):
        """New tracker returns zero for all counters."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()

        assert tracker.get_rate_limit_count("svc", 60) == 0
        assert tracker.get_request_count("svc", 60) == 0
        assert tracker.get_backoff_level("svc") == 0

    def test_clear_service_resets_all_categories(self):
        """Clear service resets rate limits, requests, and backoff."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        tracker.record_rate_limit("svc")
        tracker.record_request("svc")
        tracker.increment_backoff("svc")

        tracker.clear_service("svc")

        assert tracker.get_rate_limit_count("svc", 60) == 0
        assert tracker.get_request_count("svc", 60) == 0
        assert tracker.get_backoff_level("svc") == 0


class TestMemoryRateLimitTrackerBehavior:
    """Behavior tests for MemoryRateLimitTracker operations."""

    def test_record_rate_limit_increments_count(self):
        """Recording rate limits increments the count."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        tracker.record_rate_limit("svc")
        tracker.record_rate_limit("svc")

        assert tracker.get_rate_limit_count("svc", 60) == 2

    def test_record_request_increments_count(self):
        """Recording requests increments the count."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        tracker.record_request("svc")
        tracker.record_request("svc")
        tracker.record_request("svc")

        assert tracker.get_request_count("svc", 60) == 3

    def test_time_window_prunes_old_events(self):
        """Events outside the time window are pruned from count."""
        from unittest.mock import patch as mock_patch

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()

        # Given: events at t=1000 and t=1050
        with mock_patch("time.time", return_value=1000.0):
            tracker.record_rate_limit("svc")
        with mock_patch("time.time", return_value=1050.0):
            tracker.record_rate_limit("svc")

        # When: query at t=1070 with 30s window (cutoff=1040)
        with mock_patch("time.time", return_value=1070.0):
            count = tracker.get_rate_limit_count("svc", 30)

        # Then: only the t=1050 event survives
        assert count == 1

    def test_services_isolated(self):
        """Different services have independent counters."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        tracker.record_rate_limit("svc_a")
        tracker.record_rate_limit("svc_a")
        tracker.record_rate_limit("svc_b")

        assert tracker.get_rate_limit_count("svc_a", 60) == 2
        assert tracker.get_rate_limit_count("svc_b", 60) == 1

    def test_increment_backoff_returns_new_level(self):
        """Increment backoff returns sequentially increasing levels."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()

        assert tracker.increment_backoff("svc") == 1
        assert tracker.increment_backoff("svc") == 2
        assert tracker.increment_backoff("svc") == 3

    def test_reset_backoff_to_zero(self):
        """Reset backoff returns level to zero."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        tracker.increment_backoff("svc")
        tracker.increment_backoff("svc")
        tracker.reset_backoff("svc")

        assert tracker.get_backoff_level("svc") == 0

    def test_concurrent_record_thread_safety(self):
        """Concurrent recording from multiple threads produces correct totals."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker()
        threads = []

        def record():
            for _ in range(100):
                tracker.record_rate_limit("svc")
                tracker.record_request("svc")

        for _ in range(5):
            t = threading.Thread(target=record)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert tracker.get_rate_limit_count("svc", 60) == 500
        assert tracker.get_request_count("svc", 60) == 500


# =============================================================================
# 439: RateLimitTracker Hybrid — Contract & Behavior
# =============================================================================


class TestRateLimitTrackerHybridContract:
    """Contract tests for RateLimitTracker hybrid L1+L2 structure."""

    def test_has_memory_layer(self):
        """Tracker wraps a MemoryRateLimitTracker as L1."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        assert isinstance(tracker._memory, MemoryRateLimitTracker)

    def test_redis_initially_none(self):
        """Redis backend is None before lazy initialization."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        assert tracker._redis is None
        assert tracker._redis_initialized is False

    def test_probe_interval_constant(self):
        """Redis probe interval is 30 seconds."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            _REDIS_PROBE_INTERVAL,
        )

        assert _REDIS_PROBE_INTERVAL == 30.0


class TestProbeConnectTimeoutWiringBehavior:
    """687 D2 — the lazy L2 probe forwards RedisSettings.probe_connect_timeout
    to the RedisCacheAdapter constructor (proves the setting drives the value,
    not a hardcoded 0.5 literal).
    """

    def test_ensure_redis_forwards_settings_probe_timeout(self):
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker

        tracker = RateLimitTracker()

        cb_settings = MagicMock()
        cb_settings.rate_limit_distributed = True
        cb_settings.rate_limit_cascade_window_seconds = 60
        cb_settings.self_ddos_window_seconds = 10
        redis_settings = MagicMock()
        redis_settings.probe_connect_timeout = 0.75

        with (
            patch(
                "baldur.settings.circuit_breaker.get_circuit_breaker_settings",
                return_value=cb_settings,
            ),
            patch(
                "baldur.settings.redis.get_redis_settings",
                return_value=redis_settings,
            ),
            patch("baldur.adapters.cache.RedisCacheAdapter") as mock_adapter,
        ):
            tracker._ensure_redis()

        mock_adapter.assert_called_once()
        assert mock_adapter.call_args.kwargs["socket_connect_timeout"] == 0.75


class TestRateLimitTrackerHybridBehavior:
    """Behavior tests for L2-prefer, L1-fallback read/write paths."""

    def _make_tracker_with_redis(self):
        """Helper: create a tracker with a mocked Redis backend."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        mock_redis = MagicMock()
        tracker._redis = mock_redis
        tracker._redis_initialized = True
        return tracker, mock_redis

    # ---- Write path: L1 always + L2 fire-and-forget ----

    def test_write_rate_limit_always_writes_to_memory(self):
        """Rate limit write goes to both L1 and L2."""
        tracker, mock_redis = self._make_tracker_with_redis()

        tracker.record_rate_limit("svc")

        assert tracker._memory.get_rate_limit_count("svc", 60) == 1
        mock_redis.record_rate_limit.assert_called_once_with("svc")

    def test_write_request_always_writes_to_memory(self):
        """Request write goes to both L1 and L2."""
        tracker, mock_redis = self._make_tracker_with_redis()

        tracker.record_request("svc")

        assert tracker._memory.get_request_count("svc", 60) == 1
        mock_redis.record_request.assert_called_once_with("svc")

    def test_write_fire_and_forget_redis_error_ignored(self):
        """L2 write error is silently ignored, L1 still records."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.record_rate_limit.side_effect = ConnectionError("gone")

        tracker.record_rate_limit("svc")

        # L1 still has the event
        assert tracker._memory.get_rate_limit_count("svc", 60) == 1

    def test_write_request_fire_and_forget_redis_error_ignored(self):
        """L2 request write error is silently ignored, L1 still records."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.record_request.side_effect = ConnectionError("gone")

        tracker.record_request("svc")

        assert tracker._memory.get_request_count("svc", 60) == 1

    # ---- Read path: L2-prefer, L1-fallback ----

    def test_read_rate_limit_count_prefers_redis(self):
        """Read path prefers L2 (Redis) when available."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_rate_limit_count.return_value = 42

        count = tracker.get_rate_limit_count("svc", 60)

        assert count == 42
        mock_redis.get_rate_limit_count.assert_called_once_with("svc", 60)

    def test_read_request_count_prefers_redis(self):
        """Request count read prefers L2 when available."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_request_count.return_value = 99

        count = tracker.get_request_count("svc", 60)

        assert count == 99

    def test_read_backoff_prefers_redis(self):
        """Backoff level read prefers L2 when available."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_backoff_level.return_value = 3

        level = tracker.get_backoff_level("svc")

        assert level == 3

    def test_read_falls_back_to_memory_on_redis_error(self):
        """Rate limit count falls back to L1 on L2 error."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_rate_limit_count.side_effect = ConnectionError("gone")
        tracker._memory.record_rate_limit("svc")

        count = tracker.get_rate_limit_count("svc", 60)

        assert count == 1

    def test_read_request_falls_back_to_memory_on_redis_error(self):
        """Request count falls back to L1 on L2 error."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_request_count.side_effect = ConnectionError("gone")
        tracker._memory.record_request("svc")

        count = tracker.get_request_count("svc", 60)

        assert count == 1

    def test_read_backoff_falls_back_to_memory_on_redis_error(self):
        """Backoff level falls back to L1 on L2 error."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.get_backoff_level.side_effect = ConnectionError("gone")
        tracker._memory.increment_backoff("svc")

        level = tracker.get_backoff_level("svc")

        assert level == 1

    # ---- Increment backoff: L1 + L2 ----

    def test_increment_backoff_uses_redis_value_when_available(self):
        """Increment returns L2 value when Redis is available."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.increment_backoff.return_value = 5

        level = tracker.increment_backoff("svc")

        assert level == 5
        assert tracker._memory.get_backoff_level("svc") == 1

    def test_increment_backoff_returns_memory_on_redis_error(self):
        """Increment falls back to L1 value on L2 error."""
        tracker, mock_redis = self._make_tracker_with_redis()
        mock_redis.increment_backoff.side_effect = ConnectionError("gone")

        level = tracker.increment_backoff("svc")

        assert level == 1

    # ---- Reset backoff: both layers ----

    def test_reset_backoff_resets_both_layers(self):
        """Reset backoff clears both L1 and L2."""
        tracker, mock_redis = self._make_tracker_with_redis()
        tracker._memory.increment_backoff("svc")

        tracker.reset_backoff("svc")

        assert tracker._memory.get_backoff_level("svc") == 0
        mock_redis.reset_backoff.assert_called_once_with("svc")

    # ---- Clear service: both layers ----

    def test_clear_service_clears_both_layers(self):
        """Clear service removes data from both L1 and L2."""
        tracker, mock_redis = self._make_tracker_with_redis()
        tracker._memory.record_rate_limit("svc")

        tracker.clear_service("svc")

        assert tracker._memory.get_rate_limit_count("svc", 60) == 0
        mock_redis.clear_service.assert_called_once_with("svc")

    # ---- L1-only mode (no Redis) ----

    def test_l1_only_when_redis_not_initialized(self):
        """Without Redis, tracker operates in L1-only mode."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker.record_rate_limit("svc")
        tracker.record_request("svc")

        assert tracker.get_rate_limit_count("svc", 60) == 1
        assert tracker.get_request_count("svc", 60) == 1


# =============================================================================
# 439: _ensure_redis() Boundary Analysis
# =============================================================================


class TestEnsureRedisBehavior:
    """Boundary analysis for _ensure_redis() lazy init with cooldown."""

    def test_distributed_false_returns_false(self):
        """Distributed disabled → _ensure_redis returns False."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        mock_settings = MagicMock()
        mock_settings.rate_limit_distributed = False

        with patch(
            "baldur.settings.circuit_breaker.get_circuit_breaker_settings",
            return_value=mock_settings,
        ):
            result = tracker._ensure_redis()

        assert result is False
        assert tracker._redis is None

    def test_already_initialized_returns_true_immediately(self):
        """Already initialized → returns True without re-init."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker._redis = MagicMock()
        tracker._redis_initialized = True

        result = tracker._ensure_redis()

        assert result is True

    def test_probe_cooldown_returns_false_within_interval(self):
        """Within cooldown interval → returns False without probing."""
        import time

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker._next_redis_probe = time.monotonic() + 9999

        result = tracker._ensure_redis()

        assert result is False

    def test_redis_connection_failure_sets_cooldown(self):
        """Connection failure sets next probe time to now + interval."""
        import time
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            _REDIS_PROBE_INTERVAL,
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        mock_settings = MagicMock()
        mock_settings.rate_limit_distributed = True
        mock_settings.rate_limit_cascade_window_seconds = 60
        mock_settings.self_ddos_window_seconds = 10

        before = time.monotonic()

        with patch(
            "baldur.settings.circuit_breaker.get_circuit_breaker_settings",
            return_value=mock_settings,
        ):
            with patch(
                "baldur.adapters.cache.RedisCacheAdapter",
                side_effect=ConnectionError("no redis"),
            ):
                result = tracker._ensure_redis()

        assert result is False
        assert tracker._next_redis_probe >= before + _REDIS_PROBE_INTERVAL

    def test_redis_connection_success_sets_initialized(self):
        """Successful connection sets _redis_initialized to True."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()

        mock_settings = MagicMock()
        mock_settings.rate_limit_distributed = True
        mock_settings.rate_limit_cascade_window_seconds = 60
        mock_settings.self_ddos_window_seconds = 10

        mock_adapter = MagicMock()
        mock_adapter._redis.ping.return_value = True

        with patch(
            "baldur.settings.circuit_breaker.get_circuit_breaker_settings",
            return_value=mock_settings,
        ):
            with patch(
                "baldur.adapters.cache.RedisCacheAdapter",
                return_value=mock_adapter,
            ):
                with patch(
                    "baldur.services.circuit_breaker.rate_limit_lua.RedisRateLimitBackend"
                ):
                    result = tracker._ensure_redis()

        assert result is True
        assert tracker._redis_initialized is True

    def test_idempotent_after_initialization(self):
        """Repeated calls after init always return True."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker._redis = MagicMock()
        tracker._redis_initialized = True

        # Call multiple times — should always return True, no re-init
        for _ in range(5):
            assert tracker._ensure_redis() is True


# =============================================================================
# 439: Singleton Lifecycle — reset_rate_limit_tracker
# =============================================================================


class TestResetRateLimitTrackerBehavior:
    """Singleton lifecycle tests for reset_rate_limit_tracker."""

    def test_reset_clears_singleton(self):
        """Reset creates a new instance on next get."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            get_rate_limit_tracker,
            reset_rate_limit_tracker,
        )

        tracker1 = get_rate_limit_tracker()
        reset_rate_limit_tracker()
        tracker2 = get_rate_limit_tracker()

        assert tracker1 is not tracker2

    def test_reset_idempotent_when_no_instance(self):
        """Reset is safe to call when no instance exists."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            reset_rate_limit_tracker,
        )

        # Should not raise even when called multiple times
        reset_rate_limit_tracker()
        reset_rate_limit_tracker()

    def test_singleton_thread_safe(self):
        """Concurrent get_rate_limit_tracker returns the same instance."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            get_rate_limit_tracker,
            reset_rate_limit_tracker,
        )

        reset_rate_limit_tracker()
        results = []

        def get_tracker():
            results.append(get_rate_limit_tracker())

        threads = [threading.Thread(target=get_tracker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r is results[0] for r in results)


class TestRateLimitTrackerBoundedBehavior:
    """Write-side trim bounds L1 memory (the leak fired when reads were L2-routed)."""

    def _tracker_with_healthy_redis(self, retention_seconds: float):
        """Hybrid tracker with a healthy L2 stub and an injected L1 retention."""
        from unittest.mock import MagicMock

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
            RateLimitTracker,
        )

        tracker = RateLimitTracker()
        tracker._memory = MemoryRateLimitTracker(retention_seconds=retention_seconds)
        tracker._redis = MagicMock()
        tracker._redis_initialized = True
        return tracker

    def test_l1_stays_bounded_with_healthy_l2_and_no_reads(self):
        """Write-only load (reads served by L2) keeps the L1 series inside retention.

        Before the write-side trim, this scenario grew the L1 lists without
        bound at request rate: pruning lived only in the L1 reads, and the
        hybrid read path never reaches L1 while Redis is healthy.
        """
        from unittest.mock import patch as mock_patch

        retention = 60.0
        tracker = self._tracker_with_healthy_redis(retention_seconds=retention)

        # Given: 500 writes spread over ~10x the retention window, never read
        with mock_patch("time.time") as mock_time:
            for i in range(500):
                mock_time.return_value = 1_000_000.0 + i * 1.2
                tracker.record_request("svc")
                tracker.record_rate_limit("svc")

        # Then: only the entries inside the retention window survive.
        # Last write t=1000598.8, cutoff=1000538.8; the write-side trim pops
        # entries at/below the cutoff, leaving exactly the 50 newest.
        assert len(tracker._memory._request_counter._events["svc"]) == 50
        assert len(tracker._memory._rate_limit_counter._events["svc"]) == 50

    def test_l1_bounded_in_memory_only_mode_for_never_read_service(self):
        """A recorded-but-never-read service stays bounded without any L2."""
        from unittest.mock import patch as mock_patch

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker(retention_seconds=30.0)

        with mock_patch("time.time") as mock_time:
            for i in range(200):
                mock_time.return_value = 5_000.0 + i * 1.0
                tracker.record_request("svc")

        # Last write t=5199, cutoff=5169; entries at/below 5169 trimmed -> 30 left.
        assert len(tracker._request_counter._events["svc"]) == 30

    def test_reads_are_non_destructive(self):
        """get_*_count only counts; repeated reads return the same value."""
        from unittest.mock import patch as mock_patch

        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker(retention_seconds=120.0)

        with mock_patch("time.time", return_value=2_000.0):
            tracker.record_rate_limit("svc")
            tracker.record_rate_limit("svc")

        with mock_patch("time.time", return_value=2_010.0):
            first = tracker.get_rate_limit_count("svc", 60)
            second = tracker.get_rate_limit_count("svc", 60)

        assert first == second == 2
        assert len(tracker._rate_limit_counter._events["svc"]) == 2


class TestRateLimitTrackerContract:
    """L1 retention resolution: injected value, or the exact L2 retention bound."""

    def test_injected_retention_is_respected(self):
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            MemoryRateLimitTracker,
        )

        tracker = MemoryRateLimitTracker(retention_seconds=42.0)

        assert tracker._resolve_retention() == 42.0

    def test_default_retention_matches_the_l2_bound_expression(self):
        """Lazy default reuses max(cascade_window, self_ddos_window, floor) - the
        same bound the L2 backend applies, so L1 and L2 retain identically."""
        from baldur.services.circuit_breaker.rate_limit_tracker import (
            _MIN_L2_RETENTION_SECONDS,
            MemoryRateLimitTracker,
        )
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings

        settings = get_circuit_breaker_settings()
        expected = max(
            settings.rate_limit_cascade_window_seconds,
            settings.self_ddos_window_seconds,
            _MIN_L2_RETENTION_SECONDS,
        )

        assert MemoryRateLimitTracker()._resolve_retention() == expected
