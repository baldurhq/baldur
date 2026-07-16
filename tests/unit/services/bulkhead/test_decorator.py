"""
@bulkhead decorator unit tests.

Verifies the sync/async auto-dispatching decorator:
- automatic sync function detection and wrapping
- automatic async function detection and wrapping
- fallback function support
- unregistered-custom-domain strict parity (both colors raise
  BulkheadNotFoundError; fallback does not mask it)
- docstring usage examples are executable as rewritten
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from baldur.core.connection_health import ConnectionType
from baldur.services.bulkhead.decorator import (
    bulkhead,
    bulkhead_for_cache,
    bulkhead_for_database,
)
from baldur.services.bulkhead.exceptions import (
    BulkheadFullError,
    BulkheadNotFoundError,
)
from baldur.services.bulkhead.registry import (
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead
from baldur.settings.bulkhead import reset_bulkhead_settings


@pytest.fixture(autouse=True)
def _empty_provider_slot(monkeypatch):
    """Pin the resolution chain to its fallback leg for this module.

    These tests exercise the base registry/decorator semantics; a populated
    provider slot (e.g. a registry overlay registered by another test's
    environment) would leak saturated compartments across tests because
    reset_bulkhead_registry() clears only the fallback leg.
    """
    from baldur.factory.registry import ProviderRegistry

    monkeypatch.setattr(
        ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
    )


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before and after each test."""
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()


class TestBulkheadDecoratorSync:
    """Sync function decorator tests."""

    def test_sync_function_wrapped(self):
        """A sync function is wrapped by the bulkhead."""
        call_count = 0

        @bulkhead(ConnectionType.DATABASE)
        def sync_work():
            nonlocal call_count
            call_count += 1
            return "result"

        result = sync_work()
        assert result == "result"
        assert call_count == 1

    def test_sync_function_with_args(self):
        """A sync function with positional args."""

        @bulkhead("database")
        def add(a: int, b: int) -> int:
            return a + b

        result = add(3, 5)
        assert result == 8

    def test_sync_function_with_kwargs(self):
        """A sync function with keyword args."""

        @bulkhead(ConnectionType.CACHE)
        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        result = greet("World", greeting="Hi")
        assert result == "Hi, World!"

    def test_sync_function_raises_when_full(self):
        """The bulkhead raises when full."""
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        # Occupy every slot of the bulkhead.
        max_concurrent = db_bulkhead.get_state().max_concurrent
        for _ in range(max_concurrent):
            db_bulkhead.try_acquire()

        @bulkhead(ConnectionType.DATABASE)
        def will_fail():
            return "never"

        with pytest.raises(BulkheadFullError):
            will_fail()

        # Cleanup.
        for _ in range(max_concurrent):
            db_bulkhead.release()


class TestBulkheadDecoratorAsync:
    """Async function decorator tests."""

    @pytest.mark.asyncio
    async def test_async_function_wrapped(self):
        """An async function is wrapped by the bulkhead."""
        call_count = 0

        @bulkhead(ConnectionType.DATABASE)
        async def async_work():
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return "async_result"

        result = await async_work()
        assert result == "async_result"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_async_function_with_args(self):
        """An async function with positional args."""

        @bulkhead("database")
        async def async_add(a: int, b: int) -> int:
            await asyncio.sleep(0.01)
            return a + b

        result = await async_add(10, 20)
        assert result == 30


