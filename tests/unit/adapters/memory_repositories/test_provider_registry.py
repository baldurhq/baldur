"""
Unit tests for ProviderRegistry.
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

    def test_registry_set_default_to_memory(self):
        """Test setting the DLQ registry's default to the memory provider."""
        from baldur.adapters.memory import InMemoryFailedOperationRepository
        from baldur.factory import ProviderRegistry

        ProviderRegistry.clear_instances()
        ProviderRegistry.failed_op_repo.set_default("memory")

        defaults = ProviderRegistry.get_defaults()
        assert defaults["repo"] == "memory"

        repo = ProviderRegistry.get_failed_operation_repo()
        assert isinstance(repo, InMemoryFailedOperationRepository)


class TestProviderRegistrySetDefaultsContract:
    """778 D4 — ``set_defaults`` no longer carries a ``repo`` shortcut.

    It used to set one provider name across the dead-letter, circuit-breaker
    and security registries at once. The framework's own wiring contradicts
    that: those three do not share a backend, and 778 wires the dead-letter
    registry to a chain of its own while breaker state stays on Redis.
    Callers set each registry's default individually.
    """

    def test_repo_keyword_is_rejected(self):
        """Passing it raises rather than silently doing nothing.

        Silent acceptance would be the worse outcome for a released
        keyword: a caller carrying it forward would read "no error" as "my
        three registries were set".
        """
        from baldur.factory import ProviderRegistry

        with pytest.raises(TypeError, match="repo"):
            ProviderRegistry.set_defaults(repo="memory")

    def test_cache_and_queue_shortcuts_still_apply(self):
        """The two parameters that survived still do their job."""
        from baldur.factory import ProviderRegistry

        with ProviderRegistry.cache.snapshot(), ProviderRegistry.queue.snapshot():
            ProviderRegistry.set_defaults(cache="memory", queue="sync")

            defaults = ProviderRegistry.get_defaults()
            assert defaults["cache"] == "memory"
            assert defaults["queue"] == "sync"

    def test_the_replacement_sets_one_registry_without_touching_the_others(self):
        """The migration path named in the removal's changelog entry.

        Setting the dead-letter default must leave the breaker and security
        registries where they were — the whole reason the shortcut went.
        """
        from baldur.factory import ProviderRegistry

        with (
            ProviderRegistry.failed_op_repo.snapshot(),
            ProviderRegistry.circuit_breaker_repo.snapshot(),
            ProviderRegistry.security_repo.snapshot(),
        ):
            ProviderRegistry.circuit_breaker_repo.set_default("redis")
            ProviderRegistry.security_repo.set_default("memory")

            ProviderRegistry.failed_op_repo.set_default("memory")

            assert ProviderRegistry.failed_op_repo.get_default_name() == "memory"
            assert ProviderRegistry.circuit_breaker_repo.get_default_name() == "redis"
            assert ProviderRegistry.security_repo.get_default_name() == "memory"

    def test_circuit_breaker_registry_offers_no_sql_provider(self):
        """778 D4 — the removed adapter is gone from the registry too.

        Registration is the framework advertising a surface; leaving "sql"
        registered for breaker state would keep advertising a store no
        wiring row, knob or documented path can select.
        """
        from baldur.factory import ProviderRegistry

        names = set(ProviderRegistry.circuit_breaker_repo.list_providers())

        assert "sql" not in names
        assert {"memory", "redis", "layered"} <= names
        # The dead-letter registry is the one that gained a selectable SQL
        # backend in the same change — the contrast is the point.
        assert "sql" in set(ProviderRegistry.failed_op_repo.list_providers())
