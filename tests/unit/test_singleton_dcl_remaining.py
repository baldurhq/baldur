"""
DCL Singleton Pattern Tests for 10 Remaining Modules

Tests for singletons that required manual DCL application due to
non-standard patterns (multiple globals, complex branching, missing resets).

Modules:
  - adapters/airgap/factory.py
  - adapters/audit/singleton.py
  - adapters/redis/__init__.py
  - adapters/ipc/cb_state_snapshot.py
  - adapters/redis/circuit_breaker.py
  - adapters/redis/dlq.py
  - core/tls.py
  - meta/rate_limit_escalation.py
  - metrics/reconciler.py
  - services/postmortem/revision.py

Verification techniques:
  - §8.10 Singleton/lifecycle — get_*() caching, reset_*() clearing
  - §8.7 Concurrency/thread safety — multi-thread DCL consistency
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

# =============================================================================
# 1. AirGap Adapter Factory — DCL singleton
# =============================================================================


class TestAirgapAdapterSingletonBehavior:
    """Verify get_airgap_adapter / reset_airgap_adapter DCL singleton.

    Source: src/baldur/adapters/airgap/factory.py:31-73
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.airgap.factory import reset_airgap_adapter

        reset_airgap_adapter()
        yield
        reset_airgap_adapter()

    def test_get_airgap_adapter_returns_same_instance(self):
        """Repeated get_airgap_adapter() calls must return the same instance."""
        from baldur.adapters.airgap.factory import get_airgap_adapter

        first = get_airgap_adapter()
        second = get_airgap_adapter()
        assert first is second

    def test_reset_airgap_adapter_clears_singleton(self):
        """After reset, get_airgap_adapter must create a new instance."""
        from baldur.adapters.airgap.factory import (
            get_airgap_adapter,
            reset_airgap_adapter,
        )

        first = get_airgap_adapter()
        reset_airgap_adapter()
        second = get_airgap_adapter()
        assert first is not second

    def test_configure_airgap_adapter_overrides_singleton(self):
        """configure_airgap_adapter must replace the cached instance."""
        from baldur.adapters.airgap.base import AirGapStorageAdapter
        from baldur.adapters.airgap.factory import (
            configure_airgap_adapter,
            get_airgap_adapter,
        )

        # Given
        first = get_airgap_adapter()
        custom = MagicMock(spec=AirGapStorageAdapter)

        # When
        configure_airgap_adapter(custom)

        # Then
        assert get_airgap_adapter() is custom
        assert get_airgap_adapter() is not first

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "airgap_adapter" in _REGISTRY

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get_airgap_adapter concurrently
        must all receive the same singleton instance."""
        from baldur.adapters.airgap.factory import get_airgap_adapter

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_airgap_adapter())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 2. Audit Adapter Singleton — DCL singleton
# =============================================================================


class TestAuditAdapterSingletonBehavior:
    """Verify get_audit_adapter / reset_audit_adapter DCL singleton.

    Source: src/baldur/adapters/audit/singleton.py:31-123
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.audit.singleton import reset_audit_adapter

        reset_audit_adapter()
        yield
        reset_audit_adapter()

    def test_get_audit_adapter_returns_same_instance(self):
        """Repeated get_audit_adapter() calls must return the same instance."""
        from baldur.adapters.audit.singleton import (
            configure_audit_adapter,
            get_audit_adapter,
        )

        # Given — inject a known adapter to avoid fallback chain side effects
        custom = MagicMock()
        configure_audit_adapter(custom)

        # When
        first = get_audit_adapter()
        second = get_audit_adapter()

        # Then
        assert first is second
        assert first is custom

    def test_reset_audit_adapter_clears_singleton(self):
        """After reset, get_audit_adapter must create a new instance."""
        from baldur.adapters.audit.singleton import (
            configure_audit_adapter,
            get_audit_adapter,
            reset_audit_adapter,
        )

        # Given
        adapter_a = MagicMock(name="a1")
        configure_audit_adapter(adapter_a)
        first = get_audit_adapter()

        # When
        reset_audit_adapter()
        adapter_b = MagicMock(name="a2")
        configure_audit_adapter(adapter_b)
        second = get_audit_adapter()

        # Then
        assert first is not second

    def test_configure_audit_adapter_overrides_singleton(self):
        """configure_audit_adapter must replace the cached instance."""
        from baldur.adapters.audit.singleton import (
            configure_audit_adapter,
            get_audit_adapter,
        )

        # Given
        custom = MagicMock()
        configure_audit_adapter(custom)

        # Then
        assert get_audit_adapter() is custom

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "audit_adapter" in _REGISTRY


