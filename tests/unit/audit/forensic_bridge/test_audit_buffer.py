"""
Audit Buffer 테스트.

테스트 대상:
- TestInMemoryAuditBuffer: 메모리 버퍼 폴백
- TestRedisAuditBuffer: Redis Audit Buffer
"""

import json
from unittest.mock import MagicMock, patch


class TestRedisAuditBuffer:
    """Redis Audit Buffer 테스트."""

    def test_log_success(self):
        """Redis 기록 성공."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        # Mock Redis
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        result = buffer.log({"event_type": "TEST"}, domain="test")

        assert result is True
        mock_redis.pipeline.assert_called_once()
        mock_pipe.lpush.assert_called_once()
        mock_pipe.expire.assert_called_once()
        mock_pipe.execute.assert_called_once()

    def test_log_failure_uses_fallback(self):
        """Redis 실패 시 폴백 사용."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = Exception("Redis down")
        mock_redis.pipeline.return_value = mock_pipe

        # spec을 사용하여 log_raw가 없는 fallback 시뮬레이션
        mock_fallback = MagicMock(spec=["log"])

        buffer = RedisAuditBuffer(
            redis_client=mock_redis,
            fallback_adapter=mock_fallback,
        )

        result = buffer.log({"event_type": "TEST"})

        assert result is False
        mock_fallback.log.assert_called_once()

    def test_on_fallback_callback(self):
        """폴백 콜백 호출."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = Exception("Connection refused")
        mock_redis.pipeline.return_value = mock_pipe

        callback_called = []

        def on_fallback(e):
            callback_called.append(str(e))

        buffer = RedisAuditBuffer(
            redis_client=mock_redis,
            on_fallback=on_fallback,
        )

        buffer.log({"event_type": "TEST"})

        assert len(callback_called) == 1
        assert "Connection refused" in callback_called[0]

    def test_consecutive_failures_tracking(self):
        """연속 실패 추적."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = Exception("Error")
        mock_redis.pipeline.return_value = mock_pipe

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        buffer.log({"event_type": "TEST1"})
        buffer.log({"event_type": "TEST2"})
        buffer.log({"event_type": "TEST3"})

        stats = buffer.get_buffer_stats()
        assert stats["consecutive_failures"] == 3
        assert stats["total_fallbacks"] == 3

    def test_success_resets_failure_count(self):
        """성공 시 실패 카운트 리셋."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        # 수동으로 failure 설정
        buffer._consecutive_failures = 5

        buffer.log({"event_type": "TEST"})

        assert buffer._consecutive_failures == 0

    def test_should_use_fallback(self):
        """폴백 사용 여부 판단."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        buffer = RedisAuditBuffer(redis_client=mock_redis)

        buffer._consecutive_failures = 2
        assert buffer.should_use_fallback() is False

        buffer._consecutive_failures = 3
        assert buffer.should_use_fallback() is True

    def test_is_healthy(self):
        """Redis 연결 상태 확인."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        buffer = RedisAuditBuffer(redis_client=mock_redis)

        # Healthy
        mock_redis.ping.return_value = True
        assert buffer.is_healthy() is True

        # Unhealthy
        mock_redis.ping.side_effect = Exception("Connection lost")
        assert buffer.is_healthy() is False

    def test_get_pending_count(self):
        """대기 엔트리 수 조회."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_redis.llen.return_value = 42

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        count = buffer.get_pending_count("test")

        assert count == 42
        mock_redis.llen.assert_called_with("audit:{test}:buffer")

    def test_flush_to_external_safe(self):
        """Processing-Queue 안전 플러시 (600 D2: flush_to_external removed)."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()

        # lrange returns the 2 items moved into the processing queue
        mock_redis.lrange.return_value = [
            json.dumps(
                {
                    "entry": {"event": "e1"},
                    "timestamp": "2026-01-08T00:00:00Z",
                    "instance_id": "test",
                }
            ),
            json.dumps(
                {
                    "entry": {"event": "e2"},
                    "timestamp": "2026-01-08T00:00:01Z",
                    "instance_id": "test",
                }
            ),
        ]

        # Atomic Lua move/complete: 2 entries moved, then completed
        mock_lua = MagicMock()
        mock_lua.atomic_batch_move.return_value = 2

        # spec without log_raw/log_batch forces the per-entry .log() path
        mock_target = MagicMock(spec=["log"])

        buffer = RedisAuditBuffer(redis_client=mock_redis)
        with patch.object(buffer, "_get_lua_scripts", return_value=mock_lua):
            flushed = buffer.flush_to_external_safe(mock_target, domain="test")

        assert flushed == 2
        assert mock_target.log.call_count == 2
        mock_lua.atomic_batch_complete.assert_called_once_with("test", 2)

    def test_clear_domain(self):
        """도메인 삭제."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_redis.llen.return_value = 5

        buffer = RedisAuditBuffer(redis_client=mock_redis)

        count = buffer.clear_domain("test")

        assert count == 5
        mock_redis.delete.assert_called_with("audit:{test}:buffer")

    def test_custom_key_prefix(self):
        """커스텀 키 프리픽스."""
        from baldur.adapters.audit.redis_buffer import RedisAuditBuffer

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        buffer = RedisAuditBuffer(
            redis_client=mock_redis,
            key_prefix="custom:audit:",
        )

        buffer.log({"event": "test"}, domain="myapp")

        # lpush가 custom:audit:myapp 키로 호출되었는지 확인
        call_args = mock_pipe.lpush.call_args
        assert "custom:audit:{myapp}:buffer" in str(call_args)

    # NOTE: test_factory_function_no_redis는 실제 Redis 연결을 시도하므로
    # tests/integration/baldur/test_regional_gate_integration.py로 이동됨
