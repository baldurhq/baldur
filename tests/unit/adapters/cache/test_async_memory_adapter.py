"""Unit tests for AsyncInMemoryCacheAdapter (672 D8).

Async twin of InMemoryCacheAdapter, scoped to the minimal
AsyncCacheProviderInterface dedup surface (asetnx / aget / acas_dict_field /
adelete). Backs the async idempotency fallback path and is itself the async
unit-test double for AsyncIdempotencyGate.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction — asetnx set-if-absent, adelete existence report.
- §8.9 Concurrency — asetnx / acas_dict_field are loop-atomic (no await between
  read and write): a concurrent gather on one key elects exactly one winner.
- §8.7 Time dependency — TTL expiry via patched module ``time.time`` (no
  ``time.sleep``); an expired entry reads as absent and is re-acquirable.
- §8.6 Serialization/isolation — key_prefix keeps distinct instances from
  colliding.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.cache.async_memory_adapter import AsyncInMemoryCacheAdapter

_TIME = "baldur.adapters.cache.async_memory_adapter.time.time"


# =============================================================================
# Contract — provider identity
# =============================================================================


class TestAsyncInMemoryAdapterContract:
    def test_provider_name_is_memory(self):
        """provider_name drives the async cache resolver's topology detection —
        it must report 'memory' so the resolver picks the in-memory backing."""
        assert AsyncInMemoryCacheAdapter().provider_name == "memory"


# =============================================================================
# Behavior — asetnx / aget / adelete
# =============================================================================


class TestAsyncInMemoryAdapterBehavior:
    """Awaited op semantics for the async in-memory dedup adapter."""

    @pytest.mark.asyncio
    async def test_asetnx_sets_when_absent_and_is_readable(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="b:")

        acquired = await cache.asetnx("k", {"status": "executing"})

        assert acquired is True
        assert await cache.aget("k") == {"status": "executing"}

    @pytest.mark.asyncio
    async def test_asetnx_returns_false_when_present_and_does_not_overwrite(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="b:")
        await cache.asetnx("k", {"v": 1})

        second = await cache.asetnx("k", {"v": 2})

        assert second is False
        assert await cache.aget("k") == {"v": 1}  # original preserved

    @pytest.mark.asyncio
    async def test_aget_missing_key_returns_none(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="b:")
        assert await cache.aget("nope") is None

    @pytest.mark.asyncio
    async def test_adelete_existing_returns_true_missing_returns_false(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="b:")
        await cache.asetnx("k", {"v": 1})

        assert await cache.adelete("k") is True
        assert await cache.adelete("k") is False  # already gone
        assert await cache.aget("k") is None

    @pytest.mark.asyncio
    async def test_clear_all_empties_the_store(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="b:")
        await cache.asetnx("k1", {"v": 1})
        await cache.asetnx("k2", {"v": 2})

        cache.clear_all()

        assert await cache.aget("k1") is None
        assert await cache.aget("k2") is None

    @pytest.mark.asyncio
    async def test_aclose_is_noop_and_does_not_raise(self):
        """The in-memory adapter holds no sockets — aclose is a symmetric no-op."""
        cache = AsyncInMemoryCacheAdapter()
        await cache.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_key_prefix_isolates_distinct_instances(self):
        """Two adapters with different prefixes cannot collide on one logical key."""
        a = AsyncInMemoryCacheAdapter(key_prefix="layerA:")
        b = AsyncInMemoryCacheAdapter(key_prefix="layerB:")

        assert await a.asetnx("shared", {"who": "a"}) is True
        # b's store is separate — the same logical key is still free for b.
        assert await b.asetnx("shared", {"who": "b"}) is True
        assert await a.aget("shared") == {"who": "a"}
        assert await b.aget("shared") == {"who": "b"}


# =============================================================================
# Behavior — acas_dict_field single-field CAS
# =============================================================================


class TestAsyncInMemoryAdapterCasBehavior:
    """acas_dict_field replaces the record only on a matching field."""

    @pytest.mark.asyncio
    async def test_cas_matching_field_replaces_record(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="c:")
        await cache.asetnx("k", {"status": "executing", "n": 1})

        swapped = await cache.acas_dict_field(
            "k", "status", "executing", {"status": "completed"}
        )

        assert swapped is True
        assert await cache.aget("k") == {"status": "completed"}

    @pytest.mark.asyncio
    async def test_cas_field_mismatch_returns_false_without_write(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="c:")
        await cache.asetnx("k", {"status": "completed"})

        swapped = await cache.acas_dict_field(
            "k", "status", "executing", {"status": "failed"}
        )

        assert swapped is False
        assert await cache.aget("k") == {"status": "completed"}  # unchanged

    @pytest.mark.asyncio
    async def test_cas_missing_key_returns_false(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="c:")
        assert (
            await cache.acas_dict_field("absent", "status", "executing", {"x": 1})
        ) is False

    @pytest.mark.asyncio
    async def test_cas_non_dict_value_returns_false(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="c:")
        await cache.asetnx("k", "not-a-dict")

        assert (
            await cache.acas_dict_field("k", "status", "executing", {"x": 1})
        ) is False


# =============================================================================
# Behavior — acas_takeover failed / stale-executing takeover (673 D1 / G1)
# =============================================================================


class TestAsyncInMemoryAdapterCasTakeoverBehavior:
    """``acas_takeover`` replaces the record IFF it is failed OR stale-executing;
    loop-atomic (no await between read/write), so a fresh claim is single-winner."""

    @pytest.mark.asyncio
    async def test_failed_record_is_taken_over(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        await cache.asetnx("k", {"status": "failed", "retry_count": 1})

        taken = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=0.0
        )

        assert taken is True
        assert (await cache.aget("k"))["status"] == "executing"

    @pytest.mark.asyncio
    async def test_stale_executing_record_is_taken_over(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        await cache.asetnx("k", {"status": "executing", "started_at": 100.0})

        taken = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 500.0}, stale_before=200.0
        )

        assert taken is True
        assert (await cache.aget("k"))["started_at"] == 500.0

    @pytest.mark.asyncio
    async def test_fresh_executing_record_is_not_taken_over(self):
        """A claim younger than ``stale_before`` survives (no double-execute)."""
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        fresh = {"status": "executing", "started_at": 100.0}
        await cache.asetnx("k", dict(fresh))

        taken = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 999.0}, stale_before=50.0
        )

        assert taken is False
        assert await cache.aget("k") == fresh

    @pytest.mark.asyncio
    async def test_completed_record_is_not_taken_over(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        await cache.asetnx("k", {"status": "completed", "result": {"ok": True}})

        taken = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 1.0}, stale_before=1e12
        )

        assert taken is False
        assert (await cache.aget("k"))["status"] == "completed"

    @pytest.mark.asyncio
    async def test_missing_or_non_dict_record_is_not_taken_over(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        assert (
            await cache.acas_takeover(
                "absent", {"status": "executing", "started_at": 1.0}, stale_before=1e12
            )
        ) is False

        await cache.asetnx("bad", "not-a-dict")
        assert (
            await cache.acas_takeover(
                "bad", {"status": "executing", "started_at": 1.0}, stale_before=1e12
            )
        ) is False

    @pytest.mark.asyncio
    async def test_second_takeover_after_fresh_claim_loses(self):
        """Single-winner: the second takeover on the winner's fresh claim loses."""
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        await cache.asetnx("k", {"status": "failed"})

        first = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=500.0
        )
        second = await cache.acas_takeover(
            "k", {"status": "executing", "started_at": 1000.0}, stale_before=500.0
        )

        assert first is True
        assert second is False
        assert (await cache.aget("k"))["started_at"] == 1000.0

    @pytest.mark.asyncio
    async def test_concurrent_takeover_on_failed_record_elects_single_winner(self):
        """A gather of N takeovers on one seeded failed record → exactly one win
        (loop-atomic: the first rewrites to a fresh claim, the rest see it fresh)."""
        cache = AsyncInMemoryCacheAdapter(key_prefix="ct:")
        await cache.asetnx("race", {"status": "failed"})

        results = await asyncio.gather(
            *(
                cache.acas_takeover(
                    "race",
                    {"status": "executing", "started_at": 1000.0 + i},
                    stale_before=500.0,
                )
                for i in range(20)
            )
        )

        assert results.count(True) == 1
        assert results.count(False) == 19


