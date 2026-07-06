"""
Async Redis Cache Adapter for Baldur System.

Async twin of :class:`RedisCacheAdapter`, scoped to the minimal
:class:`AsyncCacheProviderInterface` dedup surface (asetnx / aget /
acas_dict_field / adelete). Backs the awaitable idempotency gate so
``aprotect(idempotency_key=...)`` performs its cross-worker dedup via native
``await`` instead of offloading a synchronous ``setnx`` off the event loop.

Cross-consistency with the sync adapter:
    Uses the same key-prefix resolution (``get_effective_key_prefix`` /
    TestModeContext / NamespaceSettings) and the same JSON serialization as
    :class:`RedisCacheAdapter`, and reads the same ``get_redis_settings()``
    (url + socket timeouts). Async and sync therefore write the SAME Redis keys
    for the same logical dedup key ⇒ a sync ``protect`` and an async
    ``aprotect`` on one key dedup against each other by construction.

Requirements:
    - redis>=4.2 (``redis.asyncio`` shipped in redis-py 4.2).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog

from baldur.adapters.cache.redis_adapter import LUA_CAS_DICT_FIELD, LUA_CAS_TAKEOVER
from baldur.core.exceptions import AdapterConnectionError
from baldur.interfaces.cache_provider import AsyncCacheProviderInterface
from baldur.utils.serialization import fast_dumps, fast_loads

logger = structlog.get_logger()

__all__ = ["AsyncRedisCacheAdapter"]


def _record_operation_error(operation: str) -> None:
    """Record a swallowed async Redis cache operation error (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.drift_metrics import record_cache_operation_error

        record_cache_operation_error(backend="redis", operation=operation)
    except Exception:
        pass


class AsyncRedisCacheAdapter(AsyncCacheProviderInterface):
    """Redis implementation of the async dedup cache surface (``redis.asyncio``).

    Example:
        >>> cache = AsyncRedisCacheAdapter()
        >>> await cache.asetnx("k", {"status": "executing"}, ttl=timedelta(minutes=5))
        True
        >>> await cache.aclose()  # drain the pool on shutdown
    """

    def __init__(
        self,
        url: str | None = None,
        client: Any | None = None,
        key_prefix: str | None = None,
        socket_timeout: float | None = None,
        socket_connect_timeout: float | None = None,
        retry_on_timeout: bool | None = None,
    ) -> None:
        """Initialize the async Redis cache adapter.

        Args:
            url: Redis URL. When ``None``, resolves from ``BALDUR_REDIS_URL``
                via ``get_redis_settings()`` (same source as the sync adapter).
            client: Pre-configured ``redis.asyncio`` client (takes precedence;
                used by unit tests to inject a fake).
            key_prefix: Tri-state prefix selector, identical semantics to
                :class:`RedisCacheAdapter` — ``None`` (default) resolves the
                dynamic per-operation prefix (TestModeContext / NamespaceSettings
                aware) so async keys match the sync adapter's keys; ``""`` adds
                no prefix; a literal string is a static override.
            socket_timeout: Socket read timeout. ``None`` reads
                ``RedisSettings.socket_timeout`` so the async client's socket
                behavior matches the sync adapter.
            socket_connect_timeout: Socket connect timeout. ``None`` reads
                ``RedisSettings.socket_connect_timeout``.
            retry_on_timeout: Retry-on-timeout flag. ``None`` reads
                ``RedisSettings.retry_on_timeout``.
        """
        self._key_prefix = key_prefix

        if client is not None:
            self._redis = client
            return

        from baldur.settings.redis import get_redis_settings

        settings = get_redis_settings()
        if url is None:
            url = settings.url
        if socket_timeout is None:
            socket_timeout = settings.socket_timeout
        if socket_connect_timeout is None:
            socket_connect_timeout = settings.socket_connect_timeout
        if retry_on_timeout is None:
            retry_on_timeout = settings.retry_on_timeout

        try:
            import redis.asyncio as aioredis
        except ImportError as e:
            raise ImportError(
                "AsyncRedisCacheAdapter requires redis>=4.2 (redis.asyncio). "
                "Install baldur-framework[redis]."
            ) from e

        connect_kwargs: dict[str, Any] = {
            "socket_timeout": socket_timeout,
            "socket_connect_timeout": socket_connect_timeout,
            "retry_on_timeout": retry_on_timeout,
            "decode_responses": False,
        }
        # Auth is separated from the URL in Baldur (RedisSettings) for security
        # / ACL support, matching the sync connection factory — forward it when
        # set rather than embedding credentials in the URL.
        if settings.password is not None:
            connect_kwargs["password"] = settings.password
        if settings.username is not None:
            connect_kwargs["username"] = settings.username

        self._redis = aioredis.Redis.from_url(url, **connect_kwargs)

    def _effective_prefix(self) -> str:
        """Return the prefix to apply — tri-state, mirroring the sync adapter."""
        if self._key_prefix is None:
            from baldur.settings.namespace import get_effective_key_prefix

            return get_effective_key_prefix()
        return self._key_prefix

    def _make_key(self, key: str) -> str:
        return f"{self._effective_prefix()}{key}"

    def _serialize(self, value: Any) -> bytes:
        return fast_dumps(value, default=str)

    def _deserialize(self, data: bytes | None) -> Any:
        if data is None:
            return None
        return fast_loads(data)

    @property
    def provider_name(self) -> str:
        """Return 'redis' as the provider identifier."""
        return "redis"

    async def aget(self, key: str) -> Any | None:
        try:
            data = await self._redis.get(self._make_key(key))
            if data is None:
                return None
            return self._deserialize(data)
        except Exception as e:
            logger.exception("async_redis_cache.get_error", cache_key=key, error=e)
            return None

    async def adelete(self, key: str) -> bool:
        try:
            return await self._redis.delete(self._make_key(key)) > 0
        except Exception as e:
            logger.exception("async_redis_cache.delete_error", cache_key=key, error=e)
            return False

    async def asetnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Atomic SET NX (+ optional PX/EX) — single-round-trip acquire."""
        try:
            serialized = self._serialize(value)
            if ttl:
                return bool(
                    await self._redis.set(
                        self._make_key(key),
                        serialized,
                        nx=True,
                        ex=int(ttl.total_seconds()),
                    )
                )
            return bool(await self._redis.set(self._make_key(key), serialized, nx=True))
        except Exception as e:
            # Un-swallowed (unlike aget/adelete): asetnx is a dedup-gate-only
            # acquire op, so the outage must surface to the gate's fail-open /
            # IdempotencyUnavailableError path. Wrapped so no redis-py type leaks.
            logger.exception("async_redis_cache.setnx_error", cache_key=key, error=e)
            _record_operation_error("setnx")
            raise AdapterConnectionError(f"asetnx failed for key {key!r}") from e

    async def acas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic single-field CAS via cjson.decode + SET PX in one EVAL.

        Reuses the sync adapter's Lua script (:data:`LUA_CAS_DICT_FIELD`) so the
        async and sync CAS semantics are byte-for-byte identical on shared keys.
        """
        full_key = self._make_key(key)
        serialized_new = self._serialize(new_value)
        ttl_ms = int(ttl.total_seconds() * 1000) if ttl else 0
        result = await self._redis.eval(
            LUA_CAS_DICT_FIELD,
            1,
            full_key,
            field,
            expected,
            serialized_new,
            ttl_ms,
        )
        return result == 1

    async def acas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic failed / stale-executing takeover via one EVAL (single-winner).

        Reuses the sync adapter's Lua script (:data:`LUA_CAS_TAKEOVER`) so the
        async and sync takeover semantics are byte-for-byte identical on shared
        keys. Surfaces a cache outage (raises ``AdapterConnectionError``) rather
        than swallowing to False — this dedup-gate-only acquire op must reach the
        gate's fail-open path, mirroring :meth:`asetnx`.
        """
        full_key = self._make_key(key)
        serialized_new = self._serialize(new_record)
        ttl_ms = int(ttl.total_seconds() * 1000) if ttl else 0
        try:
            result = await self._redis.eval(
                LUA_CAS_TAKEOVER,
                1,
                full_key,
                serialized_new,
                stale_before,
                ttl_ms,
            )
        except Exception as e:
            logger.exception(
                "async_redis_cache.cas_takeover_error", cache_key=key, error=e
            )
            _record_operation_error("cas_takeover")
            raise AdapterConnectionError(f"acas_takeover failed for key {key!r}") from e
        return result == 1

    async def aclose(self) -> None:
        """Drain the ``redis.asyncio`` connection pool. Idempotent, best-effort.

        Registered with the framework-independent
        ``GracefulShutdownCoordinator`` at resolution time (see the async cache
        resolver) so this second pool — separate from the sync
        ``RedisCacheAdapter`` pool — is drained on graceful shutdown / process
        recycle across every framework adapter (Django/Flask/FastAPI/CLI). This
        closes the dev hot-reload leak vector (uvicorn ``--reload`` recycling the
        process without draining the async pool).
        """
        try:
            # redis-py 5.x prefers aclose(); fall back to close() on 4.2–4.x.
            aclose = getattr(self._redis, "aclose", None)
            if aclose is not None:
                await aclose()
            else:
                await self._redis.close()
        except Exception as e:
            logger.warning("async_redis_cache.close_failed", error=e)