# =============================================================================
# 3. Redis Client Singleton — DCL singleton
# =============================================================================


class TestRedisClientSingletonBehavior:
    """Verify get_redis_client / reset_redis_client DCL singleton.

    Source: src/baldur/adapters/redis/__init__.py:36-80
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.redis import reset_redis_client

        reset_redis_client()
        yield
        reset_redis_client()

    @patch(
        "baldur.adapters.redis._try_acquire_redis_client",
        autospec=True,
    )
    def test_get_redis_client_returns_same_instance(self, mock_acquire):
        """Repeated get_redis_client() calls must return the same instance."""
        mock_client = MagicMock(name="redis_client")
        mock_acquire.return_value = mock_client

        from baldur.adapters.redis import get_redis_client

        first = get_redis_client()
        second = get_redis_client()
        assert first is second
        mock_acquire.assert_called_once()

    @patch(
        "baldur.adapters.redis._try_acquire_redis_client",
        autospec=True,
    )
    def test_reset_redis_client_clears_singleton(self, mock_acquire):
        """After reset, get_redis_client must re-acquire."""
        mock_acquire.side_effect = [MagicMock(name="c1"), MagicMock(name="c2")]

        from baldur.adapters.redis import get_redis_client, reset_redis_client

        first = get_redis_client()
        reset_redis_client()
        second = get_redis_client()
        assert first is not second
        assert mock_acquire.call_count == 2

    def test_module_has_threading_lock(self):
        """Module must define _redis_client_lock for DCL pattern."""
        import baldur.adapters.redis as mod

        assert hasattr(mod, "_redis_client_lock")
        assert isinstance(mod._redis_client_lock, type(threading.Lock()))

    @patch(
        "baldur.adapters.redis._try_acquire_redis_client",
        autospec=True,
    )
    def test_concurrent_get_returns_same_instance(self, mock_acquire):
        """Multiple threads calling get_redis_client concurrently
        must all receive the same singleton instance."""
        mock_client = MagicMock(name="redis_shared")
        mock_acquire.return_value = mock_client

        from baldur.adapters.redis import get_redis_client

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_redis_client())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 4. CB State Snapshot Singleton — DCL singleton
# =============================================================================


class TestCBStateSnapshotSingletonBehavior:
    """Verify get_cb_state_snapshot / reset_cb_state_snapshot DCL singleton.

    Source: src/baldur/adapters/ipc/cb_state_snapshot.py:666-701
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.ipc.cb_state_snapshot import (
            reset_cb_state_snapshot,
        )

        reset_cb_state_snapshot()
        yield
        reset_cb_state_snapshot()

    def test_get_cb_state_snapshot_returns_same_instance(self):
        """Repeated get_cb_state_snapshot() calls must return the same instance."""
        from baldur.adapters.ipc.cb_state_snapshot import (
            CBStateSnapshot,
            get_cb_state_snapshot,
        )

        first = get_cb_state_snapshot()
        second = get_cb_state_snapshot()
        assert first is second
        assert isinstance(first, CBStateSnapshot)

    def test_reset_cb_state_snapshot_clears_singleton(self):
        """After reset, get_cb_state_snapshot must create a new instance."""
        from baldur.adapters.ipc.cb_state_snapshot import (
            get_cb_state_snapshot,
            reset_cb_state_snapshot,
        )

        first = get_cb_state_snapshot()
        reset_cb_state_snapshot()
        second = get_cb_state_snapshot()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "cb_state_snapshot" in _REGISTRY


