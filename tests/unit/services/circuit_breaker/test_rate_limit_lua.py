"""
Tests for Redis Rate Limit Backend (Lua-based)

Covers:
- RedisRateLimitBackend key format
- Lua script registration
- record/count operations via LuaScriptRegistry
- Backoff get/increment/reset via Redis STRING
- clear_service multi-key cleanup
"""

from unittest.mock import MagicMock, patch

# =============================================================================
# Contract: Key Format, Lua Registration
# =============================================================================


class TestRedisRateLimitBackendContract:
    """Contract tests for RedisRateLimitBackend structure and constants."""

    def test_key_prefix(self):
        """Key prefix is 'baldur:rl:'."""
        from baldur.services.circuit_breaker.rate_limit_lua import (
            RedisRateLimitBackend,
        )

        assert RedisRateLimitBackend._KEY_PREFIX == "baldur:rl:"

    def test_key_format_429(self):
        """429 key format is 'baldur:rl:429:{service}'."""
        with patch("baldur.audit.performance.lua_registry.LuaScriptRegistry"):
            from baldur.services.circuit_breaker.rate_limit_lua import (
                RedisRateLimitBackend,
            )

            backend = RedisRateLimitBackend.__new__(RedisRateLimitBackend)
            backend._redis = MagicMock()
            backend._registry = MagicMock()

            key = backend._key("429", "payment-api")

        assert key == "baldur:rl:429:payment-api"

    def test_key_format_req(self):
        """Request key format is 'baldur:rl:req:{service}'."""
        from baldur.services.circuit_breaker.rate_limit_lua import (
            RedisRateLimitBackend,
        )

        backend = RedisRateLimitBackend.__new__(RedisRateLimitBackend)
        key = backend._key("req", "payment-api")

        assert key == "baldur:rl:req:payment-api"

    def test_key_format_backoff(self):
        """Backoff key format is 'baldur:rl:backoff:{service}'."""
        from baldur.services.circuit_breaker.rate_limit_lua import (
            RedisRateLimitBackend,
        )

        backend = RedisRateLimitBackend.__new__(RedisRateLimitBackend)
        key = backend._key("backoff", "payment-api")

        assert key == "baldur:rl:backoff:payment-api"

    def test_lua_scripts_registered_on_init(self):
        """Init registers both Lua scripts via LuaScriptRegistry."""
        mock_redis = MagicMock()
        mock_registry = MagicMock()

        with patch(
            "baldur.audit.performance.lua_registry.LuaScriptRegistry",
            return_value=mock_registry,
        ):
            from baldur.services.circuit_breaker.rate_limit_lua import (
                RedisRateLimitBackend,
            )

            RedisRateLimitBackend(mock_redis)

        mock_registry.register.assert_any_call(
            "rl_record_and_count",
            RedisRateLimitBackend.__module__
            and mock_registry.register.call_args_list[0][0][1],
        )
        assert mock_registry.register.call_count == 2

    def test_lua_record_and_count_script_content(self):
        """Record-and-count script contains ZADD, ZREMRANGEBYSCORE, ZCARD, EXPIRE."""
        from baldur.services.circuit_breaker.rate_limit_lua import (
            LUA_RECORD_AND_COUNT,
        )

        assert "ZADD" in LUA_RECORD_AND_COUNT
        assert "ZREMRANGEBYSCORE" in LUA_RECORD_AND_COUNT
        assert "ZCARD" in LUA_RECORD_AND_COUNT
        assert "EXPIRE" in LUA_RECORD_AND_COUNT

    def test_lua_count_in_window_script_content(self):
        """Count-in-window script contains ZREMRANGEBYSCORE, ZCARD."""
        from baldur.services.circuit_breaker.rate_limit_lua import (
            LUA_COUNT_IN_WINDOW,
        )

        assert "ZREMRANGEBYSCORE" in LUA_COUNT_IN_WINDOW
        assert "ZCARD" in LUA_COUNT_IN_WINDOW


# =============================================================================
# Behavior: Record & Count Operations
# =============================================================================


