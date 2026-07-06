"""
Redis Audit 배치 처리용 Lua 스크립트.

Buffer → Processing Queue → External 패턴을 통한 데이터 손실 방지.
모든 Lua 스크립트는 Redis에서 원자적으로 실행됩니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import redis as redis_lib
import structlog

if TYPE_CHECKING:
    from redis import Redis

logger = structlog.get_logger()


class AuditBatchLuaScripts:
    """
    Audit 배치 처리용 Lua 스크립트 모음.

    Processing Queue 패턴:
    1. Buffer Queue에서 Processing Queue로 원자적 이동
    2. Processing Queue의 데이터를 외부 저장소로 전송
    3. 성공 시 Processing Queue 정리, 실패 시 Buffer로 복원
    """

    # Buffer → Processing Queue 원자적 배치 이동
    LUA_ATOMIC_BATCH_MOVE = """
    -- KEYS[1] = audit:buffer:{domain}
    -- KEYS[2] = audit:processing:{domain}
    -- ARGV[1] = batch_size
    -- ARGV[2] = worker_id

    local batch_size = tonumber(ARGV[1])
    local worker_id = ARGV[2]
    local moved = 0

    for i = 1, batch_size do
        local item = redis.call('RPOPLPUSH', KEYS[1], KEYS[2])
        if not item then
            break
        end
        moved = moved + 1
    end

    if moved > 0 then
        redis.call('HSET', 'audit:processing:meta',
                   KEYS[2], worker_id .. ':' .. redis.call('TIME')[1])
    end

    return moved
    """

    # Processing Queue 처리 완료 후 정리
    LUA_ATOMIC_BATCH_COMPLETE = """
    -- KEYS[1] = audit:processing:{domain}
    -- ARGV[1] = count

    local count = tonumber(ARGV[1])
    local removed = 0

    for i = 1, count do
        local item = redis.call('RPOP', KEYS[1])
        if not item then
            break
        end
        removed = removed + 1
    end

    if redis.call('LLEN', KEYS[1]) == 0 then
        redis.call('HDEL', 'audit:processing:meta', KEYS[1])
    end

    return removed
    """

    # 실패 시 Processing Queue → Buffer Queue 복원 (순서 보존)
    LUA_ATOMIC_BATCH_RESTORE = """
    -- KEYS[1] = audit:processing:{domain}
    -- KEYS[2] = audit:buffer:{domain}

    local restored = 0

    while true do
        local item = redis.call('LPOP', KEYS[1])
        if not item then
            break
        end
        redis.call('RPUSH', KEYS[2], item)
        restored = restored + 1
    end

    redis.call('HDEL', 'audit:processing:meta', KEYS[1])

    return restored
    """

    def __init__(self, redis_client: Redis):
        """
        AuditBatchLuaScripts 초기화.

        Args:
            redis_client: Redis 클라이언트
        """
        from baldur.audit.performance.lua_registry import LuaScriptRegistry

        self._redis = redis_client
        self._registry = LuaScriptRegistry(redis_client)
        self._registry.register("batch_move", self.LUA_ATOMIC_BATCH_MOVE)
        self._registry.register("batch_complete", self.LUA_ATOMIC_BATCH_COMPLETE)
        self._registry.register("batch_restore", self.LUA_ATOMIC_BATCH_RESTORE)

    def atomic_batch_move(
        self,
        domain: str,
        batch_size: int,
        worker_id: str,
    ) -> int:
        """
        Buffer Queue에서 Processing Queue로 원자적 이동.

        Args:
            domain: 도메인 이름
            batch_size: 이동할 항목 수
            worker_id: 처리 워커 식별자

        Returns:
            실제 이동된 항목 수
        """
        buffer_key = f"audit:{{{domain}}}:buffer"
        processing_key = f"audit:{{{domain}}}:processing"

        result = self._registry.execute(
            "batch_move",
            keys=[buffer_key, processing_key],
            args=[batch_size, worker_id],
        )
        return int(result) if result else 0

    def atomic_batch_complete(self, domain: str, count: int) -> int:
        """
        Processing Queue에서 처리 완료된 항목 제거.

        Args:
            domain: 도메인 이름
            count: 제거할 항목 수

        Returns:
            실제 제거된 항목 수
        """
        processing_key = f"audit:{{{domain}}}:processing"

        result = self._registry.execute(
            "batch_complete",
            keys=[processing_key],
            args=[count],
        )
        return int(result) if result else 0

    def atomic_batch_restore(self, domain: str) -> int:
        """
        Processing Queue의 항목을 Buffer Queue로 복원 (순서 보존).

        실패 시 호출하여 데이터 손실 방지.

        Args:
            domain: 도메인 이름

        Returns:
            복원된 항목 수
        """
        processing_key = f"audit:{{{domain}}}:processing"
        buffer_key = f"audit:{{{domain}}}:buffer"

        result = self._registry.execute(
            "batch_restore",
            keys=[processing_key, buffer_key],
            args=[],
        )
        return int(result) if result else 0

    def get_orphaned_processing_queues(
        self,
        timeout_seconds: int = 300,
    ) -> list[tuple[str, str, int]]:
        """
        타임아웃된 고아 Processing Queue 조회.

        Args:
            timeout_seconds: 고아 판단 임계 시간 (기본 5분)

        Returns:
            (processing_key, worker_id, age_seconds) 튜플 리스트
        """
        import time

        orphaned = []
        try:
            from typing import cast

            processing_meta = cast(
                dict[Any, Any], self._redis.hgetall("audit:processing:meta")
            )  # sync Redis client: redis-py dual stub returns dict, not Awaitable
            current_time = int(time.time())

            for processing_key, worker_info in processing_meta.items():
                key_str = (
                    processing_key.decode()
                    if isinstance(processing_key, bytes)
                    else processing_key
                )
                info_str = (
                    worker_info.decode()
                    if isinstance(worker_info, bytes)
                    else worker_info
                )

                try:
                    worker_id, timestamp_str = info_str.rsplit(":", 1)
                    age = current_time - int(timestamp_str)

                    if age > timeout_seconds:
                        orphaned.append((key_str, worker_id, age))
                except (ValueError, TypeError):
                    # 잘못된 형식의 메타 데이터
                    orphaned.append((key_str, "unknown", timeout_seconds + 1))

        except redis_lib.RedisError as e:
            logger.exception(
                "audit_batch_lua_scripts.get_orphaned_queues_failed",
                error=e,
            )

        return orphaned
