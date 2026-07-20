"""
Lua scripts for Redis audit batch processing.

Prevents data loss via the Buffer → Processing Queue → External pattern.
All Lua scripts execute atomically inside Redis.
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
    Lua script collection for audit batch processing.

    Processing Queue pattern:
    1. Atomic move from Buffer Queue to Processing Queue
    2. Send the Processing Queue data to external storage
    3. On success clear the Processing Queue; on failure restore to Buffer
    """

    # Atomic batch move: Buffer → Processing Queue
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

    # Cleanup after Processing Queue completion
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

    # On failure, restore Processing Queue → Buffer Queue (order preserved)
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
        Initialize AuditBatchLuaScripts.

        Args:
            redis_client: Redis client
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
        Atomic move from the Buffer Queue to the Processing Queue.

        Args:
            domain: Domain name
            batch_size: Number of items to move
            worker_id: Processing worker identifier

        Returns:
            Number of items actually moved
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
        Remove completed items from the Processing Queue.

        Args:
            domain: Domain name
            count: Number of items to remove

        Returns:
            Number of items actually removed
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
        Restore Processing Queue items to the Buffer Queue (order preserved).

        Call on failure to prevent data loss.

        Args:
            domain: Domain name

        Returns:
            Number of items restored
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
        Look up timed-out orphaned Processing Queues.

        Args:
            timeout_seconds: Age threshold for orphan detection (default 5 min)

        Returns:
            List of (processing_key, worker_id, age_seconds) tuples
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
                    # Malformed metadata
                    orphaned.append((key_str, "unknown", timeout_seconds + 1))

        except redis_lib.RedisError as e:
            logger.exception(
                "audit_batch_lua_scripts.get_orphaned_queues_failed",
                error=e,
            )

        return orphaned
