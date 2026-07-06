"""
DI fallback pattern tests for resolve_with_fallback and service integration.

Tests for the 3-tier FallbackPolicy:
- ALLOW: Silent fallback to in-memory adapter (no warning, no metric)
- WARN_AND_ALLOW: Fallback with warning log + Prometheus metric
- FAIL_FAST: Raise RuntimeError

Test Categories:
    A. Unit: resolve_with_fallback 3-tier behavior
    B. Integration: CircuitBreakerService / DLQServiceBase / ReplayService
"""

from unittest.mock import MagicMock, patch

import pytest

from baldur.settings.root import FallbackPolicy

# =============================================================================
# A. Unit Tests — resolve_with_fallback
# =============================================================================


class TestResolveWithFallbackBehavior:
    """Verify 3-tier policy in resolve_with_fallback."""

    def test_returns_registry_result_on_success(self):
        """Normal path: returns result from registry_method."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_repo = MagicMock()
        result = resolve_with_fallback(
            registry_method=lambda: mock_repo,
            fallback_class=MagicMock,
            service_name="TestService",
        )
        assert result is mock_repo

    def test_allow_policy_returns_fallback_silently(self):
        """ALLOW policy: returns fallback without warning or metric."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW
        fallback_cls = MagicMock()
        fallback_instance = MagicMock()
        fallback_cls.return_value = fallback_instance

        with (
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            patch("baldur.core.di_fallback.logger") as mock_logger,
        ):
            result = resolve_with_fallback(
                registry_method=MagicMock(side_effect=ValueError("No repo")),
                fallback_class=fallback_cls,
                service_name="TestService",
            )

        assert result is fallback_instance
        mock_logger.warning.assert_not_called()

    def test_warn_and_allow_policy_returns_fallback_with_warning(self):
        """WARN_AND_ALLOW policy: returns fallback with warning log."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.WARN_AND_ALLOW
        fallback_cls = MagicMock()
        fallback_cls.__name__ = "InMemoryRepo"
        fallback_instance = MagicMock()
        fallback_cls.return_value = fallback_instance

        with (
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            patch("baldur.core.di_fallback.logger") as mock_logger,
            patch("baldur.core.di_fallback._inc_fallback_metric") as mock_metric,
        ):
            result = resolve_with_fallback(
                registry_method=MagicMock(side_effect=ValueError("No repo")),
                fallback_class=fallback_cls,
                service_name="TestService",
            )

        assert result is fallback_instance
        mock_logger.warning.assert_called_once_with(
            "service.fallback_adapter",
            adapter="InMemoryRepo",
            service="TestService",
        )
        mock_metric.assert_called_once_with("TestService", "InMemoryRepo")

    def test_warn_and_allow_policy_increments_prometheus_metric(self):
        """WARN_AND_ALLOW policy: increments di_fallback_total counter."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.WARN_AND_ALLOW
        fallback_cls = MagicMock()
        fallback_cls.__name__ = "InMemoryRepo"

        with (
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            patch("baldur.core.di_fallback._inc_fallback_metric") as mock_inc,
        ):
            resolve_with_fallback(
                registry_method=MagicMock(side_effect=ValueError("No repo")),
                fallback_class=fallback_cls,
                service_name="CBService",
            )

        mock_inc.assert_called_once_with("CBService", "InMemoryRepo")

    def test_fail_fast_policy_raises_runtime_error(self):
        """FAIL_FAST policy: raises RuntimeError."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST

        with (
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            pytest.raises(RuntimeError, match="ProviderRegistry unavailable"),
        ):
            resolve_with_fallback(
                registry_method=MagicMock(side_effect=ValueError("No repo")),
                fallback_class=MagicMock,
                service_name="TestService",
            )

    def test_fail_fast_policy_chains_original_exception(self):
        """FAIL_FAST policy: RuntimeError chains the original exception."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST
        original_exc = ValueError("Original error")

        with (
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                resolve_with_fallback(
                    registry_method=MagicMock(side_effect=original_exc),
                    fallback_class=MagicMock,
                    service_name="TestService",
                )
            assert exc_info.value.__cause__ is original_exc

    def test_handles_import_error_from_registry(self):
        """ImportError from registry_method is caught and handled."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW
        fallback_cls = MagicMock()

        with patch(
            "baldur.settings.get_config",
            return_value=mock_config,
        ):
            result = resolve_with_fallback(
                registry_method=MagicMock(side_effect=ImportError("no module")),
                fallback_class=fallback_cls,
                service_name="TestService",
            )

        assert result is fallback_cls.return_value


# =============================================================================
# B. Integration Tests — Service .repository property
# =============================================================================


class TestCircuitBreakerServiceDIFallbackBehavior:
    """Verify CircuitBreakerService.repository uses resolve_with_fallback."""

    def _make_service(self):
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        service = CircuitBreakerService.__new__(CircuitBreakerService)
        service._repository = None
        service._config = None
        service._event_bus = None
        service._sync_callbacks = []
        return service

    def test_repository_uses_provider_registry_when_available(self):
        """Normal path: repository comes from ProviderRegistry."""
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=mock_repo,
        ):
            repo = service.repository

        assert repo is mock_repo

    def test_repository_falls_back_to_inmemory_when_allow_policy(self):
        """ALLOW policy: falls back to InMemory silently."""
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository

        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
        ):
            repo = service.repository

        assert isinstance(repo, InMemoryCircuitBreakerStateRepository)

    def test_repository_raises_runtime_error_when_fail_fast_policy(self):
        """FAIL_FAST policy: raises RuntimeError."""
        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            pytest.raises(RuntimeError, match="ProviderRegistry unavailable"),
        ):
            _ = service.repository

    def test_repository_caches_after_first_access(self):
        """Repository is cached after first successful access."""
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=mock_repo,
        ):
            repo1 = service.repository
            repo2 = service.repository

        assert repo1 is repo2


class TestDLQServiceBaseDIFallbackBehavior:
    """Verify DLQServiceBase.repository uses resolve_with_fallback."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    def _make_service(self):
        from baldur_pro.services.dlq.base import DLQServiceBase

        service = DLQServiceBase.__new__(DLQServiceBase)
        service._repository = None
        service.config = MagicMock(enabled=True)
        return service

    def test_repository_uses_provider_registry_when_available(self):
        """Normal path: repository comes from ProviderRegistry."""
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=mock_repo,
        ):
            repo = service.repository

        assert repo is mock_repo

    def test_repository_falls_back_to_inmemory_when_allow_policy(self):
        """ALLOW policy: falls back to InMemory silently."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
        ):
            repo = service.repository

        assert isinstance(repo, InMemoryFailedOperationRepository)

    def test_repository_raises_runtime_error_when_fail_fast_policy(self):
        """FAIL_FAST policy: raises RuntimeError."""
        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            pytest.raises(RuntimeError, match="ProviderRegistry unavailable"),
        ):
            _ = service.repository


class TestReplayServiceDIFallbackBehavior:
    """Verify ReplayService.repository uses resolve_with_fallback."""

    def _make_service(self):
        from baldur.services.replay_service.service import ReplayService

        service = ReplayService.__new__(ReplayService)
        service._repository = None
        service._config = {}
        service._adaptive_replay = None
        return service

    def test_repository_uses_provider_registry_when_available(self):
        """Normal path: repository comes from ProviderRegistry."""
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_failed_operation_repo",
            return_value=mock_repo,
        ):
            repo = service.repository

        assert repo is mock_repo

    def test_repository_falls_back_to_inmemory_when_allow_policy(self):
        """ALLOW policy: falls back to InMemory silently."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository

        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
        ):
            repo = service.repository

        assert isinstance(repo, InMemoryFailedOperationRepository)

    def test_repository_raises_runtime_error_when_fail_fast_policy(self):
        """FAIL_FAST policy: raises RuntimeError."""
        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_failed_operation_repo",
                side_effect=ValueError("No repo"),
            ),
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            pytest.raises(RuntimeError, match="ProviderRegistry unavailable"),
        ):
            _ = service.repository
