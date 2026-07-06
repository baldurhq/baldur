"""
ProviderRegistry cache metrics wrapping tests (311 — Phase 4a).

Verifies that _wrap_cache_with_metrics correctly wraps cache adapters
and prevents double-wrapping.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from baldur.adapters.cache.metrics_decorator import MetricsAwareCacheAdapter
from baldur.factory import ProviderRegistry
from baldur.interfaces.cache_provider import CacheProviderInterface


class TestWrapCacheWithMetricsBehavior:
    """ProviderRegistry._wrap_cache_with_metrics behavior."""

    def test_wraps_plain_adapter_with_metrics(self):
        """Plain CacheProviderInterface is wrapped with MetricsAwareCacheAdapter."""
        plain = MagicMock(spec=CacheProviderInterface)
        result = ProviderRegistry._wrap_cache_with_metrics(plain)
        assert isinstance(result, MetricsAwareCacheAdapter)

    def test_does_not_double_wrap(self):
        """Already-wrapped adapter is returned as-is."""
        plain = MagicMock(spec=CacheProviderInterface)
        wrapped = MetricsAwareCacheAdapter(plain)
        result = ProviderRegistry._wrap_cache_with_metrics(wrapped)
        assert result is wrapped

    def test_wrapped_adapter_delegates_to_original(self):
        """Wrapped adapter's delegate is the original adapter."""
        plain = MagicMock(spec=CacheProviderInterface)
        result = ProviderRegistry._wrap_cache_with_metrics(plain)
        assert result._delegate is plain


class TestMetricsAwareCacheAdapterReconnectBehavior:
    """MetricsAwareCacheAdapter.reconnect() delegation behavior."""

    def test_reconnect_delegates_when_delegate_has_method(self):
        """reconnect() delegates to underlying adapter and returns its result."""
        delegate = MagicMock(spec=CacheProviderInterface)
        delegate.reconnect = MagicMock(return_value=True)
        adapter = MetricsAwareCacheAdapter(delegate)

        result = adapter.reconnect()

        assert result is True
        delegate.reconnect.assert_called_once()

    def test_reconnect_returns_false_when_delegate_lacks_method(self):
        """reconnect() returns False when delegate has no reconnect method."""
        delegate = MagicMock(spec=CacheProviderInterface)
        adapter = MetricsAwareCacheAdapter(delegate)

        result = adapter.reconnect()

        assert result is False

    def test_reconnect_propagates_false_from_delegate(self):
        """reconnect() propagates False return value from delegate."""
        delegate = MagicMock(spec=CacheProviderInterface)
        delegate.reconnect = MagicMock(return_value=False)
        adapter = MetricsAwareCacheAdapter(delegate)

        result = adapter.reconnect()

        assert result is False
