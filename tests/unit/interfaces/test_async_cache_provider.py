"""Unit tests for AsyncCacheProviderInterface (672 D6).

The minimal awaitable dedup surface (``asetnx`` / ``aget`` / ``acas_dict_field``
/ ``adelete``) that ``AsyncIdempotencyGate`` awaits. Deliberately NOT a full
async parity of ``CacheProviderInterface``.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.1 Boundary analysis — the base ``asetnx`` / ``acas_dict_field`` are
  non-atomic (raising) placeholders that production adapters MUST override; a
  minimal subclass inheriting the default raises, an override does not.
- §8.5 Dependency interaction / structural contract — ``aget`` / ``adelete``
  are abstract (a subclass omitting either cannot be instantiated), while
  ``asetnx`` / ``acas_dict_field`` are concrete (raising) defaults, so they are
  NOT in ``__abstractmethods__``.
"""

from __future__ import annotations

from typing import Any

import pytest

from baldur.interfaces.cache_provider import AsyncCacheProviderInterface


class _MinimalAsyncCache(AsyncCacheProviderInterface):
    """Implements only the two abstract methods, inheriting the raising
    ``asetnx`` / ``acas_dict_field`` base defaults (models a non-overriding
    adapter author)."""

    async def aget(self, key: str) -> Any | None:
        return None

    async def adelete(self, key: str) -> bool:
        return False


# =============================================================================
# Contract — abstract surface + non-atomic-default raise (672 D6)
# =============================================================================


class TestAsyncCacheProviderContract:
    """The minimal async dedup surface's abstract-method + atomicity contract."""

    def test_aget_and_adelete_are_abstract(self):
        """``aget`` / ``adelete`` are the two abstract methods; the atomic
        primitives (``asetnx`` / ``acas_dict_field``) are concrete raising
        defaults, so they are NOT abstract."""
        abstract = AsyncCacheProviderInterface.__abstractmethods__
        assert "aget" in abstract
        assert "adelete" in abstract
        assert "asetnx" not in abstract
        assert "acas_dict_field" not in abstract

    def test_subclass_missing_abstract_method_cannot_instantiate(self):
        """A subclass omitting ``adelete`` cannot be instantiated (ABC contract)."""

        class _NoDelete(AsyncCacheProviderInterface):
            async def aget(self, key: str) -> Any | None:
                return None

        with pytest.raises(TypeError, match="adelete"):
            _NoDelete()  # type: ignore[abstract]

    @pytest.mark.asyncio
    async def test_base_asetnx_default_raises_not_implemented(self):
        """The minimal surface has no ``aset`` primitive, so the base ``asetnx``
        cannot supply a working default — it raises to make the "override with
        an atomic implementation" contract explicit (the gate's validator
        catches a non-overriding adapter before this body is reached)."""
        cache = _MinimalAsyncCache()
        with pytest.raises(NotImplementedError, match="atomic"):
            await cache.asetnx("k", {"status": "executing"})

    @pytest.mark.asyncio
    async def test_base_acas_dict_field_default_raises_not_implemented(self):
        """The base ``acas_dict_field`` raises for the same reason as ``asetnx``
        (no read-modify-write primitive to build a non-atomic default from)."""
        cache = _MinimalAsyncCache()
        with pytest.raises(NotImplementedError, match="atomic"):
            await cache.acas_dict_field("k", "status", "executing", {"status": "done"})

    @pytest.mark.asyncio
    async def test_base_asetnx_raise_names_the_offending_subclass(self):
        """The raise message names the concrete type so a misconfigured adapter
        author sees WHICH class failed to override."""
        cache = _MinimalAsyncCache()
        with pytest.raises(NotImplementedError, match="_MinimalAsyncCache"):
            await cache.asetnx("k", {"status": "executing"})


# =============================================================================
# Contract — a genuine atomic override satisfies the surface (672 D6/D8)
# =============================================================================


class TestAsyncCacheProviderOverrideContract:
    """An adapter that overrides the atomic primitives does NOT hit the raise."""

    @pytest.mark.asyncio
    async def test_override_asetnx_does_not_raise(self):
        """``AsyncInMemoryCacheAdapter`` overrides ``asetnx`` with an atomic
        implementation, so awaiting it returns a bool rather than raising."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )

        cache = AsyncInMemoryCacheAdapter(key_prefix="ovr:")
        acquired = await cache.asetnx("k", {"status": "executing"})
        assert acquired is True

    @pytest.mark.asyncio
    async def test_override_acas_dict_field_does_not_raise(self):
        """``AsyncInMemoryCacheAdapter`` overrides ``acas_dict_field`` — awaiting
        it returns a bool rather than raising."""
        from baldur.adapters.cache.async_memory_adapter import (
            AsyncInMemoryCacheAdapter,
        )

        cache = AsyncInMemoryCacheAdapter(key_prefix="ovr:")
        await cache.asetnx("k", {"status": "executing"})
        swapped = await cache.acas_dict_field(
            "k", "status", "executing", {"status": "completed"}
        )
        assert swapped is True
