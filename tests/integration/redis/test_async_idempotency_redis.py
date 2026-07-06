"""AsyncRedisCacheAdapter + AsyncIdempotencyGate against real Redis (672 D8).

Two cross-component behaviors the in-memory unit double cannot prove:

1. **Sync/async cross-consistency on ONE key** — a sync ``IdempotencyGate`` over
   ``RedisCacheAdapter`` and an async ``AsyncIdempotencyGate`` over
   ``AsyncRedisCacheAdapter`` sharing the same key prefix write/read the SAME
   Redis keys with the SAME serialization, so they dedup against each other.
2. **``asetnx`` atomicity under genuine concurrency** — ``asyncio.gather`` of N
   acquires on one key against a live server yields exactly one winner (real
   ``SET NX`` contention, which the 2-way fake-cache unit test cannot exercise).

All tests require a running Redis instance (auto-skipped via
``pytestmark = pytest.mark.requires_redis``).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

from baldur.adapters.cache.async_redis_adapter import AsyncRedisCacheAdapter
from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.idempotency_gate import (
    AsyncIdempotencyGate,
    IdempotencyDecision,
    IdempotencyGate,
)

pytestmark = pytest.mark.requires_redis

_PREFIX = "test:aidem:"


@pytest_asyncio.fixture
async def async_cache(redis_url) -> AsyncRedisCacheAdapter:
    """AsyncRedisCacheAdapter over the test Redis; pool drained on teardown."""
    cache = AsyncRedisCacheAdapter(
        url=redis_url,
        key_prefix=_PREFIX,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )
    yield cache
    await cache.aclose()


# =============================================================================
# AsyncRedisCacheAdapter — raw dedup ops against real Redis
# =============================================================================


class TestAsyncRedisAdapterOps:
    """The four async dedup ops behave against a live server."""

    @pytest.mark.asyncio
    async def test_asetnx_acquires_once_then_loses(self, async_cache):
        assert await async_cache.asetnx("k", {"status": "executing"}) is True
        # Second acquire on the held key loses.
        assert await async_cache.asetnx("k", {"status": "executing"}) is False
        assert await async_cache.aget("k") == {"status": "executing"}

    @pytest.mark.asyncio
    async def test_acas_dict_field_transitions_executing_to_completed(
        self, async_cache
    ):
        await async_cache.asetnx("k", {"status": "executing", "n": 1})

        swapped = await async_cache.acas_dict_field(
            "k", "status", "executing", {"status": "completed", "result": {"ok": True}}
        )

        assert swapped is True
        assert (await async_cache.aget("k"))["status"] == "completed"

    @pytest.mark.asyncio
    async def test_acas_dict_field_mismatch_does_not_write(self, async_cache):
        await async_cache.asetnx("k", {"status": "completed"})

        swapped = await async_cache.acas_dict_field(
            "k", "status", "executing", {"status": "failed"}
        )

        assert swapped is False
        assert (await async_cache.aget("k"))["status"] == "completed"

    @pytest.mark.asyncio
    async def test_adelete_removes_key(self, async_cache):
        await async_cache.asetnx("k", {"status": "executing"})
        assert await async_cache.adelete("k") is True
        assert await async_cache.aget("k") is None

    @pytest.mark.asyncio
    async def test_asetnx_ttl_expires_key(self, async_cache):
        await async_cache.asetnx("k", {"status": "executing"}, ttl=timedelta(seconds=1))
        assert await async_cache.aget("k") == {"status": "executing"}

        await asyncio.sleep(1.4)  # real Redis expiry needs real elapsed time

        assert await async_cache.aget("k") is None
        # Key expired → re-acquirable.
        assert await async_cache.asetnx("k", {"status": "executing"}) is True


# =============================================================================
# AsyncIdempotencyGate — end-to-end over real Redis
# =============================================================================


class TestAsyncGateOverRealRedis:
    """The awaitable gate's acquire/mark decisions against a live server."""

    @pytest.mark.asyncio
    async def test_acquire_then_mark_completed_makes_duplicate_skip(self, async_cache):
        gate = AsyncIdempotencyGate(cache=async_cache)

        first = await gate.check_and_acquire("order:1", ttl=timedelta(seconds=30))
        assert first.decision == IdempotencyDecision.CONTINUE

        await gate.mark_completed("order:1", result={"charged": True})

        dup = await gate.check_and_acquire("order:1", ttl=timedelta(seconds=30))
        assert dup.decision == IdempotencyDecision.SKIP
        assert dup.cached_result == {"charged": True}

    @pytest.mark.asyncio
    async def test_inflight_duplicate_aborts(self, async_cache):
        gate = AsyncIdempotencyGate(cache=async_cache)

        assert (
            await gate.check_and_acquire("order:2", ttl=timedelta(seconds=30))
        ).decision == IdempotencyDecision.CONTINUE
        # Un-marked in-flight claim → concurrent duplicate ABORTs.
        assert (
            await gate.check_and_acquire("order:2", ttl=timedelta(seconds=30))
        ).decision == IdempotencyDecision.ABORT


