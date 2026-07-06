"""
DCL Singleton Pattern Sample Tests — Bulk Application (ccaeb8d)

Representative sampling from the 102 singletons that were automatically
converted to DCL pattern via apply_dcl_v2.py. One sample per module type
verifies the automated transformation applied correctly.

Modules sampled:
  - services/health_check.py         (services/ — simple)
  - services/error_budget/service.py  (services/ — configure pattern)
  - core/action_executor.py           (core/ — no reset)
  - adapters/redis/connection_factory.py (adapters/)
  - api/django/rate_limit/middleware.py  (api/ — multiple singletons)
  - metrics/prometheus.py             (metrics/ — conditional instantiation)
  - scaling/graceful_degradation.py   (scaling/ — no reset)

Verification techniques:
  - §8.10 Singleton/lifecycle — get_*() caching, reset_*() clearing
  - §8.7 Concurrency/thread safety — multi-thread DCL consistency
"""

from __future__ import annotations

import threading

import pytest

# =============================================================================
# 1. services/health_check.py — simple singleton
# =============================================================================


class TestHealthCheckServiceSingletonBehavior:
    """Verify get_health_check_service DCL singleton.

    Source: src/baldur/services/health_check.py:436-453
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.services.health_check import reset_health_check_service

        reset_health_check_service()
        yield
        reset_health_check_service()

    def test_get_health_check_service_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.services.health_check import (
            HealthCheckService,
            get_health_check_service,
        )

        first = get_health_check_service()
        second = get_health_check_service()
        assert first is second
        assert isinstance(first, HealthCheckService)

    def test_reset_health_check_service_clears_singleton(self):
        """After reset, a new instance must be created."""
        from baldur.services.health_check import (
            get_health_check_service,
            reset_health_check_service,
        )

        first = get_health_check_service()
        reset_health_check_service()
        second = get_health_check_service()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "health_check_service" in _REGISTRY

    def test_concurrent_get_returns_same_instance(self):
        """Multi-thread DCL must produce a single shared instance."""
        from baldur.services.health_check import get_health_check_service

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_health_check_service())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 2. services/error_budget/service.py — configure pattern
# =============================================================================


class TestErrorBudgetServiceSingletonBehavior:
    """Verify get_error_budget_service DCL singleton.

    Source: src/baldur/services/error_budget/service.py:263-283
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        pytest.importorskip("baldur_pro")
        import baldur_pro.services.error_budget.service as mod

        mod._service_instance = None
        yield
        mod._service_instance = None

    def test_get_error_budget_service_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur_pro.services.error_budget.service import (
            ErrorBudgetService,
            get_error_budget_service,
        )

        first = get_error_budget_service()
        second = get_error_budget_service()
        assert first is second
        assert isinstance(first, ErrorBudgetService)

    def test_module_has_threading_lock(self):
        """Module must define _service_instance_lock for DCL."""
        import baldur_pro.services.error_budget.service as mod

        assert hasattr(mod, "_service_instance_lock")
        assert isinstance(mod._service_instance_lock, type(threading.Lock()))


# =============================================================================
# 3. core/action_executor.py — no reset function
# =============================================================================