class TestBulkheadDecoratorUnregisteredDomain:
    """Unregistered custom domain: strict both-color parity (D1/D3/D7)."""

    def test_sync_unregistered_domain_raises_not_found(self):
        """A sync call on a never-registered domain raises BulkheadNotFoundError."""

        @bulkhead("never_registered_sync")
        def work():
            return "ok"

        with pytest.raises(BulkheadNotFoundError):
            work()

    @pytest.mark.asyncio
    async def test_async_unregistered_domain_raises_not_found(self):
        """An async call on the same never-registered domain raises the same error."""

        @bulkhead("never_registered_async")
        async def work():
            return "ok"

        with pytest.raises(BulkheadNotFoundError):
            await work()

    def test_sync_unregistered_domain_catchable_as_key_error(self):
        """The raised error is also catchable as KeyError (multi-inheritance)."""

        @bulkhead("never_registered_ke")
        def work():
            return "ok"

        with pytest.raises(KeyError):
            work()

    def test_sync_admits_after_get_or_create_provisioning(self):
        """After get_or_create() provisioning, a sync call is admitted."""
        get_bulkhead_registry().get_or_create("provisioned_sync", max_concurrent=5)

        @bulkhead("provisioned_sync")
        def work():
            return "admitted"

        assert work() == "admitted"

    @pytest.mark.asyncio
    async def test_async_admits_after_get_or_create_provisioning(self):
        """After get_or_create() provisioning, an async call is admitted."""
        get_bulkhead_registry().get_or_create("provisioned_async_dec", max_concurrent=5)

        @bulkhead("provisioned_async_dec")
        async def work():
            return "admitted"

        assert await work() == "admitted"

    def test_sync_admits_after_register_provisioning(self):
        """After register() provisioning, a sync call is admitted."""
        get_bulkhead_registry().register(
            SemaphoreBulkhead("registered_sync", max_concurrent=5)
        )

        @bulkhead("registered_sync")
        def work():
            return "admitted"

        assert work() == "admitted"

    def test_fallback_not_invoked_on_unregistered_domain(self):
        """An unregistered domain raises before fallback — fallback is NOT invoked.

        Resolution (registry.get) precedes policy composition, and fallback fires
        only on BulkheadFullError; this pins that the strict not-found contract is
        never silently absorbed by a fallback.
        """
        fallback = MagicMock(spec=lambda: None, return_value="fallback_value")

        @bulkhead("never_registered_fb", fallback=fallback)
        def work():
            return "primary"

        with pytest.raises(BulkheadNotFoundError):
            work()

        fallback.assert_not_called()


class TestBulkheadDecoratorDocstringExamples:
    """The decorator docstring usage examples run as rewritten (D6/D7)."""

    def test_docstring_example_module_custom_domain_provision_first(self):
        """Module docstring example: provision custom_domain, then decorate."""
        get_bulkhead_registry().get_or_create("custom_domain", max_concurrent=10)

        @bulkhead("custom_domain", timeout=5.0)
        def custom_operation():
            pass

        # Runs without an unhandled BulkheadNotFoundError.
        assert custom_operation() is None

    def test_docstring_example_function_fallback_provision_first(self):
        """Function docstring example: provision reports, decorate with fallback."""
        get_bulkhead_registry().get_or_create("reports", max_concurrent=5)

        def fetch_data():
            return {"status": "ok"}

        @bulkhead("reports", fallback=lambda: {"status": "unavailable"})
        def get_data():
            return fetch_data()

        assert get_data() == {"status": "ok"}

    def test_docstring_example_builtin_external_api_needs_no_provisioning(self):
        """Function docstring example: the built-in external_api needs no provisioning."""

        @bulkhead("external_api", timeout=5.0)
        def call_external_api():
            return "called"

        assert call_external_api() == "called"


class TestBulkheadDecoratorFallback:
    """Fallback function tests."""

    def test_sync_fallback_called_when_full(self):
        """A sync fallback is called when the bulkhead is full."""
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        # Occupy every slot of the bulkhead.
        max_concurrent = db_bulkhead.get_state().max_concurrent
        for _ in range(max_concurrent):
            db_bulkhead.try_acquire()

        def fallback_fn():
            return "fallback_result"

        @bulkhead(ConnectionType.DATABASE, fallback=fallback_fn)
        def primary_work():
            return "primary_result"

        result = primary_work()
        assert result == "fallback_result"

        # Cleanup.
        for _ in range(max_concurrent):
            db_bulkhead.release()

    def test_sync_fallback_with_args(self):
        """A sync fallback that takes args."""
        registry = get_bulkhead_registry()
        db_bulkhead = registry.get(ConnectionType.DATABASE)

        max_concurrent = db_bulkhead.get_state().max_concurrent
        for _ in range(max_concurrent):
            db_bulkhead.try_acquire()

        def fallback_fn(x: int) -> int:
            return x * 2  # Different logic.

        @bulkhead(ConnectionType.DATABASE, fallback=fallback_fn)
        def compute(x: int) -> int:
            return x * 10  # Original logic.

        result = compute(5)
        assert result == 10  # fallback: 5 * 2

        # Cleanup.
        for _ in range(max_concurrent):
            db_bulkhead.release()

    @pytest.mark.asyncio
    async def test_async_fallback_called_when_full(self):
        """An async fallback is called when the bulkhead is full."""
        registry = get_bulkhead_registry()

        # Occupy the async bulkhead.
        async_bh = registry.get_async(ConnectionType.DATABASE)
        max_concurrent = async_bh.get_state().max_concurrent
        for _ in range(max_concurrent):
            await async_bh.try_acquire()

        async def fallback_fn():
            return "async_fallback"

        @bulkhead(ConnectionType.DATABASE, fallback=fallback_fn)
        async def async_primary():
            return "async_primary"

        result = await async_primary()
        assert result == "async_fallback"

        # Cleanup.
        for _ in range(max_concurrent):
            await async_bh.release()


