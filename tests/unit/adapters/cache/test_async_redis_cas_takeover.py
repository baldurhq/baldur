"""Unit tests for ``AsyncRedisCacheAdapter`` acquire-op atomicity + outage (673).

Async twin of ``test_redis_cas_takeover.py``, mocked against a ``redis.asyncio``
client (the real ``EVAL`` semantics are covered by the ``requires_redis`` lane):

- **acas_takeover call-shape (G1):** one ``EVAL`` of the shared ``LUA_CAS_TAKEOVER``
  script with ``[serialized_new, stale_before, ttl_ms]``; ``1 → True`` / ``0 → False``.
- **Un-swallow (G2):** ``asetnx`` / ``acas_takeover`` re-raise a backend I/O error as
  ``AdapterConnectionError`` (never swallow to ``False``) — mirroring the sync
  adapter, so the async gate's fail-open / unavailable path is reachable.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock ``redis.asyncio`` client, AsyncMock).
- §8.2 Exception/edge cases (backend error → domain exception).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from baldur.adapters.cache.async_redis_adapter import AsyncRedisCacheAdapter
from baldur.adapters.cache.redis_adapter import LUA_CAS_TAKEOVER
from baldur.core.exceptions import AdapterConnectionError
from baldur.utils.serialization import fast_dumps


@pytest.fixture
def adapter() -> AsyncRedisCacheAdapter:
    """AsyncRedisCacheAdapter over an injected AsyncMock client, static prefix."""
    return AsyncRedisCacheAdapter(client=AsyncMock(), key_prefix="at:")


_NEW = {"status": "executing", "started_at": 123.0, "retry_count": 1}


class TestAsyncRedisCasTakeoverCallShapeBehavior:
    """``acas_takeover`` evaluates the shared takeover Lua via one ``EVAL``."""

    @pytest.mark.asyncio
    async def test_eval_uses_shared_takeover_script_and_argv_layout(self, adapter):
        adapter._redis.eval.return_value = 1

        ok = await adapter.acas_takeover(
            "k", _NEW, stale_before=1750.5, ttl=timedelta(seconds=30)
        )

        assert ok is True
        adapter._redis.eval.assert_awaited_once_with(
            LUA_CAS_TAKEOVER,
            1,
            adapter._make_key("k"),
            fast_dumps(_NEW, default=str),
            1750.5,
            30_000,
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_lua_returns_0(self, adapter):
        adapter._redis.eval.return_value = 0
        assert await adapter.acas_takeover("k", _NEW, stale_before=1.0) is False

    @pytest.mark.asyncio
    async def test_ttl_none_serializes_to_zero(self, adapter):
        adapter._redis.eval.return_value = 1

        await adapter.acas_takeover("k", _NEW, stale_before=1.0, ttl=None)

        assert adapter._redis.eval.await_args.args[5] == 0


class TestAsyncRedisAcquireUnswallowBehavior:
    """``asetnx`` / ``acas_takeover`` surface a backend I/O error (G2)."""

    @pytest.mark.asyncio
    async def test_asetnx_backend_error_raises_adapter_connection_error(self, adapter):
        adapter._redis.set.side_effect = ConnectionError("redis down")

        with pytest.raises(AdapterConnectionError, match="asetnx failed"):
            await adapter.asetnx("k", {"status": "executing"})

    @pytest.mark.asyncio
    async def test_asetnx_wraps_cause_without_leaking_backend_type(self, adapter):
        cause = ConnectionError("redis down")
        adapter._redis.set.side_effect = cause

        with pytest.raises(AdapterConnectionError) as exc_info:
            await adapter.asetnx("k", {"status": "executing"})

        assert exc_info.value.__cause__ is cause

    @pytest.mark.asyncio
    async def test_acas_takeover_backend_error_raises_adapter_connection_error(
        self, adapter
    ):
        adapter._redis.eval.side_effect = ConnectionError("redis down")

        with pytest.raises(AdapterConnectionError, match="acas_takeover failed"):
            await adapter.acas_takeover("k", _NEW, stale_before=1.0)

    @pytest.mark.asyncio
    async def test_acas_takeover_wraps_cause_without_leaking_backend_type(
        self, adapter
    ):
        cause = ConnectionError("redis down")
        adapter._redis.eval.side_effect = cause

        with pytest.raises(AdapterConnectionError) as exc_info:
            await adapter.acas_takeover("k", _NEW, stale_before=1.0)

        assert exc_info.value.__cause__ is cause