# =============================================================================
# Behavior — TTL expiry (time dependency, no time.sleep)
# =============================================================================


class TestAsyncInMemoryAdapterTtlBehavior:
    """A TTL'd entry reads as present within the window and absent after it,
    and an expired key is re-acquirable via asetnx."""

    @pytest.mark.asyncio
    async def test_entry_readable_within_ttl_and_none_after_expiry(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="t:")

        with patch(_TIME, return_value=1000.0):
            await cache.asetnx("k", {"v": 1}, ttl=timedelta(seconds=10))

        # Within window (t=1005): still present.
        with patch(_TIME, return_value=1005.0):
            assert await cache.aget("k") == {"v": 1}

        # Past window (t=1011): expired → None.
        with patch(_TIME, return_value=1011.0):
            assert await cache.aget("k") is None

    @pytest.mark.asyncio
    async def test_expired_key_is_reacquirable_via_asetnx(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="t:")

        with patch(_TIME, return_value=1000.0):
            assert await cache.asetnx("k", {"v": 1}, ttl=timedelta(seconds=10)) is True

        # After expiry, the key is free again — asetnx wins.
        with patch(_TIME, return_value=1011.0):
            assert await cache.asetnx("k", {"v": 2}, ttl=timedelta(seconds=10)) is True

    @pytest.mark.asyncio
    async def test_cas_on_expired_entry_returns_false(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="t:")

        with patch(_TIME, return_value=1000.0):
            await cache.asetnx("k", {"status": "executing"}, ttl=timedelta(seconds=10))

        with patch(_TIME, return_value=1011.0):
            swapped = await cache.acas_dict_field(
                "k", "status", "executing", {"status": "completed"}
            )

        assert swapped is False


# =============================================================================
# Behavior — loop atomicity (no await between read and write)
# =============================================================================


class TestAsyncInMemoryAdapterAtomicityBehavior:
    """asetnx / acas_dict_field never await between the check and the write, so a
    concurrent gather on one key elects exactly one winner."""

    @pytest.mark.asyncio
    async def test_concurrent_asetnx_elects_single_winner(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="a:")

        results = await asyncio.gather(
            *(cache.asetnx("race", {"who": i}) for i in range(20))
        )

        assert results.count(True) == 1
        assert results.count(False) == 19

    @pytest.mark.asyncio
    async def test_concurrent_cas_elects_single_winner(self):
        cache = AsyncInMemoryCacheAdapter(key_prefix="a:")
        await cache.asetnx("k", {"status": "executing"})

        # Many coroutines race to transition executing → their own terminal
        # state; the atomic CAS lets exactly one win.
        results = await asyncio.gather(
            *(
                cache.acas_dict_field(
                    "k", "status", "executing", {"status": f"done-{i}"}
                )
                for i in range(20)
            )
        )

        assert results.count(True) == 1
