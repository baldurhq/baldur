"""
Unit tests for service-level cache interface unification (367).

Verifies that services correctly delegate to CacheProviderInterface
or domain Store ABCs after django.core.cache removal.

Verification techniques:
- Dependency interaction: verify delegation to store/cache
- Graceful degradation: behavior when store unavailable
- Serialization roundtrip: ConfigHistoryService save→get
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# ConfigHistoryService → ConfigHistoryStore
# =============================================================================


class TestConfigHistoryServiceBehavior:
    """Behavior tests for ConfigHistoryService store delegation."""

    @pytest.fixture
    def mock_store(self):
        from baldur.interfaces.config_history_store import ConfigHistoryStore

        store = MagicMock(spec=ConfigHistoryStore)
        store.next_version.return_value = 1
        return store

    @pytest.fixture
    def service(self, mock_store):
        from baldur.services.config_history.service import ConfigHistoryService

        return ConfigHistoryService(store=mock_store)

    def test_save_version_delegates_to_store(self, service, mock_store):
        """save_version calls store.next_version and store.save_version."""
        result = service.save_version(
            config_type="circuit_breaker",
            values={"threshold": 5},
            changed_by="admin",
            reason="test",
        )

        assert result is not None
        assert result.version == 1
        mock_store.next_version.assert_called_once_with("circuit_breaker")
        mock_store.save_version.assert_called_once()
        call_args = mock_store.save_version.call_args
        assert call_args[0][0] == "circuit_breaker"  # config_type
        assert isinstance(call_args[0][1], dict)  # version_data

    def test_get_current_version_delegates_to_store(self, service, mock_store):
        """get_current_version calls store.get_current."""
        mock_store.get_current.return_value = {
            "version": 1,
            "timestamp": 1234567890.0,
            "config_type": "circuit_breaker",
            "values": {"threshold": 5},
            "changed_by": "admin",
            "reason": "",
            "hash": "abc123",
        }

        result = service.get_current_version("circuit_breaker")

        mock_store.get_current.assert_called_once_with("circuit_breaker")
        assert result is not None
        assert result.version == 1

    def test_get_history_delegates_to_store(self, service, mock_store):
        """get_history calls store.get_history."""
        mock_store.get_history.return_value = [
            {
                "version": 2,
                "timestamp": 1234567891.0,
                "config_type": "circuit_breaker",
                "values": {"t": 5},
                "changed_by": "admin",
                "reason": "",
                "hash": "def456",
            }
        ]

        result = service.get_history("circuit_breaker", limit=10)

        mock_store.get_history.assert_called_once()
        assert len(result) == 1
        assert result[0].version == 2

    def test_get_version_count_delegates_to_store(self, service, mock_store):
        """get_version_count calls store.get_version_count."""
        mock_store.get_version_count.return_value = 5

        assert service.get_version_count("cb") == 5
        mock_store.get_version_count.assert_called_once_with("cb")

    def test_clear_history_delegates_to_store(self, service, mock_store):
        """clear_history calls store.clear."""
        assert service.clear_history("cb") is True
        mock_store.clear.assert_called_once_with("cb")

    def test_save_version_returns_none_for_invalid_config_type(self, service):
        """save_version returns None for unsupported config type."""
        result = service.save_version(
            config_type="invalid_type",
            values={"x": 1},
            changed_by="admin",
        )
        assert result is None

    def test_store_none_graceful_degradation(self):
        """Service degrades gracefully when store is None."""
        from baldur.services.config_history.service import ConfigHistoryService

        svc = ConfigHistoryService(store=None)
        # Patch ProviderRegistry to raise so store stays None
        with patch(
            "baldur.factory.ProviderRegistry",
        ) as mock_reg:
            mock_reg.config_history_store.get.side_effect = Exception("unavailable")
            assert svc.save_version("circuit_breaker", {"v": 1}, "admin") is None
            assert svc.get_current_version("circuit_breaker") is None
            assert svc.get_history("circuit_breaker", 10) == []
            assert svc.get_version_count("circuit_breaker") == 0


# =============================================================================
# IdempotencyService → CacheProviderInterface
# =============================================================================


class TestIdempotencyServiceBehavior:
    """Behavior tests for IdempotencyService cache provider delegation."""

    @pytest.fixture
    def mock_cache(self):
        cache = MagicMock()
        cache.get.return_value = None
        cache.set.return_value = True
        cache.delete.return_value = True
        cache.mget.return_value = {}
        cache.mset.return_value = True
        return cache

    @pytest.fixture
    def service(self, mock_cache):
        from baldur.services.idempotency.service import IdempotencyService

        svc = IdempotencyService()
        svc._cache = mock_cache
        return svc

    def test_check_calls_cache_get(self, service, mock_cache):
        """check() calls cache.get with the key."""
        from baldur.services.idempotency.models import IdempotencyKey

        key = IdempotencyKey.for_event("evt-123")
        mock_cache.get.return_value = None

        result = service.check(key)

        mock_cache.get.assert_called_with(key.cache_key)
        assert result.is_duplicate is False

    def test_check_cache_hit_returns_duplicate(self, service, mock_cache):
        """check() returns duplicate when cache has value."""
        from baldur.services.idempotency.models import IdempotencyKey

        key = IdempotencyKey.for_event("evt-123")
        mock_cache.get.return_value = 42

        result = service.check(key)
        assert result.is_duplicate is True

    def test_mark_as_processed_calls_cache_set_with_timedelta(
        self, service, mock_cache
    ):
        """mark_as_processed passes timedelta to cache.set."""
        from baldur.services.idempotency.models import IdempotencyKey

        key = IdempotencyKey.for_event("evt-123")
        service.mark_as_processed(key, record_id=42, ttl=300)

        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        assert call_args[0][0] == key.cache_key
        assert call_args[0][1] == 42
        assert call_args[1]["ttl"] == timedelta(seconds=300)

    def test_batch_check_calls_mget(self, service, mock_cache):
        """batch_check uses cache.mget."""
        from baldur.services.idempotency.models import IdempotencyKey

        keys = [IdempotencyKey.for_event(f"evt-{i}") for i in range(3)]
        mock_cache.mget.return_value = {keys[1].cache_key: True}

        results = service.batch_check(keys)

        mock_cache.mget.assert_called_once()
        assert results[0].is_duplicate is False
        assert results[1].is_duplicate is True
        assert results[2].is_duplicate is False

    def test_batch_mark_calls_mset_with_timedelta(self, service, mock_cache):
        """batch_mark_as_processed passes timedelta to cache.mset."""
        from baldur.services.idempotency.models import IdempotencyKey

        keys = [IdempotencyKey.for_event(f"evt-{i}") for i in range(2)]
        service.batch_mark_as_processed(keys, ttl=600)

        mock_cache.mset.assert_called_once()
        call_args = mock_cache.mset.call_args
        assert call_args[1]["ttl"] == timedelta(seconds=600)

    def test_clear_calls_cache_delete(self, service, mock_cache):
        """clear() calls cache.delete."""
        from baldur.services.idempotency.models import IdempotencyKey

        key = IdempotencyKey.for_event("evt-123")
        service.clear(key)

        mock_cache.delete.assert_called_once_with(key.cache_key)

    def test_inmemory_fallback_when_no_adapter_registered(self):
        """Falls back to module-level InMemoryCacheAdapter when no provider
        is registered (532 D1 — replaces the historical _NoopCache fallback)."""
        from baldur.adapters.cache.memory_adapter import InMemoryCacheAdapter
        from baldur.core.exceptions import AdapterNotFoundError
        from baldur.services.idempotency._cache_resolver import (
            _SERVICE_FALLBACK_CACHE,
        )
        from baldur.services.idempotency.service import IdempotencyService

        svc = IdempotencyService()
        with patch(
            "baldur.factory.registry.ProviderRegistry.get_cache",
            side_effect=AdapterNotFoundError(adapter_type="cache"),
        ):
            cache = svc._get_cache()

        assert isinstance(cache, InMemoryCacheAdapter)
        assert cache is _SERVICE_FALLBACK_CACHE
        # Fallback cache is empty by default — reads return None / {}.
        assert cache.get("any") is None
        assert cache.mget(["a", "b"]) == {}


# =============================================================================
# DailyReportCollector → CacheProviderInterface
# =============================================================================


class TestDailyReportCollectorBehavior:
    """Behavior tests for DailyReportCollector cache delegation."""

    def test_add_result_uses_cache_provider(self):
        """add_result calls ProviderRegistry.get_cache() and push_limit()."""
        from baldur.services.daily_report.aggregator import DailyReportCollector

        mock_cache = MagicMock()
        mock_cache.push_limit.return_value = 1

        collector = DailyReportCollector()

        with patch("baldur.factory.ProviderRegistry") as mock_reg:
            mock_reg.get_cache.return_value = mock_cache
            collector.add_result("my_task", {"status": "ok"})

        mock_reg.get_cache.assert_called_once()
        mock_cache.push_limit.assert_called_once()
        # Verify push_limit uses ttl= with timedelta
        call_kwargs = mock_cache.push_limit.call_args[1]
        assert "ttl" in call_kwargs
        assert isinstance(call_kwargs["ttl"], timedelta)

    def test_add_result_fail_open_on_error(self):
        """add_result silently drops entry when cache fails (fail-open)."""
        from baldur.services.daily_report.aggregator import DailyReportCollector

        collector = DailyReportCollector()

        with patch("baldur.factory.ProviderRegistry") as mock_reg:
            mock_reg.get_cache.side_effect = Exception("cache error")
            # Should not raise — fail-open behavior
            collector.add_result("my_task", {"status": "ok"})


# =============================================================================
# L2RedisCache → CacheProviderInterface
# =============================================================================


class TestL2RedisCacheBehavior:
    """Behavior tests for L2RedisCache provider delegation."""

    def test_get_redis_uses_provider_registry(self):
        """_get_redis() calls ProviderRegistry.get_cache()."""
        from baldur.services.precomputed_cache.l2_cache import L2RedisCache

        mock_cache = MagicMock()
        cache_obj = L2RedisCache()

        with patch("baldur.factory.ProviderRegistry") as mock_reg:
            mock_reg.get_cache.return_value = mock_cache
            result = cache_obj._get_redis()

        assert result is mock_cache
        assert cache_obj._initialized is True

    def test_set_uses_ttl_kwarg(self):
        """set() passes ttl= (not timeout=) to cache provider."""
        from baldur.services.precomputed_cache.l2_cache import L2RedisCache

        mock_cache = MagicMock()
        mock_cache.set.return_value = True

        cache_obj = L2RedisCache()
        cache_obj._redis = mock_cache

        cache_obj.set("key", "value", ttl=120.0)

        mock_cache.set.assert_called_once_with(
            "key", "value", ttl=timedelta(seconds=120)
        )


# =============================================================================
# CircuitBreakerService._get_cached_data → ProviderRegistry.get_cache
# =============================================================================


class TestCircuitBreakerCacheBehavior:
    """Behavior test for circuit breaker cache lookup delegation."""

    def test_get_cached_data_uses_provider_registry(self):
        """_get_cached_data calls ProviderRegistry.get_cache().get()."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        mock_cache = MagicMock()
        mock_cache.get.return_value = {"some": "data"}

        svc = CircuitBreakerService.__new__(CircuitBreakerService)

        with patch("baldur.factory.ProviderRegistry") as mock_reg:
            mock_reg.get_cache.return_value = mock_cache
            result = svc._get_cached_data("test-key")

        assert result == {"some": "data"}
        mock_cache.get.assert_called_once_with("test-key")

    def test_get_cached_data_returns_none_on_exception(self):
        """_get_cached_data returns None on any exception."""
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        svc = CircuitBreakerService.__new__(CircuitBreakerService)

        with patch("baldur.factory.ProviderRegistry") as mock_reg:
            mock_reg.get_cache.side_effect = Exception("unavailable")
            result = svc._get_cached_data("test-key")

        assert result is None