class TestRedisRateLimitBackendBehavior:
    """Behavior tests for RedisRateLimitBackend operations."""

    def _make_backend(self):
        mock_redis = MagicMock()
        mock_registry = MagicMock()

        with patch(
            "baldur.audit.performance.lua_registry.LuaScriptRegistry",
            return_value=mock_registry,
        ):
            from baldur.services.circuit_breaker.rate_limit_lua import (
                RedisRateLimitBackend,
            )

            backend = RedisRateLimitBackend(mock_redis)

        return backend, mock_redis, mock_registry

    def test_record_rate_limit_calls_lua_script(self):
        """record_rate_limit executes rl_record_and_count with 429 key."""
        backend, _, mock_registry = self._make_backend()
        mock_registry.execute.return_value = 5

        with patch("time.time", return_value=1000.0):
            result = backend.record_rate_limit("svc")

        assert result == 5
        mock_registry.execute.assert_called_once()
        call_args = mock_registry.execute.call_args
        assert call_args[0][0] == "rl_record_and_count"
        assert call_args[1]["keys"] == ["baldur:rl:429:svc"]
        args = call_args[1]["args"]
        assert args[0] == 1000.0
        assert args[1] == 120  # default retention
        assert args[2] == 180  # default ttl
        assert isinstance(args[3], str)  # unique member

    def test_record_request_calls_lua_script(self):
        """record_request executes rl_record_and_count with req key."""
        backend, _, mock_registry = self._make_backend()
        mock_registry.execute.return_value = 10

        with patch("time.time", return_value=2000.0):
            result = backend.record_request("svc")

        assert result == 10
        mock_registry.execute.assert_called_once()
        call_args = mock_registry.execute.call_args
        assert call_args[0][0] == "rl_record_and_count"
        assert call_args[1]["keys"] == ["baldur:rl:req:svc"]
        args = call_args[1]["args"]
        assert args[0] == 2000.0
        assert args[1] == 120
        assert args[2] == 180
        assert isinstance(args[3], str)

    def test_get_rate_limit_count_calls_count_script(self):
        """get_rate_limit_count executes rl_count_in_window with 429 key."""
        backend, _, mock_registry = self._make_backend()
        mock_registry.execute.return_value = 7

        with patch("time.time", return_value=1060.0):
            result = backend.get_rate_limit_count("svc", 60)

        assert result == 7
        mock_registry.execute.assert_called_once_with(
            "rl_count_in_window",
            keys=["baldur:rl:429:svc"],
            args=[1000.0],  # 1060 - 60
        )

    def test_get_request_count_calls_count_script(self):
        """get_request_count executes rl_count_in_window with req key."""
        backend, _, mock_registry = self._make_backend()
        mock_registry.execute.return_value = 20

        with patch("time.time", return_value=1120.0):
            result = backend.get_request_count("svc", 60)

        assert result == 20
        mock_registry.execute.assert_called_once_with(
            "rl_count_in_window",
            keys=["baldur:rl:req:svc"],
            args=[1060.0],  # 1120 - 60
        )

    # ---- Backoff via Redis STRING ----

    def test_get_backoff_level_none_returns_zero(self):
        """Missing backoff key returns 0."""
        backend, mock_redis, _ = self._make_backend()
        mock_redis.get.return_value = None

        level = backend.get_backoff_level("svc")

        assert level == 0
        mock_redis.get.assert_called_once_with("baldur:rl:backoff:svc")

    def test_get_backoff_level_returns_stored_value(self):
        """Existing backoff key returns its integer value."""
        backend, mock_redis, _ = self._make_backend()
        mock_redis.get.return_value = b"3"

        level = backend.get_backoff_level("svc")

        assert level == 3

    def test_increment_backoff_uses_incr(self):
        """Increment backoff uses Redis INCR command and sets TTL."""
        backend, mock_redis, _ = self._make_backend()
        mock_redis.incr.return_value = 4

        level = backend.increment_backoff("svc")

        assert level == 4
        mock_redis.incr.assert_called_once_with("baldur:rl:backoff:svc")
        mock_redis.expire.assert_called_once_with("baldur:rl:backoff:svc", 3600)

    def test_reset_backoff_deletes_key(self):
        """Reset backoff deletes the backoff key."""
        backend, mock_redis, _ = self._make_backend()

        backend.reset_backoff("svc")

        mock_redis.delete.assert_called_once_with("baldur:rl:backoff:svc")

    # ---- Clear service ----

    def test_clear_service_deletes_all_three_keys(self):
        """Clear service deletes 429, req, and backoff keys."""
        backend, mock_redis, _ = self._make_backend()

        backend.clear_service("svc")

        expected_keys = [
            "baldur:rl:429:svc",
            "baldur:rl:req:svc",
            "baldur:rl:backoff:svc",
        ]
        assert mock_redis.delete.call_count == 3
        for key in expected_keys:
            mock_redis.delete.assert_any_call(key)

    def test_clear_service_ignores_individual_key_errors(self):
        """Individual key deletion errors do not prevent other deletions."""
        backend, mock_redis, _ = self._make_backend()
        mock_redis.delete.side_effect = [
            ConnectionError("gone"),
            None,
            None,
        ]

        # Should not raise
        backend.clear_service("svc")

        assert mock_redis.delete.call_count == 3

    def test_custom_retention_ttl_passed_to_lua(self):
        """Custom retention and ttl are passed to Lua args."""
        mock_redis = MagicMock()
        mock_registry = MagicMock()
        mock_registry.execute.return_value = 1

        with patch(
            "baldur.audit.performance.lua_registry.LuaScriptRegistry",
            return_value=mock_registry,
        ):
            from baldur.services.circuit_breaker.rate_limit_lua import (
                RedisRateLimitBackend,
            )

            backend = RedisRateLimitBackend(
                mock_redis, retention_seconds=600, ttl_seconds=660
            )

        with patch("time.time", return_value=5000.0):
            backend.record_rate_limit("svc")

        args = mock_registry.execute.call_args[1]["args"]
        assert args[1] == 600
        assert args[2] == 660


class TestUniqueMemberBehavior:
    """Verify _unique_member generates distinct values."""

    def test_unique_member_produces_unique_values(self):
        """Consecutive calls produce distinct member IDs."""
        from baldur.services.circuit_breaker.rate_limit_lua import _unique_member

        members = {_unique_member() for _ in range(100)}
        assert len(members) == 100

    def test_unique_member_contains_pid(self):
        """Member includes process ID for cross-process uniqueness."""
        import os

        from baldur.services.circuit_breaker.rate_limit_lua import _unique_member

        member = _unique_member()
        assert str(os.getpid()) in member
