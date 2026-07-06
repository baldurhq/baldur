"""
Unit Tests for 437 — Zero-Config First-Run Experience: Lazy Redis Init.

Tests for _ensure_redis() lazy initialization in ResilientStorageBackend:
- Initial DEGRADED mode (no Redis call in __init__)
- Lazy Redis probe with 500ms timeout
- 30s cooldown to prevent retry storm
- Redis negative cache integration
- WAL recovery after successful lazy connect
- Thread-safe DCL
- One-time CRITICAL log on first failure
"""

import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_redis_negative_cache():
    """Clear Redis negative cache so _ensure_redis() proceeds to connection."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    prev = (state.unavailable, state.fail_time)
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable, state.fail_time = prev


# =============================================================================
# Contract Tests — Design decisions from 437
# =============================================================================


class TestLazyRedisInitContract:
    """Design contract verification for 437 lazy Redis initialization."""

    @pytest.fixture
    def temp_wal_dir(self):
        """Create temporary WAL directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def config(self, temp_wal_dir):
        """Create config for memory-only backend."""
        from baldur.settings.resilient_storage import ResilientStorageSettings

        return ResilientStorageSettings(
            wal_dir=temp_wal_dir,
            allow_memory_only=True,
        )

    def test_init_starts_in_degraded_mode(self, config):
        """D2: Backend starts in DEGRADED mode, not REDIS."""
        from baldur.adapters.resilient.backend import (
            ResilientStorageBackend,
            ResilientStorageMode,
        )

        backend = ResilientStorageBackend(config)
        assert backend.mode == ResilientStorageMode.DEGRADED
        backend.close()

    def test_init_does_not_attempt_redis_connection(self, config):
        """D1: __init__() must NOT attempt Redis I/O."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            backend = ResilientStorageBackend(config)
            MockAdapter.assert_not_called()
        backend.close()

    def test_probe_interval_is_30_seconds(self, config):
        """D3: Redis probe cooldown interval is 30 seconds."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        backend = ResilientStorageBackend(config)
        assert backend._REDIS_PROBE_INTERVAL == 30.0
        backend.close()

    def test_initial_probe_allowed_immediately(self, config):
        """First _ensure_redis() must not be blocked by cooldown."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        backend = ResilientStorageBackend(config)
        assert backend._next_redis_probe == 0.0
        backend.close()

    def test_ensure_redis_uses_500ms_connect_timeout(self, config):
        """Lazy probe uses socket_connect_timeout=0.5 (not default 5.0s)."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            mock_instance = MagicMock()
            mock_instance._redis.ping.return_value = True
            MockAdapter.return_value = mock_instance

            backend = ResilientStorageBackend(config)
            backend._ensure_redis()

            call_kwargs = MockAdapter.call_args[1]
            assert call_kwargs["socket_connect_timeout"] == 0.5
        backend.close()


# =============================================================================
# Behavior Tests — _ensure_redis() behavior
# =============================================================================


