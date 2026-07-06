"""
Lua Script Atomic Hash Chain (5 RTT → 1 RTT).

Provides atomic hash chain operations using Redis Lua scripts.
"""

from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class LuaAtomicHashChain:
    """
    Lua Script based atomic hash chain operations.

    Problem:
        Python-side operations require 5 round trips:
        1. INCR sequence
        2. GET previous_hash
        3. HSET pending entry
        4. HSET chain state
        5. DEL pending entry

    Solution:
        Execute all operations in a single Lua script server-side.
        Redis guarantees atomic execution within a Lua script.

    Effect:
        5 RTT → 1 RTT (80% reduction in network latency)

    Usage:
        lua_chain = LuaAtomicHashChain(redis_client)
        result = lua_chain.add_integrity_atomic(entry_data, expected_hash)
    """

    # Lua script for atomic sequence allocation + state update
    LUA_ATOMIC_ADD_INTEGRITY = """
    -- KEYS[1] = seq_key (audit:hash_chain:seq)
    -- KEYS[2] = state_key (audit:hash_chain:state)
    -- KEYS[3] = pending_key (audit:hash_chain:pending:{seq})
    -- ARGV[1] = expected_hash
    -- ARGV[2] = previous_hash
    -- ARGV[3] = timestamp
    -- ARGV[4] = pending_ttl_seconds

    -- 1. Atomically increment sequence
    local new_seq = redis.call('INCR', KEYS[1])

    -- 2. Get current previous_hash for validation
    local stored_prev = redis.call('HGET', KEYS[2], 'previous_hash')
    if stored_prev and stored_prev ~= ARGV[2] then
        -- Rollback sequence on mismatch
        redis.call('DECR', KEYS[1])
        return {err='PREV_HASH_MISMATCH', expected=ARGV[2], found=stored_prev}
    end

    -- 3. Set PENDING state with TTL
    local pending_key = KEYS[3] .. ':' .. tostring(new_seq)
    redis.call('HSET', pending_key,
               'expected_hash', ARGV[1],
               'previous_hash', ARGV[2],
               'reserved_at', ARGV[3])
    redis.call('EXPIRE', pending_key, tonumber(ARGV[4]))

    -- 4. Return allocated sequence
    return {seq=new_seq, prev_hash=(stored_prev or 'GENESIS')}
    """

    # Lua script for atomic commit (clear pending + update state)
    LUA_ATOMIC_COMMIT = """
    -- KEYS[1] = pending_key
    -- KEYS[2] = state_key
    -- ARGV[1] = sequence
    -- ARGV[2] = new_hash
    -- ARGV[3] = timestamp

    -- 1. Verify pending entry exists
    local exists = redis.call('EXISTS', KEYS[1])
    if exists == 0 then
        return {err='PENDING_NOT_FOUND'}
    end

    -- 2. Verify expected hash matches (tamper detection)
    local expected = redis.call('HGET', KEYS[1], 'expected_hash')
    if expected and expected ~= ARGV[2] then
        return {err='HASH_MISMATCH', expected=expected, actual=ARGV[2]}
    end

    -- 3. Update chain state atomically
    redis.call('HSET', KEYS[2],
               'previous_hash', ARGV[2],
               'sequence', ARGV[1],
               'updated_at', ARGV[3])

    -- 4. Delete pending entry
    redis.call('DEL', KEYS[1])

    return {ok=true}
    """

    # Lua script for batch state query
    LUA_BATCH_GET_STATE = """
    -- KEYS = list of state keys
    -- Returns array of {seq, prev_hash} for each key

    local results = {}
    for i, key in ipairs(KEYS) do
        local seq = redis.call('HGET', key, 'sequence') or '0'
        local prev_hash = redis.call('HGET', key, 'previous_hash') or 'GENESIS'
        table.insert(results, {seq=tonumber(seq), prev_hash=prev_hash, key=key})
    end
    return cjson.encode(results)
    """

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur:",
        pending_ttl_seconds: int = 30,
    ):
        """
        Initialize Lua atomic hash chain.

        Args:
            redis_client: Redis client instance
            key_prefix: Prefix for all Redis keys
            pending_ttl_seconds: TTL for pending entries
        """
        from baldur.audit.performance.lua_registry import LuaScriptRegistry

        self._redis = redis_client
        self._key_prefix = key_prefix
        self._pending_ttl = pending_ttl_seconds
        self._registry = LuaScriptRegistry(redis_client)
        self._registry.register("add_integrity", self.LUA_ATOMIC_ADD_INTEGRITY)
        self._registry.register("commit", self.LUA_ATOMIC_COMMIT)
        self._registry.register("batch_get", self.LUA_BATCH_GET_STATE)

    def _get_keys(self) -> dict[str, str]:
        """Get standard Redis key names."""
        return {
            "seq": f"{self._key_prefix}audit:{{hash_chain}}:seq",
            "state": f"{self._key_prefix}audit:{{hash_chain}}:state",
            "pending_prefix": f"{self._key_prefix}audit:{{hash_chain}}:pending",
        }

    def reserve_sequence_atomic(
        self,
        expected_hash: str,
        previous_hash: str,
    ) -> tuple[bool, int, str]:
        """
        Atomically reserve a sequence number with expected hash.

        Single RTT operation combining:
        - Sequence increment
        - Previous hash validation
        - Pending state creation

        Args:
            expected_hash: Expected final hash after write
            previous_hash: Previous hash for chain validation

        Returns:
            Tuple of (success, sequence, error_message)
        """
        keys = self._get_keys()
        timestamp = utc_now().isoformat()

        try:
            result = self._registry.execute(
                "add_integrity",
                keys=[keys["seq"], keys["state"], keys["pending_prefix"]],
                args=[
                    expected_hash,
                    previous_hash,
                    timestamp,
                    str(self._pending_ttl),
                ],
            )

            if isinstance(result, dict) and "err" in result:
                return False, 0, result["err"]

            if isinstance(result, list) and len(result) >= 2:
                # Redis returns list: [seq, prev_hash]
                seq = int(result[0]) if result[0] else 0
                return True, seq, ""

            # Parse other formats
            if hasattr(result, "get"):
                seq = result.get("seq", 0)
                return True, int(seq), ""

            return True, int(result) if result else 0, ""

        except Exception as e:
            logger.exception(
                "lua_atomic_hash_chain.reserve_failed",
                error=e,
            )
            return False, 0, str(e)

    def commit_sequence_atomic(
        self,
        sequence: int,
        actual_hash: str,
    ) -> tuple[bool, str]:
        """
        Atomically commit a reserved sequence.

        Single RTT operation combining:
        - Pending entry verification
        - Hash validation
        - State update
        - Pending cleanup

        Args:
            sequence: Sequence number to commit
            actual_hash: Actual computed hash

        Returns:
            Tuple of (success, error_message)
        """
        keys = self._get_keys()
        pending_key = f"{keys['pending_prefix']}:{sequence}"
        timestamp = utc_now().isoformat()

        try:
            result = self._registry.execute(
                "commit",
                keys=[pending_key, keys["state"]],
                args=[str(sequence), actual_hash, timestamp],
            )

            if isinstance(result, dict) and "err" in result:
                return False, result["err"]

            return True, ""

        except Exception as e:
            logger.exception(
                "lua_atomic_hash_chain.commit_failed",
                error=e,
            )
            return False, str(e)