# =============================================================================
# 5. Redis Circuit Breaker Repo Singleton — DCL + reset added
# =============================================================================


class TestRedisCircuitBreakerRepoSingletonBehavior:
    """Verify get/reset_redis_circuit_breaker_repo DCL singleton.

    Source: src/baldur/adapters/redis/circuit_breaker.py:821-860
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.redis.circuit_breaker import (
            reset_redis_circuit_breaker_repo,
        )

        reset_redis_circuit_breaker_repo()
        yield
        reset_redis_circuit_breaker_repo()

    @patch(
        "baldur.adapters.redis.circuit_breaker.RedisCircuitBreakerStateRepository",
        autospec=True,
    )
    @patch(
        "baldur.adapters.resilient.backend.get_storage_backend",
        autospec=True,
    )
    def test_get_redis_cb_repo_returns_same_instance(
        self, mock_get_backend, mock_repo_cls
    ):
        """Repeated get_redis_circuit_breaker_repo() calls must return the same instance."""
        mock_backend = MagicMock()
        mock_get_backend.return_value = mock_backend
        mock_instance = MagicMock()
        mock_repo_cls.return_value = mock_instance

        from baldur.adapters.redis.circuit_breaker import (
            get_redis_circuit_breaker_repo,
        )

        first = get_redis_circuit_breaker_repo()
        second = get_redis_circuit_breaker_repo()
        assert first is second
        mock_repo_cls.assert_called_once()

    @patch(
        "baldur.adapters.redis.circuit_breaker.RedisCircuitBreakerStateRepository",
        autospec=True,
    )
    @patch(
        "baldur.adapters.resilient.backend.get_storage_backend",
        autospec=True,
    )
    def test_reset_redis_cb_repo_clears_singleton(
        self, mock_get_backend, mock_repo_cls
    ):
        """After reset, get_redis_circuit_breaker_repo must create a new instance."""
        mock_get_backend.return_value = MagicMock()
        mock_repo_cls.side_effect = [MagicMock(name="r1"), MagicMock(name="r2")]

        from baldur.adapters.redis.circuit_breaker import (
            get_redis_circuit_breaker_repo,
            reset_redis_circuit_breaker_repo,
        )

        first = get_redis_circuit_breaker_repo()
        reset_redis_circuit_breaker_repo()
        second = get_redis_circuit_breaker_repo()
        assert first is not second

    def test_module_has_threading_lock(self):
        """Module must define _redis_cb_repo_lock for DCL pattern."""
        import baldur.adapters.redis.circuit_breaker as mod

        assert hasattr(mod, "_redis_cb_repo_lock")
        assert isinstance(mod._redis_cb_repo_lock, type(threading.Lock()))


# =============================================================================
# 6. Redis DLQ Repo Singleton — DCL + reset added
# =============================================================================


class TestRedisDLQRepoSingletonBehavior:
    """Verify get/reset_redis_dlq_repo DCL singleton.

    Source: src/baldur/adapters/redis/dlq.py:539-570
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.adapters.redis.dlq import reset_redis_dlq_repo

        reset_redis_dlq_repo()
        yield
        reset_redis_dlq_repo()

    @patch(
        "baldur.adapters.redis.dlq.RedisDLQRepository",
        autospec=True,
    )
    @patch(
        "baldur.adapters.resilient.backend.get_storage_backend",
        autospec=True,
    )
    def test_get_redis_dlq_repo_returns_same_instance(
        self, mock_get_backend, mock_repo_cls
    ):
        """Repeated get_redis_dlq_repo() calls must return the same instance."""
        mock_get_backend.return_value = MagicMock()
        mock_instance = MagicMock()
        mock_repo_cls.return_value = mock_instance

        from baldur.adapters.redis.dlq import get_redis_dlq_repo

        first = get_redis_dlq_repo()
        second = get_redis_dlq_repo()
        assert first is second
        mock_repo_cls.assert_called_once()

    @patch(
        "baldur.adapters.redis.dlq.RedisDLQRepository",
        autospec=True,
    )
    @patch(
        "baldur.adapters.resilient.backend.get_storage_backend",
        autospec=True,
    )
    def test_reset_redis_dlq_repo_clears_singleton(
        self, mock_get_backend, mock_repo_cls
    ):
        """After reset, get_redis_dlq_repo must create a new instance."""
        mock_get_backend.return_value = MagicMock()
        mock_repo_cls.side_effect = [MagicMock(name="d1"), MagicMock(name="d2")]

        from baldur.adapters.redis.dlq import (
            get_redis_dlq_repo,
            reset_redis_dlq_repo,
        )

        first = get_redis_dlq_repo()
        reset_redis_dlq_repo()
        second = get_redis_dlq_repo()
        assert first is not second

    def test_module_has_threading_lock(self):
        """Module must define _redis_dlq_repo_lock for DCL pattern."""
        import baldur.adapters.redis.dlq as mod

        assert hasattr(mod, "_redis_dlq_repo_lock")
        assert isinstance(mod._redis_dlq_repo_lock, type(threading.Lock()))


