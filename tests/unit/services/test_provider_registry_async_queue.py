"""
ProviderRegistry async queue extension unit tests.

Verifies register_async_queue / get_async_queue methods:
registration, singleton caching, default selection, error handling, reset.

Test Categories:
    A. Behavior: registration, retrieval, singleton, default, error, reset
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import AdapterNotFoundError
from baldur.factory import ProviderRegistry
from baldur.interfaces.task_queue import AsyncTaskQueueInterface

# =============================================================================
# Minimal stub for testing
# =============================================================================


class _FakeAsyncQueue(AsyncTaskQueueInterface):
    """Fake async queue for registry tests."""

    @property
    def provider_name(self) -> str:
        return "fake"

    def task(self, name=None, *, max_retries=3, timeout=None, queue=None):
        def decorator(func):
            return func

        return decorator

    async def enqueue(self, task_name, args=(), kwargs=None, options=None):
        return "fake-id"

    async def enqueue_many(self, tasks, options=None):
        return ["fake-id"]

    async def get_result(self, task_id, timeout=None):
        return MagicMock()

    async def revoke(self, task_id):
        return True

    async def queue_length(self, queue_name="default"):
        return 0

    async def health_check(self):
        return True


class _FakeAsyncQueue2(_FakeAsyncQueue):
    """Second fake for multi-registration tests."""

    @property
    def provider_name(self) -> str:
        return "fake2"


# =============================================================================
# Behavior Tests
# =============================================================================


class TestProviderRegistryAsyncQueueBehavior:
    """ProviderRegistry async queue registration and retrieval."""

    def setup_method(self):
        """Save and clear async queue state before each test."""
        self._snapshot = ProviderRegistry.async_queue.save_state()
        # Fully blank state: reset() preserves auto_discover, but tests need
        # isolation from lazy discovery callbacks too.
        ProviderRegistry.async_queue.restore_state(
            {
                "providers": {},
                "instances": {},
                "default": None,
                "auto_discover": None,
            }
        )

    def teardown_method(self):
        """Restore async queue state after each test."""
        ProviderRegistry.async_queue.restore_state(self._snapshot)

    def test_register_async_queue_adds_to_registry(self):
        """register_async_queue stores the adapter class."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        assert ProviderRegistry.async_queue.has_provider("fake")
        assert ProviderRegistry.async_queue.get_provider("fake") is _FakeAsyncQueue

    def test_first_registered_becomes_default(self):
        """First registered async queue is automatically set as default."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        assert ProviderRegistry.async_queue.get_default_name() == "fake"

    def test_second_registered_does_not_change_default(self):
        """Subsequent registrations don't override the default."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        ProviderRegistry.register_async_queue("fake2", _FakeAsyncQueue2)
        assert ProviderRegistry.async_queue.get_default_name() == "fake"

    def test_get_async_queue_returns_instance(self):
        """get_async_queue returns an instance of the registered class."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        instance = ProviderRegistry.get_async_queue("fake")
        assert isinstance(instance, _FakeAsyncQueue)

    def test_get_async_queue_singleton(self):
        """get_async_queue with singleton=True returns same instance."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        first = ProviderRegistry.get_async_queue("fake")
        second = ProviderRegistry.get_async_queue("fake")
        assert first is second

    def test_get_async_queue_non_singleton(self):
        """get_async_queue with singleton=False returns new instance each time."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        first = ProviderRegistry.get_async_queue("fake", singleton=False)
        second = ProviderRegistry.get_async_queue("fake", singleton=False)
        assert first is not second

    def test_get_async_queue_default_selection(self):
        """get_async_queue with no name uses the default."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        instance = ProviderRegistry.get_async_queue()
        assert isinstance(instance, _FakeAsyncQueue)

    def test_get_async_queue_unknown_raises_adapter_not_found(self):
        """get_async_queue with unregistered name raises AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_async_queue("nonexistent")

    def test_get_async_queue_none_default_raises_adapter_not_found(self):
        """get_async_queue with no registered queues raises AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError):
            ProviderRegistry.get_async_queue()

    def test_list_providers_includes_async_queue(self):
        """list_providers includes async_queue key."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        providers = ProviderRegistry.list_providers()
        assert "async_queue" in providers
        assert "fake" in providers["async_queue"]

    def test_clear_instances_clears_async_queue_instances(self):
        """clear_instances removes cached async queue instances."""
        ProviderRegistry.register_async_queue("fake", _FakeAsyncQueue)
        ProviderRegistry.get_async_queue("fake")
        assert ProviderRegistry.async_queue.instance_count() > 0

        ProviderRegistry.clear_instances()
        assert ProviderRegistry.async_queue.instance_count() == 0
