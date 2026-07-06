"""
Stage 24: Partial Network Partition Tests

Scenarios:
1. DB alive, Redis dead → cache bypass to DB
2. Redis alive, DB dead → read from cache
3. External API dead → use cached response
4. All connections healthy → normal operation
5. All connections dead → graceful failure

"""

from __future__ import annotations


class TestPartitionState:
    """Tests for PartitionState detection."""

    def test_detect_partial_partition_cache_down(self):
        """Cache only down is partial partition."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=True,
            cache_available=False,
            external_apis={"payment_gateway": True},
        )

        assert state.is_partial_partition is True
        assert state.is_full_partition is False
        assert state.is_healthy is False

    def test_detect_partial_partition_db_down(self):
        """DB only down is partial partition."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=False, cache_available=True, external_apis={}
        )

        assert state.is_partial_partition is True

    def test_detect_partial_partition_external_api_down(self):
        """External API only down is partial partition."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=True,
            cache_available=True,
            external_apis={"payment_api": False, "notification_api": True},
        )

        assert state.is_partial_partition is True

    def test_all_healthy_not_partition(self):
        """All healthy is not a partition."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=True, cache_available=True, external_apis={"api1": True}
        )

        assert state.is_partial_partition is False
        assert state.is_healthy is True

    def test_all_down_full_partition(self):
        """All down is full partition, not partial."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=False, cache_available=False, external_apis={"api1": False}
        )

        assert state.is_partial_partition is False
        assert state.is_full_partition is True

    def test_empty_external_apis(self):
        """No external APIs registered - just db and cache."""
        from baldur.core.connection_health import PartitionState

        state = PartitionState(
            db_available=True, cache_available=True, external_apis={}
        )

        assert state.is_healthy is True
        assert state.is_partial_partition is False


class TestConnectionHealthMonitor:
    """Tests for DefaultConnectionHealthMonitor."""

    def test_register_and_check_healthy(self):
        """Healthy connection check."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()

        # Always successful health check
        monitor.register_health_check(ConnectionType.DATABASE, "primary", lambda: True)

        health = monitor.check_health(ConnectionType.DATABASE, "primary")

        assert health.status == ConnectionStatus.HEALTHY
        assert health.consecutive_failures == 0
        assert health.last_success is not None
        assert health.latency_ms is not None

    def test_unregistered_connection_unknown(self):
        """Unregistered connection returns UNKNOWN status."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()
        health = monitor.check_health(ConnectionType.CACHE, "nonexistent")

        assert health.status == ConnectionStatus.UNKNOWN

    def test_single_failure_degraded(self):
        """Single failure results in DEGRADED status."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()
        monitor.register_health_check(ConnectionType.CACHE, "redis", lambda: False)

        health = monitor.check_health(ConnectionType.CACHE, "redis")

        assert health.status == ConnectionStatus.DEGRADED
        assert health.consecutive_failures == 1

    def test_consecutive_failures_unhealthy(self):
        """3 consecutive failures result in UNHEALTHY status."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor(failure_threshold=3)
        monitor.register_health_check(ConnectionType.CACHE, "redis", lambda: False)

        # First two failures → DEGRADED
        monitor.check_health(ConnectionType.CACHE, "redis")
        health = monitor.check_health(ConnectionType.CACHE, "redis")
        assert health.status == ConnectionStatus.DEGRADED

        # Third failure → UNHEALTHY
        health = monitor.check_health(ConnectionType.CACHE, "redis")
        assert health.status == ConnectionStatus.UNHEALTHY
        assert health.consecutive_failures == 3

    def test_recovery_after_failure(self):
        """Successful check resets failure count."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        check_result = [False]  # Mutable for closure

        monitor = DefaultConnectionHealthMonitor()
        monitor.register_health_check(
            ConnectionType.DATABASE, "primary", lambda: check_result[0]
        )

        # Fail twice
        monitor.check_health(ConnectionType.DATABASE, "primary")
        health = monitor.check_health(ConnectionType.DATABASE, "primary")
        assert health.consecutive_failures == 2

        # Succeed
        check_result[0] = True
        health = monitor.check_health(ConnectionType.DATABASE, "primary")
        assert health.status == ConnectionStatus.HEALTHY
        assert health.consecutive_failures == 0

    def test_exception_treated_as_failure(self):
        """Exception in health check is treated as failure."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()
        monitor.register_health_check(
            ConnectionType.CACHE,
            "redis",
            lambda: (_ for _ in ()).throw(ConnectionError("Connection refused")),
        )

        health = monitor.check_health(ConnectionType.CACHE, "redis")

        assert health.status == ConnectionStatus.DEGRADED
        assert "Connection refused" in health.error_message

    def test_get_partition_state(self):
        """get_partition_state aggregates all connections."""
        from baldur.core.connection_health import (
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()

        # DB healthy
        monitor.register_health_check(ConnectionType.DATABASE, "primary", lambda: True)
        monitor.check_health(ConnectionType.DATABASE, "primary")

        # Cache unhealthy
        monitor.register_health_check(ConnectionType.CACHE, "redis", lambda: False)
        for _ in range(3):
            monitor.check_health(ConnectionType.CACHE, "redis")

        # External API healthy
        monitor.register_health_check(
            ConnectionType.EXTERNAL_API, "payment", lambda: True
        )
        monitor.check_health(ConnectionType.EXTERNAL_API, "payment")

        state = monitor.get_partition_state()

        assert state.db_available is True
        assert state.cache_available is False
        assert state.external_apis["payment"] is True
        assert state.is_partial_partition is True

    def test_unregister_health_check(self):
        """Can unregister a health check."""
        from baldur.core.connection_health import (
            ConnectionStatus,
            ConnectionType,
            DefaultConnectionHealthMonitor,
        )

        monitor = DefaultConnectionHealthMonitor()
        monitor.register_health_check(ConnectionType.DATABASE, "primary", lambda: True)

        result = monitor.unregister_health_check(ConnectionType.DATABASE, "primary")
        assert result is True

        # Now should return UNKNOWN
        health = monitor.check_health(ConnectionType.DATABASE, "primary")
        assert health.status == ConnectionStatus.UNKNOWN


class TestFallbackStrategy:
    """Tests for fallback strategies."""

    def test_primary_success_no_fallback(self):
        """Primary success returns value without fallback."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import PartitionAwareFallback

        state = PartitionState(db_available=True, cache_available=True)
        strategy = PartitionAwareFallback(state)

        result = strategy.execute(
            primary_fn=lambda: "primary_value", default_value="default"
        )

        assert result.value == "primary_value"
        assert result.used_fallback is False
        assert result.success is True

    def test_explicit_fallback_used(self):
        """Explicit fallback function is tried first."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import (
            FallbackMode,
            PartitionAwareFallback,
        )

        state = PartitionState(db_available=True, cache_available=True)
        strategy = PartitionAwareFallback(state)

        def failing_primary():
            raise RuntimeError("Primary failed")

        result = strategy.execute(
            primary_fn=failing_primary,
            fallback_fn=lambda: "fallback_value",
            default_value="default",
        )

        assert result.value == "fallback_value"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.RETRY_ALTERNATIVE

    def test_cache_down_db_fallback(self):
        """Cache down triggers DB fallback."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import (
            FallbackMode,
            PartitionAwareFallback,
        )

        state = PartitionState(db_available=True, cache_available=False)

        strategy = PartitionAwareFallback(
            state, db_fallback=lambda: {"from": "database"}
        )

        def failing_primary():
            raise ConnectionError("Redis down")

        result = strategy.execute(primary_fn=failing_primary, default_value=None)

        assert result.value == {"from": "database"}
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.DEGRADE_GRACEFULLY

    def test_db_down_cache_fallback(self):
        """DB down triggers cache fallback."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import (
            FallbackMode,
            PartitionAwareFallback,
        )

        state = PartitionState(db_available=False, cache_available=True)

        strategy = PartitionAwareFallback(
            state, cache_fallback=lambda: {"from": "cache"}
        )

        def failing_primary():
            raise ConnectionError("DB down")

        result = strategy.execute(primary_fn=failing_primary, default_value=None)

        assert result.value == {"from": "cache"}
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.USE_CACHE

    def test_all_fallbacks_fail_use_default(self):
        """All fallbacks fail uses default value."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import (
            FallbackMode,
            PartitionAwareFallback,
        )

        state = PartitionState(db_available=False, cache_available=False)
        strategy = PartitionAwareFallback(state)

        def failing_primary():
            raise RuntimeError("All down")

        result = strategy.execute(
            primary_fn=failing_primary, default_value="safe_default"
        )

        assert result.value == "safe_default"
        assert result.fallback_mode == FallbackMode.USE_DEFAULT

    def test_complete_failure(self):
        """No fallbacks and no default results in FAIL_FAST."""
        from baldur.core.connection_health import PartitionState
        from baldur.core.fallback_strategy import (
            FallbackMode,
            PartitionAwareFallback,
        )

        state = PartitionState(db_available=False, cache_available=False)
        strategy = PartitionAwareFallback(state)

        def failing_primary():
            raise RuntimeError("Complete failure")

        result = strategy.execute(primary_fn=failing_primary, default_value=None)

        assert result.value is None
        assert result.fallback_mode == FallbackMode.FAIL_FAST
        assert result.original_error is not None


