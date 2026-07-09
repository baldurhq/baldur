"""
Redis Cache Adapter for Baldur System

Concrete implementation of CacheProviderInterface using Redis.
Provides distributed caching with locking support for circuit breakers.

Requirements:
    - redis>=4.0.0
    - django-redis (optional, for Django cache integration)

Related:
    - interfaces/cache_provider.py: Interface definition
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import structlog

from baldur.core.exceptions import AdapterConnectionError
from baldur.interfaces.cache_provider import (
    CacheProviderInterface,
    DistributedLock,
    generate_lock_owner_id,
)
from baldur.utils.serialization import fast_dumps, fast_loads

logger = structlog.get_logger()


def _record_operation_error(operation: str) -> None:
    """Record a swallowed Redis cache operation error (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.drift_metrics import record_cache_operation_error

        record_cache_operation_error(backend="redis", operation=operation)
    except Exception:
        pass


# Atomic compare-and-set on a single field of a JSON-blob dict record.
# Decodes the existing record via cjson.decode, branches on field equality,
# and writes the pre-serialized new value with PX TTL in a single EVAL.
# Returns 1 on successful CAS, 0 on missing key / non-dict / field mismatch.
LUA_CAS_DICT_FIELD = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return 0
end
local rec = cjson.decode(raw)
if type(rec) ~= 'table' then
    return 0
end
if rec[ARGV[1]] ~= ARGV[2] then
    return 0
end
local ttl_ms = tonumber(ARGV[4])
if ttl_ms and ttl_ms > 0 then
    redis.call('SET', KEYS[1], ARGV[3], 'PX', ttl_ms)
else
    redis.call('SET', KEYS[1], ARGV[3])
end
return 1
"""


# Atomic takeover of a failed / stale-executing dedup claim. Decodes the record,
# and rewrites it iff status == 'failed' OR (status == 'executing' AND
# started_at < stale_before) — the app-computed staleness threshold passed as an
# arg (never redis TIME, to keep the comparison app-clock vs app-clock). The
# started_at re-check inside the atomic op is what makes a fresh post-takeover
# claim (started_at == now) ineligible for a second taker — single-winner.
# ARGV[1]=serialized new record, ARGV[2]=stale_before, ARGV[3]=ttl_ms.
# Returns 1 on takeover, 0 on missing key / non-dict / fresh-executing / other.
LUA_CAS_TAKEOVER = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return 0
end
local rec = cjson.decode(raw)
if type(rec) ~= 'table' then
    return 0
end
local status = rec['status']
local takeable = false
if status == 'failed' then
    takeable = true
elseif status == 'executing' then
    local started_at = tonumber(rec['started_at'])
    local stale_before = tonumber(ARGV[2])
    if started_at ~= nil and stale_before ~= nil and started_at < stale_before then
        takeable = true
    end
end
if not takeable then
    return 0
end
local ttl_ms = tonumber(ARGV[3])
if ttl_ms and ttl_ms > 0 then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ttl_ms)
else
    redis.call('SET', KEYS[1], ARGV[1])
end
return 1
"""


class RedisDistributedLock(DistributedLock):
    """
    Redis-based distributed lock using SET NX with TTL.

    Uses the Redis SET command with NX (not exists) and PX (expire)
    options for atomic lock acquisition.

    Features:
        - Atomic acquire/release operations
        - Automatic TTL-based expiration (prevents deadlocks)
        - Owner identification for safe release
        - TTL extension support
    """

    def __init__(
        self,
        redis_client: Any,
        full_key: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
        sleep_interval: float = 0.1,
    ) -> None:
        """
        Initialize Redis distributed lock.

        Args:
            redis_client: Redis client instance
            full_key: Verbatim Redis key (caller-resolved, written as-is).
                ``RedisCacheAdapter.get_lock()`` resolves
                ``self._make_key(name)`` once and passes the result here;
                this lock applies no further transformation. Direct
                callers (audit / postmortem / integrity tasks) construct
                their own prefixed key.
            timeout: Lock auto-expire time
            blocking_timeout: Max time to wait when acquiring
            sleep_interval: Time between acquire retries
        """
        self._redis = redis_client
        self._name = full_key
        self._timeout = timeout
        self._blocking_timeout = blocking_timeout
        self._sleep_interval = sleep_interval

        # Unique owner ID for this lock instance
        self._owner_id = generate_lock_owner_id()
        self._acquired = False

    def acquire(
        self,
        blocking: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """
        Acquire the lock.

        Uses SET NX PX for atomic acquisition with TTL.

        Args:
            blocking: If True, retry until acquired or timeout
            timeout: Override blocking_timeout

        Returns:
            True if lock was acquired
        """
        blocking_timeout = timeout if timeout is not None else self._blocking_timeout
        timeout_ms = int(self._timeout.total_seconds() * 1000)
        stop_time = None

        if blocking and blocking_timeout is not None:
            stop_time = time.time() + blocking_timeout

        while True:
            # Try to acquire: SET key value NX PX timeout
            acquired = self._redis.set(
                self._name,
                self._owner_id,
                nx=True,
                px=timeout_ms,
            )

            if acquired:
                self._acquired = True
                logger.debug(
                    "redis_lock.acquired_lock",
                    name=self._name,
                )
                return True

            if not blocking:
                return False

            if stop_time is not None and time.time() >= stop_time:
                logger.debug(
                    "redis_lock.timeout_acquiring_lock",
                    name=self._name,
                )
                return False

            time.sleep(self._sleep_interval)

    def release(self) -> None:
        """
        Release the lock if owned by this instance.

        Uses Lua script for atomic check-and-delete.

        Raises:
            LockNotOwnedError: If lock is not owned by this instance
        """
        if not self._acquired:
            logger.warning(
                "redis_lock.attempting_release_non_acquired",
                name=self._name,
            )
            return

        # Lua script for atomic check-and-delete
        # Only delete if the value matches our owner ID
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """

        try:
            result = self._redis.eval(lua_script, 1, self._name, self._owner_id)
            if result == 1:
                self._acquired = False
                logger.debug(
                    "redis_lock.released_lock",
                    name=self._name,
                )
            else:
                logger.warning(
                    "redis_lock.lock_owned_expired",
                    name=self._name,
                )
                self._acquired = False
        except Exception as e:
            logger.exception(
                "redis_lock.error_releasing_lock",
                error=e,
            )
            self._acquired = False
            raise

    def locked(self) -> bool:
        """Check if lock is currently held by anyone."""
        return self._redis.exists(self._name) > 0

    def owned(self) -> bool:
        """Check if lock is held by this instance."""
        if not self._acquired:
            return False
        current_owner = self._redis.get(self._name)
        if isinstance(current_owner, bytes):
            current_owner = current_owner.decode("utf-8")
        return current_owner == self._owner_id

    def extend(self, additional_time: timedelta) -> bool:
        """
        Extend the lock's TTL.

        Uses Lua script to atomically verify ownership and extend.

        Args:
            additional_time: Time to add to current TTL

        Returns:
            True if extension was successful
        """
        if not self._acquired:
            return False

        additional_ms = int(additional_time.total_seconds() * 1000)

        # Lua script: verify ownership then extend TTL
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("pexpire", KEYS[1], ARGV[2])
        else
            return 0
        end
        """

        try:
            result = self._redis.eval(
                lua_script, 1, self._name, self._owner_id, additional_ms
            )
            return result == 1
        except Exception as e:
            logger.exception(
                "redis_lock.error_extending_lock",
                error=e,
            )
            return False


class RedisCacheAdapter(CacheProviderInterface):
    """
    Redis implementation of CacheProviderInterface.

    This adapter provides full Redis caching functionality including
    distributed locks, atomic counters, and TTL management.

    Configuration:
        ``url`` can be passed explicitly. When omitted (no-arg construction),
        the adapter reads ``BALDUR_REDIS_URL`` via :func:`get_redis_settings`,
        matching ``RedisConnectionFactory``'s URL source.

    Example:
        >>> cache = RedisCacheAdapter(url="redis://localhost:6379/0")
        >>> cache.set("key", {"data": "value"}, ttl=timedelta(minutes=5))
        >>> data = cache.get("key")
        >>> with cache.get_lock("my_lock") as lock:
        ...     # Critical section
        ...     pass
    """

    def __init__(
        self,
        url: str | None = None,
        client: Any | None = None,
        key_prefix: str | None = None,
        default_ttl: timedelta | None = None,
        socket_timeout: float | None = None,
        socket_connect_timeout: float | None = None,
        retry_on_timeout: bool | None = None,
    ) -> None:
        """
        Initialize Redis cache adapter.

        Args:
            url: Redis URL (e.g., "redis://localhost:6379/0"). When ``None``,
                resolves from ``BALDUR_REDIS_URL`` via ``get_redis_settings()``.
            client: Pre-configured Redis client (takes precedence over url)
            key_prefix: Tri-state prefix selector:

                - ``None`` (default) — per-operation dynamic prefix via
                  :func:`get_effective_key_prefix`. Honors
                  :class:`TestModeContext` (`xtest:` synthetic isolation) and
                  :class:`NamespaceSettings` (region/tenant/env separation).
                  Symmetric with :class:`ResilientStorageBackend`.
                - ``""`` — composer pattern: no prefix added. The caller is
                  responsible for prepending its own prefix
                  (`ResilientStorageBackend`, `rate_limit_tracker`).
                - ``"static:"`` — static literal override. Used by tests for
                  per-instance isolation that must NOT shift with
                  TestModeContext.
            default_ttl: Default TTL for set operations
            socket_timeout: Socket read timeout in seconds. When None (default),
                resolves from RedisSettings.socket_timeout via the connection
                factory (same source as the async adapter).
            socket_connect_timeout: Socket connect timeout in seconds. When None
                (default), resolves from RedisSettings.socket_connect_timeout.
            retry_on_timeout: Retry-on-timeout flag. When None (default),
                resolves from RedisSettings.retry_on_timeout.
        """
        self._key_prefix = key_prefix
        self._default_ttl = default_ttl

        if client is not None:
            self._redis = client
        else:
            if url is None:
                from baldur.settings.redis import get_redis_settings

                url = get_redis_settings().url

            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )

            self._redis = get_redis_connection_factory().create(
                url,
                socket_timeout=socket_timeout,
                socket_connect_timeout=socket_connect_timeout,
                retry_on_timeout=retry_on_timeout,
                decode_responses=False,
            )

        self._lua_registry: Any | None = None

    def _get_lua_registry(self) -> Any:
        """Lazy-init LuaScriptRegistry and register adapter-owned scripts."""
        if self._lua_registry is None:
            from baldur.audit.performance.lua_registry import LuaScriptRegistry

            registry = LuaScriptRegistry(self._redis)
            registry.register("idempotency_cas_dict_field", LUA_CAS_DICT_FIELD)
            registry.register("idempotency_cas_takeover", LUA_CAS_TAKEOVER)
            self._lua_registry = registry
        return self._lua_registry

    def _effective_prefix(self) -> str:
        """Return the prefix string to apply to this operation.

        Tri-state per :meth:`__init__` ``key_prefix`` argument. The dynamic
        path reads :func:`get_effective_key_prefix` per call so
        :class:`TestModeContext` (a request-scoped ``ContextVar``) flips
        synthetic traffic into the ``xtest:`` namespace without rebuilding
        the adapter.
        """
        if self._key_prefix is None:
            from baldur.settings.namespace import get_effective_key_prefix

            return get_effective_key_prefix()
        return self._key_prefix

    def _make_key(self, key: str) -> str:
        """Add prefix to key."""
        return f"{self._effective_prefix()}{key}"

    def _serialize(self, value: Any) -> bytes:
        """Serialize value to bytes."""
        return fast_dumps(value, default=str)

    def _deserialize(self, data: bytes) -> Any:
        """Deserialize bytes to Python object."""
        if data is None:
            return None
        return fast_loads(data)

    @property
    def provider_name(self) -> str:
        """Return 'redis' as the provider identifier."""
        return "redis"

    @property
    def raw_client(self) -> Any:
        """Return the underlying redis client.

        Public seam for composed components that need the raw client for
        operations the cache interface does not expose (e.g. Lua eval,
        pipelines). The raw client already legitimately escapes the adapter
        via :meth:`get_lock`, which hands it to ``DistributedLock``.
        """
        return self._redis

    # =========================================================================
    # Basic Operations
    # =========================================================================

    def get(self, key: str) -> Any | None:
        """Get value by key."""
        try:
            data = self._redis.get(self._make_key(key))
            if data is None:
                return None
            return self._deserialize(data)
        except Exception as e:
            logger.exception(
                "redis_cache.get_error",
                cache_key=key,
                error=e,
            )
            return None

    def set(
        self,
        key: str,
        value: Any,
        ttl: timedelta | None = None,
    ) -> bool:
        """Set value with optional TTL."""
        try:
            ttl = ttl or self._default_ttl
            serialized = self._serialize(value)

            if ttl:
                return bool(
                    self._redis.set(
                        self._make_key(key),
                        serialized,
                        ex=int(ttl.total_seconds()),
                    )
                )
            return bool(self._redis.set(self._make_key(key), serialized))
        except Exception as e:
            logger.exception(
                "redis_cache.set_error",
                cache_key=key,
                error=e,
            )
            return False

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        try:
            return self._redis.delete(self._make_key(key)) > 0
        except Exception as e:
            logger.exception(
                "redis_cache.delete_error",
                cache_key=key,
                error=e,
            )
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        try:
            return self._redis.exists(self._make_key(key)) > 0
        except Exception as e:
            logger.exception(
                "redis_cache.exists_error",
                cache_key=key,
                error=e,
            )
            return False

    # =========================================================================
    # Atomic Operations
    # =========================================================================

    def incr(self, key: str, amount: int = 1) -> int:
        """Atomically increment a counter."""
        try:
            return self._redis.incr(self._make_key(key), amount)
        except Exception as e:
            logger.exception(
                "redis_cache.incr_error",
                cache_key=key,
                error=e,
            )
            return 0

    def decr(self, key: str, amount: int = 1) -> int:
        """Atomically decrement a counter."""
        try:
            return self._redis.decr(self._make_key(key), amount)
        except Exception as e:
            logger.exception(
                "redis_cache.decr_error",
                cache_key=key,
                error=e,
            )
            return 0

    def expire(self, key: str, ttl: timedelta) -> bool:
        """Set expiration on existing key."""
        try:
            return bool(
                self._redis.expire(
                    self._make_key(key),
                    int(ttl.total_seconds()),
                )
            )
        except Exception as e:
            logger.exception(
                "redis_cache.expire_error",
                cache_key=key,
                error=e,
            )
            return False

    def ttl(self, key: str) -> int | None:
        """Get remaining TTL in seconds."""
        try:
            result = self._redis.ttl(self._make_key(key))
            if result == -1:
                return None  # No expiration
            if result == -2:
                return -2  # Key doesn't exist
            return result
        except Exception as e:
            logger.exception(
                "redis_cache.ttl_error",
                cache_key=key,
                error=e,
            )
            return -2

    def setnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Set value only if key does not exist."""
        try:
            serialized = self._serialize(value)
            if ttl:
                return bool(
                    self._redis.set(
                        self._make_key(key),
                        serialized,
                        nx=True,
                        ex=int(ttl.total_seconds()),
                    )
                )
            return bool(self._redis.setnx(self._make_key(key), serialized))
        except Exception as e:
            # Un-swallowed (unlike get/delete): setnx is a dedup-gate-only
            # acquire op, so surfacing the outage lets the idempotency gate's
            # fail-open / IdempotencyUnavailableError path fire instead of
            # misreporting the outage as a duplicate. Wrapped in the domain
            # exception so no redis-py type leaks past the adapter boundary.
            logger.exception(
                "redis_cache.setnx_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("setnx")
            raise AdapterConnectionError(f"setnx failed for key {key!r}") from e

    def cas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic single-field CAS via cjson.decode + SET PX in one EVAL."""
        full_key = self._make_key(key)
        serialized_new = self._serialize(new_value)
        ttl_ms = int(ttl.total_seconds() * 1000) if ttl else 0
        result = self._get_lua_registry().execute(
            "idempotency_cas_dict_field",
            keys=[full_key],
            args=[field, expected, serialized_new, ttl_ms],
        )
        return result == 1

    def cas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic failed / stale-executing takeover via one EVAL (single-winner).

        Surfaces a cache outage (raises ``AdapterConnectionError``) rather than
        swallowing to False — this is a dedup-gate-only acquire op whose failure
        must reach the gate's fail-open path, mirroring :meth:`setnx`.
        """
        full_key = self._make_key(key)
        serialized_new = self._serialize(new_record)
        ttl_ms = int(ttl.total_seconds() * 1000) if ttl else 0
        try:
            result = self._get_lua_registry().execute(
                "idempotency_cas_takeover",
                keys=[full_key],
                args=[serialized_new, stale_before, ttl_ms],
            )
        except Exception as e:
            logger.exception(
                "redis_cache.cas_takeover_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("cas_takeover")
            raise AdapterConnectionError(f"cas_takeover failed for key {key!r}") from e
        return result == 1

    # =========================================================================
    # Distributed Locking
    # =========================================================================

    def get_lock(
        self,
        name: str,
        timeout: timedelta = timedelta(seconds=10),
        blocking_timeout: float | None = None,
    ) -> DistributedLock:
        """Get a distributed lock instance.

        Resolves the storage key once via ``self._make_key(name)`` and
        passes the result to the lock constructor as ``full_key``. The
        lock snapshots the resolved key for its lifetime — acquire /
        release / extend operate on the same Redis key regardless of
        ``TestModeContext`` boundary crossings.
        """
        # #465 D1: snapshot the resolved key once so boundary crossings cannot shift it.
        return RedisDistributedLock(
            redis_client=self._redis,
            full_key=self._make_key(name),
            timeout=timeout,
            blocking_timeout=blocking_timeout,
        )

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    def mget(self, keys: list[str]) -> dict[str, Any]:
        """Get multiple values at once."""
        if not keys:
            return {}

        try:
            prefixed_keys = [self._make_key(k) for k in keys]
            values = self._redis.mget(prefixed_keys)

            result = {}
            for key, value in zip(keys, values, strict=False):
                if value is not None:
                    result[key] = self._deserialize(value)
            return result
        except Exception as e:
            logger.exception(
                "redis_cache.mget_error",
                error=e,
            )
            return {}

    def mset(
        self,
        mapping: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Set multiple values at once."""
        if not mapping:
            return True

        try:
            prefixed_mapping = {
                self._make_key(k): self._serialize(v) for k, v in mapping.items()
            }

            # MSET doesn't support TTL, so we use pipeline
            if ttl:
                pipe = self._redis.pipeline()
                ttl_seconds = int(ttl.total_seconds())
                for key, value in prefixed_mapping.items():
                    pipe.set(key, value, ex=ttl_seconds)
                pipe.execute()
            else:
                self._redis.mset(prefixed_mapping)

            return True
        except Exception as e:
            logger.exception(
                "redis_cache.mset_error",
                error=e,
            )
            return False

    def mdelete(self, keys: list[str]) -> int:
        """Delete multiple keys at once."""
        if not keys:
            return 0

        try:
            prefixed_keys = [self._make_key(k) for k in keys]
            return self._redis.delete(*prefixed_keys)
        except Exception as e:
            logger.exception(
                "redis_cache.mdelete_error",
                error=e,
            )
            return 0

    # =========================================================================
    # Hash Operations
    # =========================================================================

    def hget(self, name: str, key: str) -> Any | None:
        """Get a field from a hash."""
        try:
            data = self._redis.hget(self._make_key(name), key)
            if data is None:
                return None
            return self._deserialize(data)
        except Exception as e:
            logger.exception(
                "redis_cache.hget_error",
                hash_name=name,
                cache_key=key,
                error=e,
            )
            return None

    def hset(self, name: str, key: str, value: Any) -> bool:
        """Set a field in a hash."""
        try:
            serialized = self._serialize(value)
            self._redis.hset(self._make_key(name), key, serialized)
            return True
        except Exception as e:
            logger.exception(
                "redis_cache.hset_error",
                hash_name=name,
                cache_key=key,
                error=e,
            )
            return False

    def hgetall(self, name: str) -> dict[str, Any]:
        """Get all fields from a hash."""
        try:
            raw_data = self._redis.hgetall(self._make_key(name))
            result = {}
            for k, v in raw_data.items():
                if isinstance(k, bytes):
                    k = k.decode("utf-8")
                result[k] = self._deserialize(v)
            return result
        except Exception as e:
            logger.exception(
                "redis_cache.hgetall_error",
                hash_name=name,
                error=e,
            )
            return {}

    # =========================================================================
    # List Operations
    # =========================================================================

    def push_limit(
        self, key: str, value: Any, max_len: int, ttl: timedelta | None = None
    ) -> int:
        """Atomically append value and trim list to max_len via RPUSH+LTRIM+EXPIRE."""
        full_key = self._make_key(key)
        try:
            serialized = self._serialize(value)
            pipe = self._redis.pipeline()
            pipe.rpush(full_key, serialized)
            pipe.ltrim(full_key, -max_len, -1)
            if ttl is not None:
                pipe.expire(full_key, int(ttl.total_seconds()))
            results = pipe.execute()
            return results[0]  # RPUSH returns pre-trim length
        except Exception as e:
            logger.exception(
                "redis_cache.push_limit_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("push_limit")
            return 0

    def list_range(self, key: str, start: int, end: int) -> list[Any]:
        """Return elements from start to end (inclusive) via LRANGE."""
        full_key = self._make_key(key)
        try:
            raw_items = self._redis.lrange(full_key, start, end)
            result = []
            for item in raw_items:
                try:
                    result.append(self._deserialize(item))
                except Exception:
                    result.append(item)
            return result
        except Exception as e:
            logger.exception(
                "redis_cache.list_range_error",
                cache_key=key,
                error=e,
            )
            _record_operation_error("list_range")
            return []

    # =========================================================================
    # Health Check
    # =========================================================================

    def health_check(self) -> bool:
        """Check if Redis is reachable."""
        try:
            return self._redis.ping()
        except Exception as e:
            logger.exception(
                "redis_cache.health_check_failed",
                error=e,
            )
            return False

    def flush_all(self) -> bool:
        """Clear all keys with our prefix (not entire Redis DB)."""
        try:
            # Use SCAN to find keys with our prefix
            cursor = 0
            pattern = f"{self._effective_prefix()}*"
            deleted = 0

            while True:
                cursor, keys = self._redis.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted += self._redis.delete(*keys)
                if cursor == 0:
                    break

            logger.info(
                "redis_cache.flushed_keys",
                deleted=deleted,
            )
            return True
        except Exception as e:
            logger.exception(
                "redis_cache.flush_error",
                error=e,
            )
            return False

    # =========================================================================
    # Key Pattern Operations
    # =========================================================================

    def keys(self, pattern: str = "*") -> list[str]:
        """Find keys matching a pattern."""
        try:
            # Resolve prefix once so the SCAN pattern and the strip arithmetic
            # see the same string — TestModeContext could flip mid-call otherwise.
            prefix = self._effective_prefix()
            full_pattern = f"{prefix}{pattern}"
            raw_keys = self._redis.keys(full_pattern)
            prefix_len = len(prefix)
            return [
                (
                    k.decode("utf-8")[prefix_len:]
                    if isinstance(k, bytes)
                    else k[prefix_len:]
                )
                for k in raw_keys
            ]
        except Exception as e:
            logger.exception(
                "redis_cache.keys_error",
                pattern=pattern,
                error=e,
            )
            return []

    def scan(
        self,
        pattern: str = "*",
        count: int = 100,
    ) -> tuple[int, list[str]]:
        """Incrementally iterate keys matching a pattern."""
        try:
            prefix = self._effective_prefix()
            full_pattern = f"{prefix}{pattern}"
            cursor, raw_keys = self._redis.scan(0, match=full_pattern, count=count)
            prefix_len = len(prefix)
            keys = [
                (
                    k.decode("utf-8")[prefix_len:]
                    if isinstance(k, bytes)
                    else k[prefix_len:]
                )
                for k in raw_keys
            ]
            return (cursor, keys)
        except Exception as e:
            logger.exception(
                "redis_cache.scan_error",
                pattern=pattern,
                error=e,
            )
            return (0, [])

    def close(self) -> None:
        """Disconnect the Redis connection pool. Idempotent.

        Drains all sockets held by the underlying ``redis-py`` connection
        pool. Required by the test-fixture reset chain:
        ``reset_init_state()`` re-runs ``init()`` repeatedly under xdist,
        and without an explicit pool drain each iteration leaks file
        descriptors until the runner trips "too many open files".
        """
        try:
            self._redis.connection_pool.disconnect()
        except Exception as e:
            logger.warning("redis_cache.close_failed", error=e)

    def reconnect(self) -> bool:
        """
        Reset the connection pool - release dead connections and reconnect.

        redis-py's ConnectionPool.disconnect() closes all connections in the pool.
        A subsequent ping() call causes the pool to create new connections automatically.

        Returns:
            Whether reconnection succeeded
        """
        try:
            self._redis.connection_pool.disconnect()
            return self._redis.ping()
        except Exception as e:
            logger.exception(
                "redis_cache.reconnect_failed",
                error=e,
            )
            return False
