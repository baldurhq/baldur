"""
ProviderRegistry 테스트.
"""

import pytest


class TestProviderRegistry:
    """Tests for ProviderRegistry with In-Memory repositories."""

    @pytest.fixture(autouse=True)
    def reset_registry(self):
        """Reset ProviderRegistry before and after each test for isolation."""
        from baldur.adapters.memory import (
            InMemoryCircuitBreakerStateRepository,
            InMemoryFailedOperationRepository,
            InMemorySecurityIncidentRepository,
        )
        from baldur.factory import ProviderRegistry

        # Store original state (sub-registry level)
        fo_snapshot = ProviderRegistry.failed_op_repo.save_state()
        cb_snapshot = ProviderRegistry.circuit_breaker_repo.save_state()
        sr_snapshot = ProviderRegistry.security_repo.save_state()
        cache_snapshot = ProviderRegistry.cache.save_state()
        queue_snapshot = ProviderRegistry.queue.save_state()

        # Clear instances for fresh test
        ProviderRegistry.clear_instances()

        # Ensure memory adapters are registered
        if not ProviderRegistry.failed_op_repo.has_provider("memory"):
            ProviderRegistry.register_failed_operation_repo(
                "memory", InMemoryFailedOperationRepository
            )
        if not ProviderRegistry.circuit_breaker_repo.has_provider("memory"):
            ProviderRegistry.register_circuit_breaker_repo(
                "memory", InMemoryCircuitBreakerStateRepository
            )
        if not ProviderRegistry.security_repo.has_provider("memory"):
            ProviderRegistry.register_security_repo(
                "memory", InMemorySecurityIncidentRepository
            )

        yield

        # Restore original state
        ProviderRegistry.failed_op_repo.restore_state(fo_snapshot)
        ProviderRegistry.circuit_breaker_repo.restore_state(cb_snapshot)
        ProviderRegistry.security_repo.restore_state(sr_snapshot)
        ProviderRegistry.cache.restore_state(cache_snapshot)
        ProviderRegistry.queue.restore_state(queue_snapshot)

    def test_registry_has_inmemory_repositories_registered(self):
        """Test that in-memory repositories are auto-registered."""
        from baldur.factory import ProviderRegistry

        providers = ProviderRegistry.list_providers()
        assert "memory" in providers["failed_operation_repo"]
        assert "memory" in providers["circuit_breaker_repo"]
        assert "memory" in providers["security_repo"]

    def test_registry_creates_inmemory_repositories(self):
        """Test that registry creates in-memory repositories."""
        from baldur.adapters.memory import (
            InMemoryCircuitBreakerStateRepository,
            InMemoryFailedOperationRepository,
            InMemorySecurityIncidentRepository,
        )
        from baldur.factory import ProviderRegistry

        ProviderRegistry.clear_instances()

        failed_op_repo = ProviderRegistry.get_failed_operation_repo(name="memory")
        cb_repo = ProviderRegistry.get_circuit_breaker_repo(name="memory")
        security_repo = ProviderRegistry.get_security_repo(name="memory")

        assert isinstance(failed_op_repo, InMemoryFailedOperationRepository)
        assert isinstance(cb_repo, InMemoryCircuitBreakerStateRepository)
        assert isinstance(security_repo, InMemorySecurityIncidentRepository)

    def test_registry_caches_repositories(self):
        """Test that registry caches repository instances (singleton)."""
        from baldur.factory import ProviderRegistry

        ProviderRegistry.clear_instances()

        repo1 = ProviderRegistry.get_failed_operation_repo(name="memory")
        repo2 = ProviderRegistry.get_failed_operation_repo(name="memory")

        assert repo1 is repo2

    def test_registry_set_defaults_to_memory(self):
        """Test setting default to memory provider."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.factory import ProviderRegistry

        ProviderRegistry.clear_instances()
        ProviderRegistry.set_defaults(repo="memory")

        defaults = ProviderRegistry.get_defaults()
        assert defaults["repo"] == "memory"

        repo = ProviderRegistry.get_failed_operation_repo()
        assert isinstance(repo, InMemoryFailedOperationRepository)