class TestActionExecutorSingletonBehavior:
    """Verify get_action_executor DCL singleton.

    Source: src/baldur/core/action_executor.py:320-330
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.core.action_executor import reset_action_executor

        reset_action_executor()
        yield
        reset_action_executor()

    def test_get_action_executor_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.core.action_executor import (
            ActionExecutor,
            get_action_executor,
        )

        first = get_action_executor()
        second = get_action_executor()
        assert first is second
        assert isinstance(first, ActionExecutor)

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "action_executor" in _REGISTRY

    def test_concurrent_get_returns_same_instance(self):
        """Multi-thread DCL must produce a single shared instance."""
        from baldur.core.action_executor import get_action_executor

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_action_executor())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 4. adapters/redis/connection_factory.py — standard singleton
# =============================================================================


class TestRedisConnectionFactorySingletonBehavior:
    """Verify get_redis_connection_factory DCL singleton.

    Source: src/baldur/adapters/redis/connection_factory.py:246-263
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.adapters.redis.connection_factory import (
            reset_redis_connection_factory,
        )

        reset_redis_connection_factory()
        yield
        reset_redis_connection_factory()

    def test_get_redis_connection_factory_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.adapters.redis.connection_factory import (
            RedisConnectionFactory,
            get_redis_connection_factory,
        )

        first = get_redis_connection_factory()
        second = get_redis_connection_factory()
        assert first is second
        assert isinstance(first, RedisConnectionFactory)

    def test_reset_redis_connection_factory_clears_singleton(self):
        """After reset, a new instance must be created."""
        from baldur.adapters.redis.connection_factory import (
            get_redis_connection_factory,
            reset_redis_connection_factory,
        )

        first = get_redis_connection_factory()
        reset_redis_connection_factory()
        second = get_redis_connection_factory()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "redis_connection_factory" in _REGISTRY


# =============================================================================
# 5. api/django/rate_limit/middleware.py — multiple singletons
# =============================================================================


class TestRateLimitMiddlewareSingletonBehavior:
    """Verify rate limit middleware singletons (migrated to make_singleton_factory).

    Source: src/baldur/api/django/rate_limit/middleware.py
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.api.django.rate_limit.middleware import (
            reset_local_limiter,
            reset_redis_health_checker,
        )

        reset_redis_health_checker(cleanup=False)
        reset_local_limiter(cleanup=False)
        yield
        reset_redis_health_checker(cleanup=False)
        reset_local_limiter(cleanup=False)

    def test_get_redis_health_checker_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.api.django.rate_limit.middleware import (
            RedisHealthChecker,
            get_redis_health_checker,
        )

        first = get_redis_health_checker()
        second = get_redis_health_checker()
        assert first is second
        assert isinstance(first, RedisHealthChecker)

    def test_get_local_limiter_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.api.django.rate_limit.middleware import get_local_limiter
        from baldur.services.rate_limit import SlidingWindowLimiter

        first = get_local_limiter()
        second = get_local_limiter()
        assert first is second
        assert isinstance(first, SlidingWindowLimiter)

    def test_singletons_use_make_singleton_factory(self):
        """Singletons are registered in the make_singleton_factory registry."""
        from baldur.utils.singleton import _REGISTRY

        assert "redis_health_checker" in _REGISTRY
        assert "local_limiter" in _REGISTRY
        assert "shadow_audit" in _REGISTRY


# =============================================================================
# 6. metrics/prometheus.py — conditional instantiation
# =============================================================================


class TestMetricsSingletonBehavior:
    """Verify get_metrics DCL singleton.

    Source: src/baldur/metrics/prometheus.py:374-406
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.metrics.prometheus import reset_metrics

        reset_metrics()
        yield
        reset_metrics()

    def test_get_metrics_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.metrics.prometheus import get_metrics

        first = get_metrics()
        second = get_metrics()
        assert first is second

    def test_reset_metrics_clears_singleton(self):
        """After reset, a new instance must be created."""
        from baldur.metrics.prometheus import get_metrics, reset_metrics

        first = get_metrics()
        reset_metrics()
        second = get_metrics()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "metrics" in _REGISTRY


# =============================================================================
# 7. scaling/graceful_degradation.py — no reset function
# =============================================================================


class TestGracefulDegradationSingletonBehavior:
    """Verify get_graceful_degradation DCL singleton.

    Source: src/baldur/scaling/graceful_degradation.py:242-253
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from baldur.scaling.graceful_degradation import reset_graceful_degradation

        reset_graceful_degradation()
        yield
        reset_graceful_degradation()

    def test_get_graceful_degradation_returns_same_instance(self):
        """Repeated calls must return the same cached instance."""
        from baldur.scaling.graceful_degradation import (
            GracefulDegradation,
            get_graceful_degradation,
        )

        first = get_graceful_degradation()
        second = get_graceful_degradation()
        assert first is second
        assert isinstance(first, GracefulDegradation)

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "graceful_degradation" in _REGISTRY
