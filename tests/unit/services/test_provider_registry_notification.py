"""
ProviderRegistry notification adapter and DCL tests (commit 0b59f932).

Tests for:
- register_notification / get_notification
- Double-Checked Locking singleton creation
- auto_discover notification adapters
- reset / clear_instances includes notification state
- list_providers includes notification key

Test Categories:
    A. Contract: Default values, list_providers keys
    B. Behavior: Registration, get, DCL caching, reset, thread-safety
"""

import threading
from unittest.mock import MagicMock

import pytest

from baldur.factory import ProviderRegistry

# =============================================================================
# Fixture: isolate ProviderRegistry state
# =============================================================================


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Save and restore ProviderRegistry notification sub-registry state."""
    snapshot = ProviderRegistry.notification.save_state()

    ProviderRegistry.notification.reset()
    ProviderRegistry.notification.set_default("logging")

    yield

    ProviderRegistry.notification.restore_state(snapshot)


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestProviderRegistryNotificationContract:
    """Verify default notification settings and list_providers structure."""

    def test_default_notification_name_is_logging(self):
        """Default notification adapter name is 'logging'."""
        assert ProviderRegistry.notification.get_default_name() == "logging"

    def test_list_providers_contains_notification_key(self):
        """list_providers() includes 'notification' key."""
        providers = ProviderRegistry.list_providers()
        assert "notification" in providers

    def test_list_providers_notification_reflects_registered(self):
        """list_providers()['notification'] reflects registered adapters."""
        mock_factory = MagicMock()
        ProviderRegistry.register_notification("test_channel", mock_factory)

        providers = ProviderRegistry.list_providers()
        assert "test_channel" in providers["notification"]


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestProviderRegistryNotificationBehavior:
    """Verify registration, retrieval, DCL caching, and reset."""

    def test_register_notification_stores_factory(self):
        """register_notification stores the factory in sub-registry."""
        mock_factory = MagicMock()
        ProviderRegistry.register_notification("slack", mock_factory)

        assert ProviderRegistry.notification.has_provider("slack")
        assert ProviderRegistry.notification.get_provider("slack") is mock_factory

    def test_get_notification_returns_instance(self):
        """get_notification creates and returns an adapter instance."""
        mock_adapter = MagicMock()
        mock_factory = MagicMock(return_value=mock_adapter)
        ProviderRegistry.register_notification("test", mock_factory)

        result = ProviderRegistry.get_notification("test")

        assert result is mock_adapter
        mock_factory.assert_called_once()

    def test_get_notification_caches_instance(self):
        """get_notification caches instance (DCL singleton)."""
        mock_adapter = MagicMock()
        mock_factory = MagicMock(return_value=mock_adapter)
        ProviderRegistry.register_notification("test", mock_factory)

        result1 = ProviderRegistry.get_notification("test")
        result2 = ProviderRegistry.get_notification("test")

        assert result1 is result2
        mock_factory.assert_called_once()

    def test_get_notification_uses_default_when_name_is_none(self):
        """get_notification(None) uses default notification."""
        from baldur.interfaces.notification import LoggingNotificationAdapter

        # Auto-registration should kick in via auto_discover
        ProviderRegistry.register_notification("logging", LoggingNotificationAdapter)
        result = ProviderRegistry.get_notification(None)
        assert isinstance(result, LoggingNotificationAdapter)

    def test_get_notification_unknown_name_raises_adapter_not_found_error(self):
        """get_notification with unknown name raises AdapterNotFoundError."""
        from baldur.core.exceptions import AdapterNotFoundError

        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_notification("nonexistent_channel")

    def test_clear_instances_clears_notification_instances(self):
        """clear_instances() clears notification instances."""
        mock_adapter = MagicMock()
        ProviderRegistry.notification.set_instance("test", mock_adapter)

        ProviderRegistry.clear_instances()

        assert ProviderRegistry.notification.instance_count() == 0

    def test_reset_clears_notifications_and_instances(self):
        """reset() clears both providers and instances."""
        ProviderRegistry.register_notification("x", MagicMock())
        ProviderRegistry.notification.set_instance("x", MagicMock())

        ProviderRegistry.reset()

        assert ProviderRegistry.notification.list_providers() == []
        assert ProviderRegistry.notification.instance_count() == 0

    def test_reset_restores_default_notification_to_none(self):
        """reset() clears the default (GenericProviderRegistry resets to None)."""
        ProviderRegistry.notification.set_default("custom")
        ProviderRegistry.reset()

        assert ProviderRegistry.notification.get_default_name() is None


class TestProviderRegistryDCLThreadSafety:
    """Verify DCL thread safety for get_notification."""

    def test_concurrent_get_notification_returns_same_instance(self):
        """Multiple threads calling get_notification get the same instance."""
        from baldur.interfaces.notification import LoggingNotificationAdapter

        ProviderRegistry.register_notification("logging", LoggingNotificationAdapter)

        results = []
        errors = []

        def worker():
            try:
                results.append(ProviderRegistry.get_notification("logging"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        assert all(r is results[0] for r in results)

    def test_concurrent_get_cache_returns_same_singleton(self):
        """DCL for get_cache returns same singleton across threads."""
        from baldur.adapters.cache import InMemoryCacheAdapter

        ProviderRegistry.register_cache("memory", InMemoryCacheAdapter)

        results = []

        def worker():
            results.append(ProviderRegistry.get_cache(name="memory", singleton=True))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r is results[0] for r in results)