class TestSimpleFallback:
    """Tests for SimpleFallback strategy."""

    def test_simple_fallback_primary_success(self):
        """Primary success."""
        from baldur.core.fallback_strategy import SimpleFallback

        strategy = SimpleFallback()
        result = strategy.execute(
            primary_fn=lambda: "primary",
            fallback_fn=lambda: "fallback",
        )

        assert result.value == "primary"
        assert result.used_fallback is False

    def test_simple_fallback_uses_fallback(self):
        """Fallback used on primary failure."""
        from baldur.core.fallback_strategy import FallbackMode, SimpleFallback

        strategy = SimpleFallback()
        result = strategy.execute(
            primary_fn=lambda: (_ for _ in ()).throw(Exception("fail")),
            fallback_fn=lambda: "fallback",
        )

        assert result.value == "fallback"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.RETRY_ALTERNATIVE


class TestCacheFirstFallback:
    """Tests for CacheFirstFallback strategy."""

    def test_cache_hit(self):
        """Cache hit returns cached value."""
        from baldur.core.fallback_strategy import CacheFirstFallback

        strategy = CacheFirstFallback(
            cache_fn=lambda: "cached_value",
            db_fn=lambda: "db_value",
        )

        result = strategy.execute()

        assert result.value == "cached_value"
        assert result.used_fallback is False

    def test_cache_miss_db_fallback(self):
        """Cache miss falls back to DB."""
        from baldur.core.fallback_strategy import CacheFirstFallback, FallbackMode

        cache_updated = []

        strategy = CacheFirstFallback(
            cache_fn=lambda: None,  # Cache miss
            db_fn=lambda: "db_value",
            update_cache_fn=lambda v: cache_updated.append(v),
        )

        result = strategy.execute()

        assert result.value == "db_value"
        assert result.used_fallback is True
        assert result.fallback_mode == FallbackMode.DEGRADE_GRACEFULLY
        assert cache_updated == ["db_value"]

    def test_cache_error_db_fallback(self):
        """Cache error falls back to DB."""
        from baldur.core.fallback_strategy import CacheFirstFallback

        def failing_cache():
            raise ConnectionError("Redis down")

        strategy = CacheFirstFallback(
            cache_fn=failing_cache,
            db_fn=lambda: "db_value",
        )

        result = strategy.execute()

        assert result.value == "db_value"
        assert result.used_fallback is True
