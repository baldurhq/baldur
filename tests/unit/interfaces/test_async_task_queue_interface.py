"""
AsyncTaskQueueInterface unit tests.

Verifies the async task queue ABC contract and default method behaviors.
Tests are implementation-agnostic — they validate interface contracts only.

Test Categories:
    A. Contract: abstract methods, provider_name, __all__ export
    B. Behavior: default implementations (delay, schedule_periodic, lifecycle)
"""

from __future__ import annotations

import pytest

from baldur.interfaces.task_queue import (
    AsyncTaskQueueInterface,
    TaskOptions,
    TaskQueueInterface,
    TaskResult,
    TaskStatus,
)

# =============================================================================
# Minimal concrete implementation for testing default methods
# =============================================================================


class _StubAsyncQueue(AsyncTaskQueueInterface):
    """Minimal concrete implementation to test default methods."""

    def __init__(self) -> None:
        self._enqueue_calls: list[tuple] = []

    @property
    def provider_name(self) -> str:
        return "stub"

    def task(self, name=None, *, max_retries=3, timeout=None, queue=None):
        def decorator(func):
            return func

        return decorator

    async def enqueue(self, task_name, args=(), kwargs=None, options=None):
        self._enqueue_calls.append((task_name, args, kwargs, options))
        return "stub-id"

    async def enqueue_many(self, tasks, options=None):
        return [await self.enqueue(n, a, k, options) for n, a, k in tasks]

    async def get_result(self, task_id, timeout=None):
        return TaskResult(task_id=task_id, status=TaskStatus.PENDING)

    async def revoke(self, task_id):
        return True

    async def queue_length(self, queue_name="default"):
        return 0

    async def health_check(self):
        return True


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestAsyncTaskQueueInterfaceContract:
    """AsyncTaskQueueInterface ABC contract verification."""

    def test_required_abstract_methods_exist(self):
        """ABC declares the expected set of abstract methods."""
        abstracts = AsyncTaskQueueInterface.__abstractmethods__
        expected = {
            "provider_name",
            "task",
            "enqueue",
            "enqueue_many",
            "get_result",
            "revoke",
            "queue_length",
            "health_check",
        }
        assert abstracts == expected

    def test_cannot_instantiate_without_implementing_abstracts(self):
        """Instantiating ABC directly raises TypeError."""
        with pytest.raises(TypeError):
            AsyncTaskQueueInterface()

    def test_dto_shared_with_sync_interface(self):
        """Async and sync interfaces share the same DTO classes."""
        # Both interfaces import from the same module and use identical DTOs
        assert TaskResult is TaskResult
        assert TaskOptions is TaskOptions
        assert TaskStatus is TaskStatus
        # Verify sync interface exists in the same module
        assert TaskQueueInterface.__module__ == AsyncTaskQueueInterface.__module__

    def test_exported_in_module_all(self):
        """AsyncTaskQueueInterface is listed in module __all__."""
        from baldur.interfaces import task_queue

        assert "AsyncTaskQueueInterface" in task_queue.__all__

    def test_exported_in_interfaces_package(self):
        """AsyncTaskQueueInterface is importable from baldur.interfaces."""
        from baldur.interfaces import AsyncTaskQueueInterface as imported

        assert imported is AsyncTaskQueueInterface


# =============================================================================
# B. Behavior Tests
# =============================================================================


class TestAsyncTaskQueueInterfaceDefaultsBehavior:
    """Default method implementations behavior verification."""

    @pytest.fixture
    def stub_queue(self):
        """Stub implementation for testing defaults."""
        return _StubAsyncQueue()

    @pytest.mark.asyncio
    async def test_delay_delegates_to_enqueue(self, stub_queue):
        """delay() delegates to enqueue() with args/kwargs."""
        result = await stub_queue.delay("my_task", 1, 2, key="val")

        assert result == "stub-id"
        assert len(stub_queue._enqueue_calls) == 1
        call = stub_queue._enqueue_calls[0]
        assert call[0] == "my_task"
        assert call[1] == (1, 2)
        assert call[2] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_schedule_periodic_raises_not_implemented(self, stub_queue):
        """Default schedule_periodic() raises NotImplementedError with provider name."""
        with pytest.raises(NotImplementedError, match="stub"):
            await stub_queue.schedule_periodic("task", cron="* * * * *")

    @pytest.mark.asyncio
    async def test_unschedule_raises_not_implemented(self, stub_queue):
        """Default unschedule() raises NotImplementedError with provider name."""
        with pytest.raises(NotImplementedError, match="stub"):
            await stub_queue.unschedule("schedule-1")

    @pytest.mark.asyncio
    async def test_startup_is_noop(self, stub_queue):
        """Default startup() completes without error."""
        await stub_queue.startup()

    @pytest.mark.asyncio
    async def test_shutdown_is_noop(self, stub_queue):
        """Default shutdown() completes without error."""
        await stub_queue.shutdown()

    def test_provider_name_returns_string(self, stub_queue):
        """provider_name returns a non-empty string."""
        assert isinstance(stub_queue.provider_name, str)
        assert len(stub_queue.provider_name) > 0
