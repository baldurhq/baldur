"""
Circuit Breaker Stale Cache integration tests.

Test Coverage:
- CanaryWithStaleCacheService: Canary + Stale Cache combination
- build_stale_cache_key: cache key generation helper (#234)
- record_success auto cache store (#234)
- StaleCacheStore O(1) oldest-entry eviction (doc 594 D9 / G8)
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from baldur.services.circuit_breaker.models import (
    CanaryRecoveryStageConfig,
    RecoveryStrategy,
)
from baldur.services.circuit_breaker.stale_cache_integration import (
    CanaryWithStaleCacheService,
    build_stale_cache_key,
)

# =============================================================================
# 4.2 CanaryWithStaleCacheService Tests
# =============================================================================


class TestStaleCacheStore:
    """StaleCacheStore tests."""

    def test_set_and_get(self):
        """Cache store and lookup."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            StaleCacheStore,
        )

        store = StaleCacheStore()
        store.set("key1", {"data": "value"}, ttl_seconds=300)

        entry = store.get("key1", max_stale_age=300)

        assert entry is not None
        assert entry.value == {"data": "value"}

    def test_get_nonexistent_returns_none(self):
        """Lookup of a missing key returns None."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            StaleCacheStore,
        )

        store = StaleCacheStore()

        entry = store.get("nonexistent")

        assert entry is None

    def test_stale_detection(self):
        """Stale state detection."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            StaleCacheEntry,
        )

        entry = StaleCacheEntry(
            key="key1",
            value="data",
            ttl_seconds=1,
        )
        # Simulate TTL exceedance
        entry.cached_at = datetime.now(UTC) - timedelta(seconds=2)

        assert entry.is_stale() is True

    def test_cache_stats(self):
        """Cache statistics."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            StaleCacheStore,
        )

        store = StaleCacheStore()
        store.set("key1", "value1")
        store.get("key1")
        store.get("key2")  # miss

        stats = store.get_stats()

        assert stats["sets"] == 1
        assert stats["hits"] >= 1 or stats["stale_hits"] >= 1
        assert stats["misses"] >= 1


class TestStaleCacheStoreEvictionBehavior:
    """O(1) oldest-entry eviction semantics (doc 594 D9 / G8).

    Insertion/update order equals age order because set() always stores a
    fresh StaleCacheEntry (overwrites move to the end), so FIFO-head
    eviction IS oldest-entry eviction.
    """

    @staticmethod
    def _make_store(max_entries: int):
        from baldur.services.circuit_breaker.stale_cache_integration import (
            StaleCacheStore,
        )

        return StaleCacheStore(max_entries=max_entries)

    def test_new_key_at_capacity_evicts_oldest(self):
        """Inserting a NEW key at capacity evicts the oldest entry only."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k3", "v3")

        assert store.get("k0") is None  # oldest evicted
        assert store.get("k1") is not None
        assert store.get("k2") is not None
        assert store.get("k3") is not None
        assert store.get_stats()["size"] == 3

    def test_overwrite_at_capacity_evicts_nothing(self):
        """Overwriting an existing key at capacity replaces in place -
        no unrelated entry is evicted (fixes the pre-594 quirk)."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k0", "v0-new")

        assert store.get_stats()["size"] == 3
        entry = store.get("k0")
        assert entry is not None
        assert entry.value == "v0-new"
        assert store.get("k1") is not None
        assert store.get("k2") is not None

    def test_overwrite_refreshes_age_order(self):
        """An overwrite gets a fresh cached_at and moves to the end, so the
        next at-capacity insert evicts the actually-oldest entry."""
        store = self._make_store(max_entries=3)
        store.set("k0", "v0")
        store.set("k1", "v1")
        store.set("k2", "v2")

        store.set("k0", "v0-new")  # k0 becomes newest; k1 is now oldest
        store.set("k3", "v3")  # at capacity -> evicts k1

        assert store.get("k1") is None
        assert store.get("k0") is not None
        assert store.get("k2") is not None
        assert store.get("k3") is not None

    def test_repeated_inserts_keep_size_at_capacity(self):
        """Sustained unique-key churn never grows the store past capacity."""
        store = self._make_store(max_entries=2)

        for i in range(10):
            store.set(f"k{i}", f"v{i}")
            assert store.get_stats()["size"] <= 2

        # The two newest survive
        assert store.get("k8") is not None
        assert store.get("k9") is not None


class TestCanaryWithStaleCacheService:
    """CanaryWithStaleCacheService tests."""

    def setup_method(self):
        """Reset singletons before each test."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_canary_stale_cache_service()
        reset_canary_recovery_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_canary_stale_cache_service()
        reset_canary_recovery_manager()

    def test_closed_state_allows_backend(self):
        """Backend allowed in CLOSED state."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:123",
            cb_state="closed",
        )

        assert decision.allow_backend is True
        assert decision.use_stale is False

    def test_open_state_uses_stale_cache(self):
        """Stale Cache used in OPEN state."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        service.update_cache("payment:123", {"amount": 100}, "payment-api")

        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:123",
            cb_state="open",
        )

        assert decision.allow_backend is False
        assert decision.use_stale is True
        assert decision.stale_data == {"amount": 100}

    def test_open_state_rejects_without_cache(self):
        """OPEN state without a cache entry rejects the request."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:unknown",
            cb_state="open",
        )

        assert decision.allow_backend is False
        assert decision.reject is True

    def test_half_open_canary_request(self):
        """Canary request in HALF_OPEN state."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        manager = get_canary_recovery_manager()

        # Start canary recovery (configured at 100% traffic)
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=100.0,
                    duration_seconds=5,
                    required_success_rate=90.0,
                ),
            ],
        )
        manager.start_canary_recovery("payment-api", strategy)

        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:123",
            cb_state="half_open",
        )

        # 100% traffic -> always canary
        assert decision.allow_backend is True
        assert decision.is_canary_request is True

    def test_half_open_non_canary_uses_stale(self):
        """Non-canary requests in HALF_OPEN use the Stale Cache."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        manager = get_canary_recovery_manager()

        # Populate the cache
        service.update_cache("payment:123", {"amount": 100}, "payment-api")

        # 0% traffic canary (every request is non-canary)
        strategy = RecoveryStrategy(
            type="canary",
            canary_stages=[
                CanaryRecoveryStageConfig(
                    traffic_percent=0.0, duration_seconds=5, required_success_rate=90.0
                ),
            ],
        )
        manager.start_canary_recovery("payment-api", strategy)

        decision = service.should_allow_with_fallback(
            service_id="payment-api",
            cache_key="payment:123",
            cb_state="half_open",
        )

        # 0% traffic -> every request uses the stale cache
        assert decision.allow_backend is False
        assert decision.use_stale is True
        assert decision.stale_data == {"amount": 100}

    def test_record_success_updates_canary(self):
        """Success records propagate to the canary manager."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        manager = get_canary_recovery_manager()

        manager.start_canary_recovery("payment-api")
        service.record_success("payment-api")

        state = manager.get_recovery_state("payment-api")
        assert state.metrics.success_count == 1

    def test_get_stats(self):
        """Combined statistics lookup."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        stats = service.get_stats()

        assert "canary_allowed" in stats
        assert "stale_served" in stats
        assert "cache_stats" in stats


# =============================================================================
# Behavior — build_stale_cache_key (#234)
# =============================================================================


class TestBuildStaleCacheKeyBehavior:
    """build_stale_cache_key() behavior verification (#234)."""

    def test_static_method_format(self):
        """The static method returns a 'domain:identifier' formatted key."""
        result = CanaryWithStaleCacheService.build_stale_cache_key(
            "payment",
            "user123",
        )
        assert result == "payment:user123"

    def test_module_level_function_delegates(self):
        """The module-level build_stale_cache_key() matches the static method."""
        static_result = CanaryWithStaleCacheService.build_stale_cache_key(
            "product",
            "order456",
        )
        module_result = build_stale_cache_key("product", "order456")
        assert module_result == static_result

    def test_various_domains(self):
        """Various domain values are correctly included in the key."""
        assert build_stale_cache_key("payment", "123") == "payment:123"
        assert build_stale_cache_key("product", "abc") == "product:abc"
        assert build_stale_cache_key("user", "xyz") == "user:xyz"

    def test_empty_strings(self):
        """Empty strings still preserve the format."""
        result = build_stale_cache_key("", "")
        assert result == ":"

    def test_special_characters_in_identifier(self):
        """Special characters in the identifier are preserved as-is."""
        result = build_stale_cache_key("payment", "user-123_v2")
        assert result == "payment:user-123_v2"

    def test_consistent_key_for_same_input(self):
        """The same input always produces the same key (deterministic)."""
        key1 = build_stale_cache_key("service", "id")
        key2 = build_stale_cache_key("service", "id")
        assert key1 == key2


# =============================================================================
# Behavior — record_success auto cache store (#234)
# =============================================================================


class TestRecordSuccessAutoCacheBehavior:
    """record_success() auto cache store behavior verification (#234)."""

    def setup_method(self):
        """Reset singletons before each test."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_canary_stale_cache_service()
        reset_canary_recovery_manager()

    def teardown_method(self):
        """Clean up after each test."""
        from baldur.services.circuit_breaker.canary_recovery import (
            reset_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            reset_canary_stale_cache_service,
        )

        reset_canary_stale_cache_service()
        reset_canary_recovery_manager()

    def test_record_success_without_cache_params(self):
        """Without cache_key/response_data the old behavior holds (no cache store)."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        service.record_success("payment-api")

        # Nothing was stored in the cache
        assert service._cache.get("payment:123") is None

    def test_record_success_with_cache_params_stores_data(self):
        """Passing cache_key + response_data auto-stores into the cache."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        cache_key = build_stale_cache_key("payment", "123")
        response_data = {"amount": 500, "currency": "KRW"}

        service.record_success(
            "payment-api",
            cache_key=cache_key,
            response_data=response_data,
        )

        # Verify the value was stored
        entry = service._cache.get(
            cache_key,
            max_stale_age=service._config.stale_cache_max_age_seconds,
        )
        assert entry is not None
        assert entry.value == response_data

    def test_record_success_cache_key_only_no_store(self):
        """cache_key alone with response_data=None stores nothing."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        cache_key = build_stale_cache_key("payment", "456")

        service.record_success("payment-api", cache_key=cache_key)

        entry = service._cache.get(cache_key)
        assert entry is None

    def test_record_success_response_data_only_no_store(self):
        """response_data alone with cache_key=None stores nothing."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        service.record_success(
            "payment-api",
            response_data={"amount": 100},
        )

        # No cache_key -> cannot store; completes without error
        assert service._cache.get_stats()["sets"] == 0

    def test_record_success_auto_cache_still_records_metrics(self):
        """Success metrics are recorded normally alongside the auto store."""
        from baldur.services.circuit_breaker.canary_recovery import (
            get_canary_recovery_manager,
        )
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()
        manager = get_canary_recovery_manager()
        manager.start_canary_recovery("payment-api")

        service.record_success(
            "payment-api",
            cache_key=build_stale_cache_key("payment", "789"),
            response_data={"amount": 200},
        )

        # Verify the success metric
        with service._stats_lock:
            assert service._stats["backend_success"] >= 1

        # Verify propagation to the canary manager
        state = manager.get_recovery_state("payment-api")
        assert state.metrics.success_count == 1

    def test_record_success_cache_failure_suppressed(self):
        """An update_cache() failure is suppressed - record_success() succeeds."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        with patch.object(
            service,
            "update_cache",
            side_effect=RuntimeError("cache write failed"),
        ):
            # Must not raise
            service.record_success(
                "payment-api",
                cache_key="payment:error",
                response_data={"data": "test"},
            )

        # The success metric is still recorded
        with service._stats_lock:
            assert service._stats["backend_success"] >= 1

    def test_record_success_cache_failure_logs_warning(self):
        """An update_cache() failure triggers logger.warning."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        with (
            patch.object(
                service,
                "update_cache",
                side_effect=RuntimeError("disk full"),
            ),
            patch(
                "baldur.services.circuit_breaker.stale_cache_integration.logger",
            ) as mock_logger,
        ):
            service.record_success(
                "payment-api",
                cache_key="payment:warn",
                response_data={"data": "test"},
            )
            mock_logger.warning.assert_called_once()
            assert mock_logger.warning.call_args[0][0] == "auto.cache_update_failed"

    def test_backward_compatible_signature(self):
        """Backward compatible with the legacy record_success(service_id) signature."""
        from baldur.services.circuit_breaker.stale_cache_integration import (
            get_canary_stale_cache_service,
        )

        service = get_canary_stale_cache_service()

        # Legacy style - only service_id as a positional argument
        service.record_success("payment-api")

        with service._stats_lock:
            assert service._stats["backend_success"] >= 1
