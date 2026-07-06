"""
Unit tests for RedisCacheAdapter push_limit/list_range (414).

Tests Redis pipeline interactions via mocked Redis client.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, call

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter

# =============================================================================
# push_limit — Behavior Tests
# =============================================================================


class TestRedisCachePushLimitBehavior:
    """Behavior: push_limit issues RPUSH+LTRIM+EXPIRE pipeline."""

    def _make_adapter(self, mock_redis: MagicMock) -> RedisCacheAdapter:
        """Create RedisCacheAdapter with injected mock client."""
        return RedisCacheAdapter(client=mock_redis, key_prefix="test:")

    def test_push_limit_pipeline_command_order(self):
        """push_limit executes RPUSH → LTRIM → EXPIRE in a single pipeline."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [3, True, True]

        adapter = self._make_adapter(mock_redis)
        adapter.push_limit("mykey", {"data": 1}, max_len=100, ttl=timedelta(seconds=60))

        mock_redis.pipeline.assert_called_once()
        assert mock_pipe.rpush.call_count == 1
        assert mock_pipe.ltrim.call_count == 1
        assert mock_pipe.expire.call_count == 1

        # Verify LTRIM args: keep last max_len entries
        ltrim_args = mock_pipe.ltrim.call_args
        assert ltrim_args == call("test:mykey", -100, -1)

        # Verify EXPIRE args
        expire_args = mock_pipe.expire.call_args
        assert expire_args == call("test:mykey", 60)

    def test_push_limit_serializes_value(self):
        """push_limit serializes value before RPUSH (same as hset pattern)."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [1, True]

        adapter = self._make_adapter(mock_redis)
        adapter.push_limit("k", {"hello": "world"}, max_len=10)

        rpush_args = mock_pipe.rpush.call_args[0]
        assert rpush_args[0] == "test:k"
        # Second arg should be bytes (serialized)
        assert isinstance(rpush_args[1], bytes)

    def test_push_limit_returns_rpush_result_as_pre_trim_length(self):
        """push_limit returns the RPUSH result (pre-trim list length)."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [42, True, True]

        adapter = self._make_adapter(mock_redis)
        result = adapter.push_limit("k", "v", max_len=10, ttl=timedelta(seconds=30))

        assert result == 42

    def test_push_limit_without_ttl_skips_expire(self):
        """push_limit with ttl=None does not call EXPIRE in pipeline."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [1, True]

        adapter = self._make_adapter(mock_redis)
        adapter.push_limit("k", "v", max_len=10)

        mock_pipe.rpush.assert_called_once()
        mock_pipe.ltrim.assert_called_once()
        mock_pipe.expire.assert_not_called()

    def test_push_limit_returns_zero_on_error(self):
        """push_limit returns 0 when Redis pipeline raises exception."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.side_effect = Exception("Redis error")

        adapter = self._make_adapter(mock_redis)
        result = adapter.push_limit("k", "v", max_len=10)

        assert result == 0

    def test_push_limit_records_operation_error_on_swallow(self):
        """push_limit increments cache_operation_errors_total{backend=redis,operation=push_limit}
        in its swallow branch (#415 — adapter-level visibility for swallowed errors).
        """
        from unittest.mock import patch as patch_

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.side_effect = Exception("Redis error")

        adapter = self._make_adapter(mock_redis)

        with patch_(
            "baldur.metrics.drift_metrics.record_cache_operation_error"
        ) as mock_record:
            adapter.push_limit("k", "v", max_len=10)

        mock_record.assert_called_once_with(backend="redis", operation="push_limit")

    def test_push_limit_uses_key_prefix(self):
        """push_limit applies adapter's key prefix to the key."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe
        mock_pipe.execute.return_value = [1, True]

        adapter = RedisCacheAdapter(client=mock_redis, key_prefix="app:")
        adapter.push_limit("report:2026-04-05", "v", max_len=5)

        rpush_key = mock_pipe.rpush.call_args[0][0]
        assert rpush_key == "app:report:2026-04-05"


# =============================================================================
# list_range — Behavior Tests
# =============================================================================


class TestRedisCacheListRangeBehavior:
    """Behavior: list_range issues LRANGE and deserializes results."""

    def _make_adapter(self, mock_redis: MagicMock) -> RedisCacheAdapter:
        return RedisCacheAdapter(client=mock_redis, key_prefix="test:")

    def test_list_range_calls_lrange_with_correct_args(self):
        """list_range delegates to redis LRANGE with prefixed key."""
        mock_redis = MagicMock()
        mock_redis.lrange.return_value = []

        adapter = self._make_adapter(mock_redis)
        adapter.list_range("mylist", 0, -1)

        mock_redis.lrange.assert_called_once_with("test:mylist", 0, -1)

    def test_list_range_deserializes_each_item(self):
        """list_range deserializes each raw item from LRANGE."""
        from baldur.utils.serialization import fast_dumps

        mock_redis = MagicMock()
        items = [{"a": 1}, {"b": 2}]
        mock_redis.lrange.return_value = [fast_dumps(i, default=str) for i in items]

        adapter = self._make_adapter(mock_redis)
        result = adapter.list_range("k", 0, -1)

        assert result == items

    def test_list_range_returns_empty_on_error(self):
        """list_range returns empty list when LRANGE raises exception."""
        mock_redis = MagicMock()
        mock_redis.lrange.side_effect = Exception("Redis error")

        adapter = self._make_adapter(mock_redis)
        result = adapter.list_range("k", 0, -1)

        assert result == []

    def test_list_range_records_operation_error_on_swallow(self):
        """list_range increments cache_operation_errors_total{backend=redis,operation=list_range}
        in its swallow branch (#415 — adapter-level visibility for swallowed errors).
        """
        from unittest.mock import patch as patch_

        mock_redis = MagicMock()
        mock_redis.lrange.side_effect = Exception("Redis error")

        adapter = self._make_adapter(mock_redis)

        with patch_(
            "baldur.metrics.drift_metrics.record_cache_operation_error"
        ) as mock_record:
            adapter.list_range("k", 0, -1)

        mock_record.assert_called_once_with(backend="redis", operation="list_range")

    def test_list_range_handles_deserialization_failure_gracefully(self):
        """list_range keeps raw item when deserialization fails."""
        mock_redis = MagicMock()
        mock_redis.lrange.return_value = [b"not-valid-msgpack"]

        adapter = self._make_adapter(mock_redis)
        result = adapter.list_range("k", 0, -1)

        assert len(result) == 1
        assert result[0] == b"not-valid-msgpack"
