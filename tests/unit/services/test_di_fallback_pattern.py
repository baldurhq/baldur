"""
DI fallback pattern tests for resolve_with_fallback and service integration.

Tests for the 3-tier FallbackPolicy:
- ALLOW: Silent fallback to in-memory adapter (no warning, no metric)
- WARN_AND_ALLOW: Fallback with warning log + Prometheus metric
- FAIL_FAST: Raise RuntimeError

Test Categories:
    A. Unit: resolve_with_fallback 3-tier behavior
    B. Integration: CircuitBreakerService / DLQServiceBase / ReplayService

Each mocked config marks ``fallback_policy`` as operator-set
(``model_fields_set``): an unset policy is derived from the environment
(801 D4), and these cases pin the policy itself.
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
        mock_config.model_fields_set = {"fallback_policy"}
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
        mock_config.model_fields_set = {"fallback_policy"}
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
            error="No repo",
        )
        mock_metric.assert_called_once_with("TestService", "InMemoryRepo")

    def test_warn_and_allow_policy_increments_prometheus_metric(self):
        """WARN_AND_ALLOW policy: increments di_fallback_total counter."""
        from baldur.core.di_fallback import resolve_with_fallback

        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.WARN_AND_ALLOW
        mock_config.model_fields_set = {"fallback_policy"}
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
        mock_config.model_fields_set = {"fallback_policy"}

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
        mock_config.model_fields_set = {"fallback_policy"}
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
        mock_config.model_fields_set = {"fallback_policy"}
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

    def test_repository_asks_the_registry_for_the_layered_view_first(self):
        """Normal path: repository comes from ProviderRegistry, named "layered".

        The name assertion is the point. The mock is name-blind — it returns
        the same object whichever view is requested — so a bare
        ``repo is mock_repo`` would hold no matter which view the property
        resolved, including the one that leaves an operator's manual pin
        invisible to the traffic path.
        """
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=mock_repo,
        ) as mock_get:
            repo = service.repository

        assert repo is mock_repo
        assert mock_get.call_args_list[0].kwargs == {"name": "layered"}

    def test_repository_falls_back_to_inmemory_when_allow_policy(self):
        """ALLOW policy: falls back to InMemory silently."""
        from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository

        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.ALLOW
        mock_config.model_fields_set = {"fallback_policy"}

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                side_effect=ValueError("No repo"),
            ) as mock_get,
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
        ):
            repo = service.repository

        assert isinstance(repo, InMemoryCircuitBreakerStateRepository)
        # The layered attempt happened and raised; the fallback chain is what
        # produced this repository, not a skipped first attempt.
        assert mock_get.call_args_list[0].kwargs == {"name": "layered"}

    def test_repository_raises_runtime_error_when_fail_fast_policy(self):
        """FAIL_FAST policy: raises RuntimeError."""
        service = self._make_service()
        mock_config = MagicMock()
        mock_config.fallback_policy = FallbackPolicy.FAIL_FAST
        mock_config.model_fields_set = {"fallback_policy"}

        with (
            patch(
                "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
                side_effect=ValueError("No repo"),
            ) as mock_get,
            patch(
                "baldur.settings.get_config",
                return_value=mock_config,
            ),
            pytest.raises(RuntimeError, match="ProviderRegistry unavailable"),
        ):
            _ = service.repository

        assert mock_get.call_args_list[0].kwargs == {"name": "layered"}

    def test_repository_caches_after_first_access(self):
        """Repository is cached after first successful access."""
        service = self._make_service()
        mock_repo = MagicMock()

        with patch(
            "baldur.factory.ProviderRegistry.get_circuit_breaker_repo",
            return_value=mock_repo,
        ) as mock_get:
            repo1 = service.repository
            repo2 = service.repository

        assert repo1 is repo2
        assert mock_get.call_count == 1
        assert mock_get.call_args_list[0].kwargs == {"name": "layered"}


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
        mock_config.model_fields_set = {"fallback_policy"}

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
        mock_config.model_fields_set = {"fallback_policy"}

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
        mock_config.model_fields_set = {"fallback_policy"}

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
        mock_config.model_fields_set = {"fallback_policy"}

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


# =============================================================================
# C. 801 D4 — the effective policy and the counter, through real settings
# =============================================================================


@pytest.fixture
def fallback_environment(monkeypatch):
    """Set the environment and the operator's ``FALLBACK_POLICY`` (None = unset).

    Real settings, not a mocked config: whether the policy counts as set is
    read from ``model_fields_set``, which only a real settings build fills.
    """
    from baldur.runtime import reset_runtime
    from baldur.settings.root import reset_config

    def _set(*, production: bool, policy: str | None) -> None:
        monkeypatch.setenv(
            "BALDUR_ENVIRONMENT", "production" if production else "development"
        )
        monkeypatch.delenv("BALDUR_TEST_MODE", raising=False)
        if policy is None:
            monkeypatch.delenv("FALLBACK_POLICY", raising=False)
        else:
            monkeypatch.setenv("FALLBACK_POLICY", policy)
        reset_runtime()
        reset_config()

    yield _set

    reset_runtime()
    reset_config()


class _InMemoryProbeAdapter:
    """Stand-in in-memory adapter; its name labels the fallback metric."""


def _unbuildable():
    raise ImportError("the adapter's driver is not installed")


class TestEffectiveFallbackPolicyBehavior:
    """Unless the operator set it, production announces a fallback (801 D4)."""

    @pytest.mark.parametrize(
        ("production", "policy", "effective"),
        [
            (True, None, FallbackPolicy.WARN_AND_ALLOW),
            (False, None, FallbackPolicy.ALLOW),
            (True, "allow", FallbackPolicy.ALLOW),
            (False, "warn", FallbackPolicy.WARN_AND_ALLOW),
            (False, "fail_fast", FallbackPolicy.FAIL_FAST),
        ],
        ids=[
            "production_unset_warns",
            "development_unset_allows",
            "production_explicit_allow_wins",
            "development_explicit_warn_wins",
            "development_explicit_fail_fast_wins",
        ],
    )
    def test_effective_policy_is_the_operators_else_the_environments(
        self, fallback_environment, production, policy, effective
    ):
        from baldur.core.di_fallback import _effective_fallback_policy

        fallback_environment(production=production, policy=policy)

        assert _effective_fallback_policy() == effective

    def test_an_explicit_allow_in_production_falls_back_silently(
        self, fallback_environment
    ):
        from structlog.testing import capture_logs

        from baldur.core.di_fallback import resolve_with_fallback

        fallback_environment(production=True, policy="allow")

        with capture_logs() as logs:
            adapter = resolve_with_fallback(
                _unbuildable, _InMemoryProbeAdapter, "ExplicitAllowService"
            )

        assert isinstance(adapter, _InMemoryProbeAdapter)
        assert [e for e in logs if e.get("event") == "service.fallback_adapter"] == []

    def test_a_production_fallback_increments_the_counter_on_the_real_facade(
        self, fallback_environment
    ):
        """G11: the counter used to be looked up as an attribute neither facade
        has, so it never moved; a mock that had the attribute hid it."""
        prometheus_client = pytest.importorskip("prometheus_client")

        from baldur.core.di_fallback import resolve_with_fallback

        fallback_environment(production=True, policy=None)
        labels = {
            "service": "CounterProbeService",
            "adapter": _InMemoryProbeAdapter.__name__,
        }
        sample = "baldur_di_fallback_total"
        before = prometheus_client.REGISTRY.get_sample_value(sample, labels) or 0.0

        resolve_with_fallback(_unbuildable, _InMemoryProbeAdapter, labels["service"])

        after = prometheus_client.REGISTRY.get_sample_value(sample, labels)
        assert after == before + 1


def _circuit_breaker_service():
    from baldur.services.circuit_breaker.service import CircuitBreakerService

    service = CircuitBreakerService.__new__(CircuitBreakerService)
    service._repository = None
    service._config = None
    service._event_bus = None
    service._sync_callbacks = []
    return service


def _dlq_capture_service():
    from baldur.services.dlq_capture.service import DLQCaptureService

    service = DLQCaptureService.__new__(DLQCaptureService)
    service._repository = None
    return service


def _replay_service():
    from baldur.services.replay_service.service import ReplayService

    service = ReplayService.__new__(ReplayService)
    service._repository = None
    service._config = {}
    service._adaptive_replay = None
    return service


def _security_service():
    from baldur.services.security.models import SecurityConfig
    from baldur.services.security.service import SecurityViolationService

    return SecurityViolationService(config=MagicMock(spec=SecurityConfig))


def _session_registry():
    from baldur.services.security.session_registry import UserSessionRegistry

    return UserSessionRegistry()


class TestProductionDIFallbackSitesBehavior:
    """Every lazy store lookup announces a production fallback (801 D4/D5).

    Each site resolves through ``resolve_with_fallback``; with
    ``FALLBACK_POLICY`` unset in production, a construction error yields the
    in-memory adapter plus one ``service.fallback_adapter`` WARNING — never a
    silent in-memory adapter. The security repository and cache and the
    session registry's cache carried their own silent fallback before.
    """

    @pytest.mark.parametrize(
        ("build_site", "attribute", "registry_getter", "fallback_path"),
        [
            (
                _circuit_breaker_service,
                "repository",
                "get_circuit_breaker_repo",
                "baldur.adapters.memory.InMemoryCircuitBreakerStateRepository",
            ),
            (
                _dlq_capture_service,
                "repository",
                "get_failed_operation_repo",
                "baldur.adapters.memory.InMemoryFailedOperationRepository",
            ),
            (
                _replay_service,
                "repository",
                "get_failed_operation_repo",
                "baldur.adapters.memory.InMemoryFailedOperationRepository",
            ),
            (
                _security_service,
                "repository",
                "get_security_repo",
                "baldur.adapters.memory.InMemorySecurityIncidentRepository",
            ),
            (
                _security_service,
                "cache",
                "get_cache",
                "baldur.adapters.cache.memory_adapter.InMemoryCacheAdapter",
            ),
            (
                _session_registry,
                "cache",
                "get_cache",
                "baldur.adapters.cache.memory_adapter.InMemoryCacheAdapter",
            ),
        ],
        ids=[
            "circuit_breaker_repository",
            "dlq_capture_repository",
            "replay_repository",
            "security_repository",
            "security_cache",
            "session_registry_cache",
        ],
    )
    @pytest.mark.parametrize("error", [ImportError, ValueError])
    def test_a_construction_error_yields_memory_with_one_warning(
        self,
        fallback_environment,
        build_site,
        attribute,
        registry_getter,
        fallback_path,
        error,
    ):
        import importlib

        from structlog.testing import capture_logs

        module_path, _, class_name = fallback_path.rpartition(".")
        fallback_cls = getattr(importlib.import_module(module_path), class_name)
        fallback_environment(production=True, policy=None)
        site = build_site()

        with (
            patch(
                f"baldur.factory.ProviderRegistry.{registry_getter}",
                side_effect=error("backend cannot be constructed"),
            ),
            capture_logs() as logs,
        ):
            adapter = getattr(site, attribute)

        assert isinstance(adapter, fallback_cls)
        announced = [e for e in logs if e.get("event") == "service.fallback_adapter"]
        assert [(e["log_level"], e["service"], e["adapter"]) for e in announced] == [
            ("warning", type(site).__name__, class_name)
        ]
