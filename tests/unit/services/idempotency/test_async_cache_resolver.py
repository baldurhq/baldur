"""Unit tests for resolve_async_cache (672 D7).

Async cache resolution reuses ``resolve_cache_via_registry`` for the
production-fail-closed decision, then selects the async backing by the resolved
(unwrapped) sync cache's ``provider_name``: ``"redis"`` ⇒ a fresh
``AsyncRedisCacheAdapter`` (pool-drain registered); anything else ⇒ the async
in-memory fallback.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction — provider topology detection drives the backing
  selection; the redis branch registers the async pool drain.
- §8.6 Delegate unwrap — a metrics-wrapped sync cache's real provider is read
  through ``_delegate``.
- §8.10 Fail-closed — the prod-no-adapter ``ConfigurationError`` from the reused
  sync resolver propagates through unchanged.
"""

# NOTE: no ``from __future__ import annotations`` — keep parity with the sibling
# resolver test module; nothing here needs deferred annotations.

from unittest.mock import patch

import pytest

from baldur.adapters.cache.async_memory_adapter import AsyncInMemoryCacheAdapter
from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
from baldur.core.exceptions import AdapterNotFoundError, ConfigurationError
from baldur.services.idempotency import _cache_resolver as resolver_module
from baldur.services.idempotency._cache_resolver import resolve_async_cache


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    from baldur.services.idempotency._cache_resolver import (
        _reset_service_fallback_cache,
        _reset_warned_layers,
    )

    _reset_warned_layers()
    _reset_service_fallback_cache()
    yield
    _reset_warned_layers()
    _reset_service_fallback_cache()


@pytest.fixture
def _reset_settings_and_runtime():
    from baldur.runtime import reset_runtime
    from baldur.settings.idempotency import reset_idempotency_settings

    reset_idempotency_settings()
    reset_runtime()
    yield
    reset_idempotency_settings()
    reset_runtime()


class _StubCache:
    """A concrete sync cache reporting a chosen ``provider_name``."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider_name(self) -> str:
        return self._provider


class _MetricsWrapper:
    """Models ``MetricsAwareCacheAdapter`` — delegates provider to ``_delegate``."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate

    @property
    def provider_name(self) -> str:
        return self._delegate.provider_name


def _fallbacks():
    """A fresh (sync, async) fallback pair for a resolve call."""
    return (
        InMemoryCacheAdapter(key_prefix="sync_fb:"),
        AsyncInMemoryCacheAdapter(key_prefix="async_fb:"),
    )


# =============================================================================
# Behavior — topology detection selects the async backing (672 D7)
# =============================================================================


class TestResolveAsyncCacheTopology:
    """The resolved sync provider name selects async-redis vs async-in-memory."""

    def test_redis_provider_selects_async_redis_adapter_and_registers_drain(self):
        sync_fb, async_fb = _fallbacks()

        with (
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=_StubCache("redis"),
            ),
            patch(
                "baldur.adapters.cache.async_redis_adapter.AsyncRedisCacheAdapter"
            ) as mock_adapter,
            patch.object(resolver_module, "_register_async_pool_drain") as mock_reg,
        ):
            resolved = resolve_async_cache(
                layer="policy",
                sync_fallback_cache=sync_fb,
                async_fallback_cache=async_fb,
                raise_on_prod_no_toggle=True,
            )

        # The fresh async Redis adapter is returned, and its pool drain is wired.
        assert resolved is mock_adapter.return_value
        mock_reg.assert_called_once_with(mock_adapter.return_value)

    def test_redis_provider_detected_through_metrics_wrapper(self):
        """A metrics-wrapped sync cache still resolves to the async Redis backing —
        the wrapper's ``_delegate`` is unwrapped before reading provider_name."""
        sync_fb, async_fb = _fallbacks()
        wrapped = _MetricsWrapper(_StubCache("redis"))

        with (
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=wrapped,
            ),
            patch(
                "baldur.adapters.cache.async_redis_adapter.AsyncRedisCacheAdapter"
            ) as mock_adapter,
            patch.object(resolver_module, "_register_async_pool_drain"),
        ):
            resolved = resolve_async_cache(
                layer="policy",
                sync_fallback_cache=sync_fb,
                async_fallback_cache=async_fb,
                raise_on_prod_no_toggle=True,
            )

        assert resolved is mock_adapter.return_value

    def test_non_redis_provider_returns_async_in_memory_fallback(self):
        """A non-redis (e.g. registered memory) provider returns the caller's
        async in-memory fallback — no Redis adapter constructed, no drain wired."""
        sync_fb, async_fb = _fallbacks()

        with (
            patch(
                "baldur.factory.registry.ProviderRegistry.get_cache",
                return_value=_StubCache("memory"),
            ),
            patch(
                "baldur.adapters.cache.async_redis_adapter.AsyncRedisCacheAdapter"
            ) as mock_adapter,
            patch.object(resolver_module, "_register_async_pool_drain") as mock_reg,
        ):
            resolved = resolve_async_cache(
                layer="policy",
                sync_fallback_cache=sync_fb,
                async_fallback_cache=async_fb,
                raise_on_prod_no_toggle=True,
            )

        assert resolved is async_fb
        mock_adapter.assert_not_called()
        mock_reg.assert_not_called()

    def test_no_adapter_dev_returns_async_in_memory_fallback(self, monkeypatch):
        """No registered adapter in dev → the sync resolver returns the sync
        fallback (provider 'memory') → the async in-memory fallback is chosen."""
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "development")
        sync_fb, async_fb = _fallbacks()

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            resolved = resolve_async_cache(
                layer="policy",
                sync_fallback_cache=sync_fb,
                async_fallback_cache=async_fb,
                raise_on_prod_no_toggle=True,
            )

        assert resolved is async_fb


# =============================================================================
# Behavior — prod-fail-closed propagates from the reused sync resolver (D7)
# =============================================================================


class TestResolveAsyncCacheFailClosed:
    """The fail-closed correctness logic is NOT duplicated — the sync resolver's
    ``ConfigurationError`` (production + no adapter + no escape hatch) propagates
    out of ``resolve_async_cache`` unchanged."""

    def test_prod_no_adapter_no_escape_raises_configuration_error(
        self, monkeypatch, _reset_settings_and_runtime
    ):
        monkeypatch.setenv("BALDUR_ENVIRONMENT", "production")
        monkeypatch.setenv("BALDUR_IDEMPOTENCY_ALLOW_INMEMORY_FALLBACK", "false")
        from baldur.runtime import reset_runtime
        from baldur.settings.idempotency import reset_idempotency_settings

        reset_idempotency_settings()
        reset_runtime()

        sync_fb, async_fb = _fallbacks()

        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            with patch.object(resolver_module, "_record_fallback_metric"):
                with pytest.raises(ConfigurationError):
                    resolve_async_cache(
                        layer="policy",
                        sync_fallback_cache=sync_fb,
                        async_fallback_cache=async_fb,
                        raise_on_prod_no_toggle=True,
                    )