class TestEnsureRedisBehavior:
    """Behavior verification for _ensure_redis() lazy initialization."""

    @pytest.fixture
    def temp_wal_dir(self):
        """Create temporary WAL directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def config(self, temp_wal_dir):
        """Create config for memory-only backend."""
        from baldur.settings.resilient_storage import ResilientStorageSettings

        return ResilientStorageSettings(
            wal_dir=temp_wal_dir,
            allow_memory_only=True,
        )

    @pytest.fixture
    def backend(self, config):
        """Create a fresh backend instance (starts in DEGRADED mode)."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend

        b = ResilientStorageBackend(config)
        yield b
        b.close()

    # -- Idempotency: fast-path --

    def test_ensure_redis_returns_true_when_already_initialized(self, backend):
        """Fast path: returns True immediately if Redis already connected."""
        from baldur.adapters.resilient.backend import ResilientStorageMode

        backend._redis_initialized = True
        backend._mode = ResilientStorageMode.REDIS
        assert backend._ensure_redis() is True

    # -- State transition --

    def test_ensure_redis_forwards_settings_probe_timeout(self, backend):
        """687 D2: the probe forwards RedisSettings.probe_connect_timeout to the
        RedisCacheAdapter ctor (proves the setting drives it, not a 0.5 literal).
        """
        settings = MagicMock()
        settings.probe_connect_timeout = 0.75

        with (
            patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter,
            patch("baldur.settings.redis.get_redis_settings", return_value=settings),
        ):
            mock_instance = MagicMock()
            mock_instance.raw_client.ping.return_value = True
            MockAdapter.return_value = mock_instance
            backend._ensure_redis()

        assert MockAdapter.call_args.kwargs["socket_connect_timeout"] == 0.75

    def test_ensure_redis_transitions_to_redis_on_success(self, backend):
        """Successful probe transitions DEGRADED -> REDIS."""
        from baldur.adapters.resilient.backend import ResilientStorageMode

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            mock_instance = MagicMock()
            mock_instance._redis.ping.return_value = True
            MockAdapter.return_value = mock_instance

            result = backend._ensure_redis()

        assert result is True
        assert backend._redis_initialized is True
        assert backend.mode == ResilientStorageMode.REDIS

    def test_ensure_redis_stays_degraded_on_failure(self, backend):
        """Failed probe keeps DEGRADED mode."""
        from baldur.adapters.resilient.backend import ResilientStorageMode

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = ConnectionError("Connection refused")
            result = backend._ensure_redis()

        assert result is False
        assert backend._redis_initialized is False
        assert backend.mode == ResilientStorageMode.DEGRADED

    # -- Time dependency: cooldown --

    def test_cooldown_blocks_retry_within_interval(self, backend):
        """After failure, calls within cooldown return False without I/O."""
        # Given — first probe fails, setting cooldown
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = ConnectionError("Connection refused")
            backend._ensure_redis()

        assert backend._next_redis_probe > 0.0

        # When — second attempt within cooldown
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter2:
            result = backend._ensure_redis()

        # Then — no connection attempt, returns False
        assert result is False
        MockAdapter2.assert_not_called()

    def test_retry_allowed_after_cooldown_expires(self, backend):
        """After cooldown expires, _ensure_redis() retries connection."""
        # Given — first probe fails
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = ConnectionError("Connection refused")
            backend._ensure_redis()

        # When — expire cooldown, then retry
        backend._next_redis_probe = 0.0

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter2:
            mock_instance = MagicMock()
            mock_instance._redis.ping.return_value = True
            MockAdapter2.return_value = mock_instance
            result = backend._ensure_redis()

        # Then — connection attempt succeeds
        assert result is True
        MockAdapter2.assert_called_once()

    # -- Dependency interaction: WAL recovery --

    def test_triggers_wal_recovery_on_connect(self, backend):
        """D5: WAL recovery runs after successful lazy Redis connect."""
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            mock_instance = MagicMock()
            mock_instance._redis.ping.return_value = True
            MockAdapter.return_value = mock_instance

            with patch.object(backend, "_recover_from_wal_on_startup") as mock_recovery:
                backend._ensure_redis()

        mock_recovery.assert_called_once()

    def test_skips_wal_recovery_on_failure(self, backend):
        """WAL recovery must NOT run when Redis probe fails."""
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = ConnectionError("Connection refused")

            with patch.object(backend, "_recover_from_wal_on_startup") as mock_recovery:
                backend._ensure_redis()

        mock_recovery.assert_not_called()

    # -- Side effects: logging --

    def test_logs_critical_once_on_first_failure(self, temp_wal_dir):
        """D10: One-time CRITICAL log on first failure (allow_memory_only=False)."""
        from baldur.adapters.resilient.backend import ResilientStorageBackend
        from baldur.settings.resilient_storage import ResilientStorageSettings

        config = ResilientStorageSettings(
            wal_dir=temp_wal_dir,
            allow_memory_only=False,
        )
        backend = ResilientStorageBackend(config)

        try:
            with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
                MockAdapter.side_effect = ConnectionError("Connection refused")

                with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
                    # When — first failure
                    backend._ensure_redis()

                    # Then — CRITICAL logged once
                    mock_logger.critical.assert_called_once()
                    assert backend._degraded_critical_logged is True

                    # When — expire cooldown, second failure
                    backend._next_redis_probe = 0.0
                    mock_logger.reset_mock()
                    backend._ensure_redis()

                    # Then — no second CRITICAL
                    mock_logger.critical.assert_not_called()
        finally:
            backend.close()

    def test_skips_critical_log_when_allow_memory_only(self, backend):
        """No CRITICAL log when allow_memory_only=True."""
        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            MockAdapter.side_effect = ConnectionError("Connection refused")

            with patch("baldur.adapters.resilient.backend.logger") as mock_logger:
                backend._ensure_redis()
                mock_logger.critical.assert_not_called()

    # -- Dependency interaction: negative cache --

    def test_respects_redis_negative_cache(self, backend):
        """D4: Skips probe when the runtime Redis negative cache is active."""
        from baldur.adapters.redis import _redis_state

        state = _redis_state()
        state.unavailable = True
        state.fail_time = time.monotonic()

        with patch("baldur.adapters.cache.RedisCacheAdapter") as MockAdapter:
            result = backend._ensure_redis()

        assert result is False
        MockAdapter.assert_not_called()

    # -- Core Operations integration --

    def test_all_core_operations_call_ensure_redis(self, backend):
        """All 15 Core Operations invoke _ensure_redis() before processing."""
        operations = [
            ("get", ("k_get",)),
            ("set", ("k_set", "value")),
            ("delete", ("k_del",)),
            ("hget", ("k_hget", "field")),
            ("hset", ("k_hset", {"f": "v"})),
            ("hgetall", ("k_hgetall",)),
            ("hdel", ("k_hdel", "field")),
            ("lpush", ("k_lpush", "value")),
            ("lrange", ("k_lrange", 0, 10)),
            ("ltrim", ("k_ltrim", 0, 5)),
            ("zadd", ("k_zadd", {"m": 1.0})),
            ("zrange", ("k_zrange", 0, 10)),
            ("zrem", ("k_zrem", "member")),
            ("zcard", ("k_zcard",)),
            ("incr", ("k_incr",)),
        ]

        for op_name, args in operations:
            with patch.object(
                backend, "_ensure_redis", return_value=False
            ) as mock_ensure:
                getattr(backend, op_name)(*args)
                assert mock_ensure.called, f"{op_name}() did not call _ensure_redis()"

    # -- Concurrency: thread safety --

    def test_concurrent_ensure_redis_initializes_once(self, backend):
        """DCL: concurrent calls initialize Redis exactly once."""
        init_count = {"value": 0}
        barrier = threading.Barrier(5, timeout=5.0)

        def counting_adapter(*args, **kwargs):
            init_count["value"] += 1
            mock_instance = MagicMock()
            mock_instance._redis.ping.return_value = True
            return mock_instance

        with patch(
            "baldur.adapters.cache.RedisCacheAdapter",
            side_effect=counting_adapter,
        ):

            def worker():
                barrier.wait()
                backend._ensure_redis()

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        assert init_count["value"] == 1
        assert backend._redis_initialized is True
