"""
Async In-Memory Cache Adapter for Baldur System.

Async twin of :class:`InMemoryCacheAdapter`, scoped to the minimal
:class:`AsyncCacheProviderInterface` dedup surface (asetnx / aget /
acas_dict_field / adelete). Two roles:

1. Backs the async idempotency fallback path when no distributed cache is
   registered (single-process / OSS deployments with no Redis).
2. Serves as the async unit-test double for ``AsyncIdempotencyGate`` — no
   ``fakeredis`` dependency needed.

Atomicity:
    Each op reads and writes the in-process store WITHOUT awaiting in between,
    so it is atomic with respect to other coroutines cooperatively scheduled on
    the same event loop (CPython asyncio never preempts a coroutine mid-op
    between the read and the write here). This is the async analog of the sync
    adapter's lock-wrapped ops.

Warning:
    In-process only — like the sync adapter, locks and dedup state do not cross
    process boundaries. Not for production distributed dedup; register Redis for
    cross-worker semantics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog

from baldur.interfaces.cache_provider import AsyncCacheProviderInterface

logger = structlog.get_logger()

__all__ = ["AsyncInMemoryCacheAdapter"]


@dataclass
class _AsyncCacheEntry:
    """Internal cache entry with value and expiration."""

    value: Any
    expires_at: float | None = None  # Unix timestamp

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class AsyncInMemoryCacheAdapter(AsyncCacheProviderInterface):
    """Thread-unsafe-but-loop-atomic in-memory async cache for dedup ops.

    Example:
        >>> cache = AsyncInMemoryCacheAdapter()
        >>> await cache.asetnx("k", {"status": "executing"}, ttl=timedelta(minutes=5))
        True
        >>> await cache.aget("k")
        {'status': 'executing'}
    """

    def __init__(self, key_prefix: str = "test:") -> None:
        """Initialize the async in-memory cache.

        Args:
            key_prefix: Prefix applied to every stored key so distinct layers /
                instances cannot collide on the same logical key.
        """
        self._key_prefix = key_prefix
        self._store: dict[str, _AsyncCacheEntry] = {}

    def _make_key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    @property
    def provider_name(self) -> str:
        """Return 'memory' as the provider identifier."""
        return "memory"

    async def aget(self, key: str) -> Any | None:
        full_key = self._make_key(key)
        entry = self._store.get(full_key)
        if entry is None:
            return None
        if entry.is_expired():
            del self._store[full_key]
            return None
        return entry.value

    async def adelete(self, key: str) -> bool:
        full_key = self._make_key(key)
        if full_key in self._store:
            del self._store[full_key]
            return True
        return False

    async def asetnx(self, key: str, value: Any, ttl: timedelta | None = None) -> bool:
        """Set only if absent — atomic acquire (no await between check and set)."""
        full_key = self._make_key(key)
        entry = self._store.get(full_key)
        if entry is not None and not entry.is_expired():
            return False
        expires_at = None
        if ttl is not None:
            expires_at = time.time() + ttl.total_seconds()
        self._store[full_key] = _AsyncCacheEntry(value=value, expires_at=expires_at)
        return True

    async def acas_dict_field(
        self,
        key: str,
        field: str,
        expected: Any,
        new_value: dict[str, Any],
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic single-field CAS on a dict record (no await between read/write)."""
        full_key = self._make_key(key)
        entry = self._store.get(full_key)
        if entry is None or entry.is_expired():
            return False
        if not isinstance(entry.value, dict):
            return False
        if entry.value.get(field) != expected:
            return False
        expires_at = None
        if ttl is not None:
            expires_at = time.time() + ttl.total_seconds()
        self._store[full_key] = _AsyncCacheEntry(value=new_value, expires_at=expires_at)
        return True

    async def acas_takeover(
        self,
        key: str,
        new_record: dict[str, Any],
        *,
        stale_before: float,
        ttl: timedelta | None = None,
    ) -> bool:
        """Atomic failed / stale-executing takeover (no await between read/write).

        Replaces the record iff it is a dict whose ``status == "failed"`` OR
        (``status == "executing"`` AND ``started_at < stale_before``). In-process
        only; no I/O to fail, so it never raises ``AdapterConnectionError``.
        """
        full_key = self._make_key(key)
        entry = self._store.get(full_key)
        if entry is None or entry.is_expired():
            return False
        if not isinstance(entry.value, dict):
            return False
        status = entry.value.get("status")
        takeable = status == "failed" or (
            status == "executing" and entry.value.get("started_at", 0) < stale_before
        )
        if not takeable:
            return False
        expires_at = None
        if ttl is not None:
            expires_at = time.time() + ttl.total_seconds()
        self._store[full_key] = _AsyncCacheEntry(
            value=new_record, expires_at=expires_at
        )
        return True

    # ------------------------------------------------------------------
    # Testing / lifecycle utilities
    # ------------------------------------------------------------------

    def clear_all(self) -> None:
        """Clear the entire store (test cleanup)."""
        self._store.clear()

    async def aclose(self) -> None:
        """No-op close — symmetric with :class:`AsyncRedisCacheAdapter`.

        The in-memory adapter holds no sockets, so there is nothing to drain;
        the method exists so the resolver can treat either backing uniformly.
        """
        return
