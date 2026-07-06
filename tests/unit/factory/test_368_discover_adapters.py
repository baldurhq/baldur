"""
368 Factory adapter discovery unit tests.

Tests for discover_database_health_adapters() and discover_session_adapters().

Test Categories:
    A. Behavior: Registration of adapters via discovery functions
"""

from baldur.factory.adapters import (
    discover_database_health_adapters,
    discover_session_adapters,
)
from baldur.factory.registry import ProviderRegistry

# =============================================================================
# A. Behavior Tests
# =============================================================================


class TestDiscoverDatabaseHealthAdaptersBehavior:
    """discover_database_health_adapters() registers expected providers."""

    def setup_method(self):
        """Reset database_health registry before each test."""
        ProviderRegistry.database_health.reset()

    def test_registers_noop_adapter(self):
        """Noop adapter is registered after discovery."""
        discover_database_health_adapters()
        assert ProviderRegistry.database_health.has_provider("noop")

    def test_registers_django_adapter(self):
        """Django adapter is registered after discovery (Django is installed)."""
        discover_database_health_adapters()
        assert ProviderRegistry.database_health.has_provider("django")

    def test_idempotent_double_call(self):
        """Calling discovery twice does not duplicate registrations."""
        discover_database_health_adapters()
        discover_database_health_adapters()
        # Should still have exactly the same providers, no error
        assert ProviderRegistry.database_health.has_provider("noop")


class TestDiscoverSessionAdaptersBehavior:
    """discover_session_adapters() registers expected providers."""

    def setup_method(self):
        """Reset session_invalidation registry before each test."""
        ProviderRegistry.session_invalidation.reset()

    def test_registers_noop_adapter(self):
        """Noop adapter is registered after discovery."""
        discover_session_adapters()
        assert ProviderRegistry.session_invalidation.has_provider("noop")

    def test_registers_django_adapter(self):
        """Django adapter is registered after discovery (Django is installed)."""
        discover_session_adapters()
        assert ProviderRegistry.session_invalidation.has_provider("django")

    def test_idempotent_double_call(self):
        """Calling discovery twice does not duplicate registrations."""
        discover_session_adapters()
        discover_session_adapters()
        assert ProviderRegistry.session_invalidation.has_provider("noop")