class TestBulkheadForDatabaseDecorator:
    """bulkhead_for_database decorator tests."""

    def test_bulkhead_for_database_default(self):
        """Default DB alias decorator."""

        @bulkhead_for_database("default")
        def db_write():
            return "written"

        result = db_write()
        assert result == "written"

    def test_bulkhead_for_database_replica(self):
        """Replica DB alias decorator."""

        @bulkhead_for_database("replica")
        def db_read():
            return "read"

        result = db_read()
        assert result == "read"

    @pytest.mark.asyncio
    async def test_bulkhead_for_database_async(self):
        """Async DB decorator."""

        @bulkhead_for_database("default")
        async def async_db_write():
            await asyncio.sleep(0.01)
            return "async_written"

        result = await async_db_write()
        assert result == "async_written"


class TestBulkheadForCacheDecorator:
    """bulkhead_for_cache decorator tests."""

    def test_bulkhead_for_cache_default(self):
        """Default cache instance decorator."""

        @bulkhead_for_cache("default")
        def cache_get(key: str):
            return f"value_for_{key}"

        result = cache_get("my_key")
        assert result == "value_for_my_key"

    def test_bulkhead_for_cache_session(self):
        """Session cache instance decorator."""

        @bulkhead_for_cache("session")
        def session_get(session_id: str):
            return {"session_id": session_id}

        result = session_get("abc123")
        assert result == {"session_id": "abc123"}

    @pytest.mark.asyncio
    async def test_bulkhead_for_cache_async(self):
        """Async cache decorator."""

        @bulkhead_for_cache("default")
        async def async_cache_get(key: str):
            await asyncio.sleep(0.01)
            return f"async_value_{key}"

        result = await async_cache_get("test")
        assert result == "async_value_test"


class TestBulkheadDecoratorTimeout:
    """Timeout option tests."""

    def test_timeout_applied(self):
        """The timeout option is applied."""

        # Confirm normal operation even with timeout set.
        @bulkhead(ConnectionType.DATABASE, timeout=5.0)
        def quick_work():
            return "done"

        result = quick_work()
        assert result == "done"

    @pytest.mark.asyncio
    async def test_async_timeout_applied(self):
        """The async timeout option is applied."""

        @bulkhead(ConnectionType.DATABASE, timeout=5.0)
        async def async_quick_work():
            await asyncio.sleep(0.01)
            return "async_done"

        result = await async_quick_work()
        assert result == "async_done"


class TestBulkheadDecoratorPreservesFunctionMetadata:
    """Function metadata preservation tests."""

    def test_preserves_function_name(self):
        """The function name is preserved."""

        @bulkhead(ConnectionType.DATABASE)
        def my_named_function():
            """This is my docstring."""
            return "result"

        assert my_named_function.__name__ == "my_named_function"
        assert "docstring" in my_named_function.__doc__

    @pytest.mark.asyncio
    async def test_preserves_async_function_name(self):
        """The async function name is preserved."""

        @bulkhead(ConnectionType.DATABASE)
        async def my_async_function():
            """Async docstring."""
            return "result"

        assert my_async_function.__name__ == "my_async_function"
        assert "Async" in my_async_function.__doc__