# =============================================================================
# Sync/async cross-consistency on ONE key (shared prefix + serialization)
# =============================================================================


class TestSyncAsyncCrossConsistency:
    """A sync gate and an async gate on the same key prefix hit the SAME Redis
    keys, so a record written by one is observed by the other."""

    @pytest.mark.asyncio
    async def test_sync_acquire_blocks_async_and_completion_makes_async_skip(
        self, redis_url
    ):
        sync_cache = RedisCacheAdapter(
            url=redis_url,
            key_prefix=_PREFIX,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        async_cache = AsyncRedisCacheAdapter(
            url=redis_url,
            key_prefix=_PREFIX,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        try:
            sync_gate = IdempotencyGate(cache=sync_cache)
            async_gate = AsyncIdempotencyGate(cache=async_cache)
            key = "xconsist:o-1"

            # Sync acquires the key (writes an EXECUTING record to Redis).
            assert (
                sync_gate.check_and_acquire(key, ttl=timedelta(seconds=30)).decision
                == IdempotencyDecision.CONTINUE
            )
            # Async, hitting the SAME Redis key, sees the in-flight claim → ABORT.
            assert (
                await async_gate.check_and_acquire(key, ttl=timedelta(seconds=30))
            ).decision == IdempotencyDecision.ABORT

            # Sync completes; async now reads the completed record → SKIP.
            sync_gate.mark_completed(key, result={"who": "sync"})
            dup = await async_gate.check_and_acquire(key, ttl=timedelta(seconds=30))
            assert dup.decision == IdempotencyDecision.SKIP
            assert dup.cached_result == {"who": "sync"}
        finally:
            await async_cache.aclose()

    @pytest.mark.asyncio
    async def test_async_acquire_blocks_sync(self, redis_url):
        sync_cache = RedisCacheAdapter(
            url=redis_url,
            key_prefix=_PREFIX,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        async_cache = AsyncRedisCacheAdapter(
            url=redis_url,
            key_prefix=_PREFIX,
            socket_timeout=5.0,
            socket_connect_timeout=5.0,
        )
        try:
            sync_gate = IdempotencyGate(cache=sync_cache)
            async_gate = AsyncIdempotencyGate(cache=async_cache)
            key = "xconsist:o-2"

            assert (
                await async_gate.check_and_acquire(key, ttl=timedelta(seconds=30))
            ).decision == IdempotencyDecision.CONTINUE
            # Sync, on the same Redis key, sees the async-written claim → ABORT.
            assert (
                sync_gate.check_and_acquire(key, ttl=timedelta(seconds=30)).decision
                == IdempotencyDecision.ABORT
            )
        finally:
            await async_cache.aclose()


# =============================================================================
# asetnx atomicity under genuine concurrency (real SET NX contention)
# =============================================================================


class TestAsyncConcurrentAcquire:
    """``asyncio.gather`` of N acquires on ONE key ⇒ exactly one winner."""

    @pytest.mark.asyncio
    async def test_gather_n_asetnx_elects_single_winner(self, async_cache):
        results = await asyncio.gather(
            *(
                async_cache.asetnx("race", {"who": i}, ttl=timedelta(seconds=30))
                for i in range(25)
            )
        )

        assert results.count(True) == 1
        assert results.count(False) == 24

    @pytest.mark.asyncio
    async def test_gather_n_gate_acquires_yield_single_continue(self, async_cache):
        gate = AsyncIdempotencyGate(cache=async_cache)

        results = await asyncio.gather(
            *(
                gate.check_and_acquire("race-gate", ttl=timedelta(seconds=30))
                for _ in range(25)
            )
        )

        decisions = [r.decision for r in results]
        assert decisions.count(IdempotencyDecision.CONTINUE) == 1
        # Every loser is a definite ABORT (no double-execute, no CONTINUE leak).
        assert decisions.count(IdempotencyDecision.ABORT) == 24