# =============================================================================
# 7. TLS Config Singleton — DCL singleton
# =============================================================================


class TestTLSConfigSingletonBehavior:
    """Verify get_tls_config / reset_tls_config DCL singleton.

    Source: src/baldur/core/tls.py:77-92
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.core.tls import reset_tls_config

        reset_tls_config()
        yield
        reset_tls_config()

    def test_get_tls_config_returns_same_instance(self):
        """Repeated get_tls_config() calls must return the same instance."""
        from baldur.core.tls import TLSConfig, get_tls_config

        first = get_tls_config()
        second = get_tls_config()
        assert first is second
        assert isinstance(first, TLSConfig)

    def test_reset_tls_config_clears_singleton(self):
        """After reset, get_tls_config must create a new instance."""
        from baldur.core.tls import get_tls_config, reset_tls_config

        first = get_tls_config()
        reset_tls_config()
        second = get_tls_config()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "tls_config" in _REGISTRY

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get_tls_config concurrently
        must all receive the same singleton instance."""
        from baldur.core.tls import get_tls_config

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_tls_config())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 8. Rate Limit Escalation Handler Singleton — DCL + reset added
# =============================================================================


class TestRateLimitEscalationHandlerSingletonBehavior:
    """Verify get/reset_rate_limit_escalation_handler DCL singleton.

    Source: src/baldur/meta/rate_limit_escalation.py:205-232
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.meta.rate_limit_escalation import (
            reset_rate_limit_escalation_handler,
        )

        reset_rate_limit_escalation_handler()
        yield
        reset_rate_limit_escalation_handler()

    def test_get_rate_limit_escalation_handler_returns_same_instance(self):
        """Repeated calls must return the same instance."""
        from baldur.meta.rate_limit_escalation import (
            RateLimitEscalationHandler,
            get_rate_limit_escalation_handler,
        )

        first = get_rate_limit_escalation_handler()
        second = get_rate_limit_escalation_handler()
        assert first is second
        assert isinstance(first, RateLimitEscalationHandler)

    def test_reset_rate_limit_escalation_handler_clears_singleton(self):
        """After reset, handler must be a new instance."""
        from baldur.meta.rate_limit_escalation import (
            get_rate_limit_escalation_handler,
            reset_rate_limit_escalation_handler,
        )

        first = get_rate_limit_escalation_handler()
        reset_rate_limit_escalation_handler()
        second = get_rate_limit_escalation_handler()
        assert first is not second

    def test_singleton_registered_in_factory(self):
        """Singleton must be registered via make_singleton_factory."""
        from baldur.utils.singleton import _REGISTRY

        assert "rate_limit_escalation_handler" in _REGISTRY


# =============================================================================
# 9. Metric Reconciler Singleton — DCL singleton
# =============================================================================


class TestMetricReconcilerSingletonBehavior:
    """Verify get_reconciler / reset_reconciler DCL singleton.

    Source: src/baldur/metrics/reconciler.py:431-457
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        from baldur.metrics.reconciler import reset_reconciler

        reset_reconciler()
        yield
        reset_reconciler()

    def test_get_reconciler_returns_same_instance(self):
        """Repeated get_reconciler() calls must return the same instance."""
        from baldur.metrics.reconciler import MetricReconciler, get_reconciler

        first = get_reconciler()
        second = get_reconciler()
        assert first is second
        assert isinstance(first, MetricReconciler)

    def test_reset_reconciler_clears_singleton(self):
        """After reset, get_reconciler must create a new instance."""
        from baldur.metrics.reconciler import get_reconciler, reset_reconciler

        first = get_reconciler()
        reset_reconciler()
        second = get_reconciler()
        assert first is not second

    def test_module_has_threading_lock(self):
        """Module must define _reconciler_lock for DCL pattern."""
        import baldur.metrics.reconciler as mod

        assert hasattr(mod, "_reconciler_lock")
        assert isinstance(mod._reconciler_lock, type(threading.Lock()))

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get_reconciler concurrently
        must all receive the same singleton instance."""
        from baldur.metrics.reconciler import get_reconciler

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_reconciler())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)


# =============================================================================
# 10. Postmortem Revision Manager Singleton — DCL singleton
# =============================================================================


class TestPostmortemRevisionManagerSingletonBehavior:
    """Verify get/reset_postmortem_revision_manager DCL singleton.

    Source: src/baldur/services/postmortem/revision.py:757-807
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Ensure singleton is clean before and after each test."""
        pytest.importorskip("baldur_pro")
        from baldur_pro.services.postmortem.revision import (
            reset_postmortem_revision_manager,
        )

        reset_postmortem_revision_manager()
        yield
        reset_postmortem_revision_manager()

    def test_get_postmortem_revision_manager_returns_same_instance(self):
        """Repeated calls must return the same instance."""
        from baldur_pro.services.postmortem.revision import (
            PostmortemRevisionManager,
            get_postmortem_revision_manager,
        )

        first = get_postmortem_revision_manager()
        second = get_postmortem_revision_manager()
        assert first is second
        assert isinstance(first, PostmortemRevisionManager)

    def test_reset_postmortem_revision_manager_clears_singleton(self):
        """After reset, manager must be a new instance."""
        from baldur_pro.services.postmortem.revision import (
            get_postmortem_revision_manager,
            reset_postmortem_revision_manager,
        )

        first = get_postmortem_revision_manager()
        reset_postmortem_revision_manager()
        second = get_postmortem_revision_manager()
        assert first is not second

    def test_module_has_threading_lock(self):
        """Module must define _postmortem_revision_manager_lock for DCL pattern."""
        import baldur_pro.services.postmortem.revision as mod

        assert hasattr(mod, "_postmortem_revision_manager_lock")
        assert isinstance(mod._postmortem_revision_manager_lock, type(threading.Lock()))

    def test_concurrent_get_returns_same_instance(self):
        """Multiple threads calling get_postmortem_revision_manager concurrently
        must all receive the same singleton instance."""
        from baldur_pro.services.postmortem.revision import (
            get_postmortem_revision_manager,
        )

        results = []
        barrier = threading.Barrier(4)

        def worker():
            barrier.wait()
            results.append(get_postmortem_revision_manager())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(results) == 4
        assert all(r is results[0] for r in results)
