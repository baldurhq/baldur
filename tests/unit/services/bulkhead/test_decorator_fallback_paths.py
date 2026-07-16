"""
Cover the fallback + error-raise branches in
`baldur.services.bulkhead.decorator` that the resilience-suite tests miss:

- `bulkhead()` async + non-coroutine fallback (lines 127-128).
- `bulkhead()` async + result.error raise (lines 144-145).
- `bulkhead_for_database()` sync+async paths with fallback and explicit
  error-raise paths (lines 230-251, 261-262, 278-287, 293-294).
- `bulkhead_for_cache()` symmetrical (lines 346-367, 377-378, 394-403, 409-410).
"""

from __future__ import annotations

import pytest

from baldur.core.connection_health import ConnectionType
from baldur.services.bulkhead.decorator import (
    bulkhead,
    bulkhead_for_cache,
    bulkhead_for_database,
)
from baldur.services.bulkhead.exceptions import BulkheadFullError
from baldur.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.settings.bulkhead import reset_bulkhead_settings


@pytest.fixture(autouse=True)
def _empty_provider_slot(monkeypatch):
    """Pin the resolution chain to its fallback leg for this module.

    These tests exercise the base registry/decorator semantics; a populated
    provider slot (e.g. a registry overlay registered by another test's
    environment) would leak saturated compartments across tests because
    reset_bulkhead_registry() clears only the fallback leg.
    """
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
    )


@pytest.fixture(autouse=True)
def reset_singletons():
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()


def _occupy_sync(bulkhead_instance):
    """Saturate a sync bulkhead so the next acquire fails fast."""
    max_concurrent = bulkhead_instance.get_state().max_concurrent
    for _ in range(max_concurrent):
        bulkhead_instance.try_acquire()
    return max_concurrent


async def _occupy_async(async_bulkhead):
    max_concurrent = async_bulkhead.get_state().max_concurrent
    for _ in range(max_concurrent):
        await async_bulkhead.try_acquire()
    return max_concurrent


class TestBulkheadAsyncFallback:
    """`bulkhead()` async wrapper — fallback variants."""

    @pytest.mark.asyncio
    async def test_async_wrapper_uses_sync_fallback(self):
        registry = get_bulkhead_registry()
        async_bh = registry.get_async(ConnectionType.DATABASE)
        await _occupy_async(async_bh)

        def sync_fallback(*args, **kwargs):
            return "sync_fallback_value"

        @bulkhead(ConnectionType.DATABASE, fallback=sync_fallback)
        async def primary():
            return "primary"

        assert await primary() == "sync_fallback_value"

    @pytest.mark.asyncio
    async def test_async_wrapper_raises_business_error_through_result(self):
        # No fallback path → business error from the wrapped function must
        # be re-raised by `if result.error: raise result.error`.
        @bulkhead(ConnectionType.DATABASE)
        async def failing():
            raise ValueError("business failure")

        with pytest.raises(ValueError, match="business failure"):
            await failing()


class TestBulkheadForDatabaseSync:
    """Sync path for `bulkhead_for_database` including fallback + raise."""

    def test_sync_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_database("default")
        _occupy_sync(bh)

        def fallback(x: int) -> int:
            return x * 100

        @bulkhead_for_database("default", fallback=fallback)
        def write(x: int) -> int:
            return x

        assert write(3) == 300

    def test_sync_raises_business_error(self):
        @bulkhead_for_database("default")
        def fail() -> None:
            raise RuntimeError("db boom")

        with pytest.raises(RuntimeError, match="db boom"):
            fail()


class TestBulkheadForDatabaseAsync:
    """Async path for `bulkhead_for_database` including fallback + raise."""

    @pytest.mark.asyncio
    async def test_async_sync_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_database("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        def sync_fallback(x: int) -> int:
            return x + 100

        @bulkhead_for_database("default", fallback=sync_fallback)
        async def write(x: int) -> int:
            return x

        assert await write(5) == 105

    @pytest.mark.asyncio
    async def test_async_coroutine_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_database("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        async def async_fallback(x: int) -> int:
            return x - 1

        @bulkhead_for_database("default", fallback=async_fallback)
        async def write(x: int) -> int:
            return x

        assert await write(10) == 9

    @pytest.mark.asyncio
    async def test_async_raises_business_error(self):
        @bulkhead_for_database("default")
        async def fail() -> None:
            raise RuntimeError("async db boom")

        with pytest.raises(RuntimeError, match="async db boom"):
            await fail()


class TestBulkheadForCacheSync:
    """Sync path for `bulkhead_for_cache` including fallback + raise."""

    def test_sync_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_cache("default")
        _occupy_sync(bh)

        def fallback() -> str:
            return "cache_fallback"

        @bulkhead_for_cache("default", fallback=fallback)
        def get_value() -> str:
            return "primary"

        assert get_value() == "cache_fallback"

    def test_sync_raises_business_error(self):
        @bulkhead_for_cache("default")
        def fail() -> None:
            raise KeyError("cache miss policy boom")

        with pytest.raises(KeyError):
            fail()

    def test_sync_business_error_not_overridden_by_fallback(self):
        # The fallback predicate is _bulkhead_full_predicate — business
        # exceptions must NOT trigger fallback.
        def fallback() -> str:
            return "should_not_appear"

        @bulkhead_for_cache("default", fallback=fallback)
        def fail() -> None:
            raise RuntimeError("real failure")

        with pytest.raises(RuntimeError, match="real failure"):
            fail()


class TestBulkheadForCacheAsync:
    """Async path for `bulkhead_for_cache` including fallback + raise."""

    @pytest.mark.asyncio
    async def test_async_sync_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_cache("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        def sync_fallback() -> str:
            return "async_cache_sync_fb"

        @bulkhead_for_cache("default", fallback=sync_fallback)
        async def get_value() -> str:
            return "primary"

        assert await get_value() == "async_cache_sync_fb"

    @pytest.mark.asyncio
    async def test_async_coroutine_fallback_invoked_when_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_cache("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        async def async_fallback() -> str:
            return "async_cache_async_fb"

        @bulkhead_for_cache("default", fallback=async_fallback)
        async def get_value() -> str:
            return "primary"

        assert await get_value() == "async_cache_async_fb"

    @pytest.mark.asyncio
    async def test_async_raises_business_error(self):
        @bulkhead_for_cache("default")
        async def fail() -> None:
            raise KeyError("async cache boom")

        with pytest.raises(KeyError):
            await fail()


class TestBulkheadFullErrorPropagationWithoutFallback:
    """No fallback → BulkheadFullError reaches the caller via result.error."""

    def test_database_sync_raises_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_database("default")
        _occupy_sync(bh)

        @bulkhead_for_database("default")
        def write() -> None:
            pass

        with pytest.raises(BulkheadFullError):
            write()

    def test_cache_sync_raises_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_cache("default")
        _occupy_sync(bh)

        @bulkhead_for_cache("default")
        def read() -> None:
            pass

        with pytest.raises(BulkheadFullError):
            read()

    @pytest.mark.asyncio
    async def test_database_async_raises_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_database("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        @bulkhead_for_database("default")
        async def write() -> None:
            pass

        with pytest.raises(BulkheadFullError):
            await write()

    @pytest.mark.asyncio
    async def test_cache_async_raises_bulkhead_full(self):
        registry = get_bulkhead_registry()
        bh = registry.get_for_cache("default")
        async_bh = registry.get_async(bh.name)
        await _occupy_async(async_bh)

        @bulkhead_for_cache("default")
        async def read() -> None:
            pass

        with pytest.raises(BulkheadFullError):
            await read()
