"""
BulkheadRegistry unit tests.

Verifies registry behavior:
- Automatic registration of default bulkheads
- ConnectionType-based lookup
- Custom domain support
- Per-DB-alias / per-cache-instance bulkheads
- Strict not-found contract (BulkheadNotFoundError) on get()/get_async()
- async-twin invalidation on register()/unregister()
- built-in mutation guard (unregister blocked, overwrite warned)
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from baldur.core.connection_health import ConnectionType
from baldur.services.bulkhead.base import BulkheadType
from baldur.services.bulkhead.exceptions import (
    BulkheadError,
    BulkheadNotFoundError,
)
from baldur.services.bulkhead.registry import (
    BulkheadRegistry,
    get_bulkhead_registry,
    reset_bulkhead_registry,
)
from baldur.services.bulkhead.semaphore import SemaphoreBulkhead
from baldur.settings.bulkhead import BulkheadSettings, reset_bulkhead_settings


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before and after each test."""
    reset_bulkhead_registry()
    reset_bulkhead_settings()
    yield
    reset_bulkhead_registry()
    reset_bulkhead_settings()


class TestBulkheadRegistryDefaultBulkheads:
    """Default bulkhead auto-registration tests."""

    def test_database_bulkhead_registered(self):
        """DATABASE bulkhead is auto-registered."""
        registry = BulkheadRegistry()

        bulkhead = registry.get(ConnectionType.DATABASE)
        assert bulkhead.name == "database"
        assert isinstance(bulkhead, SemaphoreBulkhead)

    def test_cache_bulkhead_registered(self):
        """CACHE bulkhead is auto-registered."""
        registry = BulkheadRegistry()

        bulkhead = registry.get(ConnectionType.CACHE)
        assert bulkhead.name == "cache"
        assert isinstance(bulkhead, SemaphoreBulkhead)

    def test_external_api_bulkhead_registered(self):
        """EXTERNAL_API is auto-registered as the semaphore fallback.

        The base registry has no worker-pool implementation, so its
        thread-pool builder seam maps the request to semaphore isolation
        with capacity = external_api_max_workers (conservative bound).
        """
        registry = BulkheadRegistry()

        bulkhead = registry.get(ConnectionType.EXTERNAL_API)
        assert bulkhead.name == "external_api"
        assert isinstance(bulkhead, SemaphoreBulkhead)
        # Capacity maps to max_workers only — queue_size is inert.
        assert bulkhead.get_state().max_concurrent == 5

    def test_message_queue_bulkhead_registered(self):
        """MESSAGE_QUEUE bulkhead is auto-registered."""
        registry = BulkheadRegistry()

        bulkhead = registry.get(ConnectionType.MESSAGE_QUEUE)
        assert bulkhead.name == "message_queue"
        assert isinstance(bulkhead, SemaphoreBulkhead)

    def test_list_names_includes_all_defaults(self):
        """list_names includes all default bulkheads."""
        registry = BulkheadRegistry()

        names = registry.list_names()
        assert "database" in names
        assert "cache" in names
        assert "external_api" in names
        assert "message_queue" in names


class TestBulkheadRegistryGet:
    """Bulkhead lookup tests."""

    def test_get_by_connection_type(self):
        """Lookup by ConnectionType."""
        registry = BulkheadRegistry()

        bulkhead = registry.get(ConnectionType.DATABASE)
        assert bulkhead.name == "database"

    def test_get_by_string_name(self):
        """Lookup by string name."""
        registry = BulkheadRegistry()

        bulkhead = registry.get("database")
        assert bulkhead.name == "database"

    def test_get_unknown_raises_key_error(self):
        """Unknown domain raises BulkheadNotFoundError (subclasses KeyError)."""
        registry = BulkheadRegistry()

        with pytest.raises(KeyError) as exc_info:
            registry.get("unknown_domain")

        # Tightened (D2): the typed class is raised, not a bare KeyError.
        assert isinstance(exc_info.value, BulkheadNotFoundError)
        assert "unknown_domain" in str(exc_info.value)

    def test_get_unknown_message_lists_registered_names(self):
        """Not-found message lists the registered compartment names."""
        registry = BulkheadRegistry()

        with pytest.raises(BulkheadNotFoundError) as exc_info:
            registry.get("unknown_domain")

        message = str(exc_info.value)
        for builtin in ("database", "cache", "external_api", "message_queue"):
            assert builtin in message


class TestBulkheadNotFoundError:
    """BulkheadNotFoundError construction and rendering contract."""

    def test_is_subclass_of_key_error_and_bulkhead_error(self):
        """The typed error multi-inherits KeyError and BulkheadError."""
        err = BulkheadNotFoundError("missing", ["a", "b"])
        assert isinstance(err, KeyError)
        assert isinstance(err, BulkheadError)

    def test_str_renders_without_repr_quoting(self):
        """__str__ renders the message plainly (no KeyError repr-quoting)."""
        err = BulkheadNotFoundError("missing", ["a"])
        # KeyError.__str__ would wrap the message in repr quotes ("'...'");
        # the override returns args[0] directly.
        assert str(err).startswith("Bulkhead not found:")
        assert str(err) == err.args[0]

    def test_message_contains_requested_and_registered_names(self):
        """Message names the missing domain and the registered compartments."""
        err = BulkheadNotFoundError("missing", ["beta", "alpha"])
        rendered = str(err)
        assert "missing" in rendered
        assert "alpha" in rendered
        assert "beta" in rendered

    def test_empty_registered_list_renders_none_placeholder(self):
        """An empty registered list renders as the '(none)' placeholder."""
        err = BulkheadNotFoundError("missing", [])
        assert "(none)" in str(err)

    def test_extra_context_returns_name_and_registered(self):
        """extra_context() exposes the requested name and registered names."""
        err = BulkheadNotFoundError("missing", ["a", "b"])
        ctx = err.extra_context()
        assert ctx["bulkhead_name"] == "missing"
        assert ctx["registered_names"] == ["a", "b"]


class TestBulkheadExports:
    """BulkheadNotFoundError public-export surface (D2)."""

    def test_importable_from_package_root(self):
        """BulkheadNotFoundError is importable from baldur.services.bulkhead."""
        from baldur.services.bulkhead import (
            BulkheadNotFoundError as Exported,
        )

        assert Exported is BulkheadNotFoundError

    def test_listed_in_package_all(self):
        """BulkheadNotFoundError is present in the package __all__."""
        import baldur.services.bulkhead as pkg

        assert "BulkheadNotFoundError" in pkg.__all__


class TestBulkheadRegistryGetOrCreate:
    """Lookup-or-create tests."""

    def test_get_or_create_existing(self):
        """Existing bulkhead is returned."""
        registry = BulkheadRegistry()

        bulkhead1 = registry.get_or_create("database")
        bulkhead2 = registry.get_or_create("database")

        assert bulkhead1 is bulkhead2

    def test_get_or_create_new_semaphore(self):
        """New semaphore bulkhead is created."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_or_create(
            "custom_domain",
            max_concurrent=15,
            bulkhead_type="semaphore",
        )

        assert bulkhead.name == "custom_domain"
        assert isinstance(bulkhead, SemaphoreBulkhead)
        state = bulkhead.get_state()
        assert state.max_concurrent == 15

    def test_get_or_create_thread_pool_falls_back_to_semaphore(self):
        """A thread-pool request on the base registry builds the semaphore fallback."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_or_create(
            "custom_pool",
            max_concurrent=8,
            bulkhead_type="thread_pool",
        )

        assert bulkhead.name == "custom_pool"
        assert isinstance(bulkhead, SemaphoreBulkhead)
        state = bulkhead.get_state()
        assert state.max_concurrent == 8


class TestThreadPoolFallbackWarningBehavior:
    """thread_pool_unavailable WARNING on the base builder seam (D2).

    The base registry has no worker-pool implementation, so a thread-pool
    request falls back to semaphore isolation and warns once per name at
    compartment creation — stating the fallback semantics explicitly.
    """

    def test_builtin_external_api_construction_warns_once(self):
        """Registry construction warns once for the EXTERNAL_API thread-pool request."""
        with capture_logs() as logs:
            BulkheadRegistry()

        warns = [
            e
            for e in logs
            if e.get("event") == "bulkhead_registry.thread_pool_unavailable"
        ]
        assert len(warns) == 1
        assert warns[0]["log_level"] == "warning"
        assert warns[0]["bulkhead_name"] == ConnectionType.EXTERNAL_API.value

    def test_get_or_create_thread_pool_warns_once_per_name(self):
        """The WARNING fires at creation only — the cached lookup stays silent."""
        registry = BulkheadRegistry()

        with capture_logs() as logs:
            registry.get_or_create(
                "pool_a", max_concurrent=4, bulkhead_type="thread_pool"
            )
            registry.get_or_create(
                "pool_a", max_concurrent=4, bulkhead_type="thread_pool"
            )

        warns = [
            e
            for e in logs
            if e.get("event") == "bulkhead_registry.thread_pool_unavailable"
        ]
        assert len(warns) == 1
        assert warns[0]["bulkhead_name"] == "pool_a"

    def test_warning_payload_states_fallback_semantics(self):
        """The payload names the fallback and its admission-only timeout semantics."""
        registry = BulkheadRegistry()

        with capture_logs() as logs:
            registry.get_or_create(
                "pool_b", max_concurrent=6, bulkhead_type="thread_pool"
            )

        warn = next(
            e
            for e in logs
            if e.get("event") == "bulkhead_registry.thread_pool_unavailable"
        )
        assert warn["fallback"] == "semaphore"
        assert warn["max_concurrent"] == 6
        assert "no worker-pool offload" in warn["semantics"]
        assert "admission wait only" in warn["semantics"]


class TestBulkheadRegistryGetAsync:
    """Asynchronous bulkhead lookup tests."""

    def test_get_async_creates_async_bulkhead(self):
        """An async bulkhead is created for a registered (built-in) domain."""
        registry = BulkheadRegistry()

        async_bh = registry.get_async(ConnectionType.DATABASE)
        assert async_bh.name == "database"

    def test_get_async_cached(self):
        """The async bulkhead is cached across lookups."""
        registry = BulkheadRegistry()

        async_bh1 = registry.get_async("database")
        async_bh2 = registry.get_async("database")

        assert async_bh1 is async_bh2

    def test_get_async_unregistered_raises_not_found(self):
        """A domain with no sync twin raises BulkheadNotFoundError (D3 strict)."""
        registry = BulkheadRegistry()

        with pytest.raises(BulkheadNotFoundError):
            registry.get_async("never_registered")

    def test_get_async_unregistered_catchable_as_key_error(self):
        """The strict get_async miss is also catchable as KeyError."""
        registry = BulkheadRegistry()

        with pytest.raises(KeyError):
            registry.get_async("never_registered")

    def test_get_async_after_provisioning_derives_capacity(self):
        """After provisioning, the async twin derives capacity from the sync twin."""
        registry = BulkheadRegistry()

        # 7 differs from the registry default (default_max_concurrent=10), proving
        # the capacity is derived from the sync twin, not a blind default mint.
        registry.get_or_create("provisioned_async", max_concurrent=7)
        async_bh = registry.get_async("provisioned_async")

        assert async_bh.get_state().max_concurrent == 7


class TestBulkheadRegistryGetForDatabase:
    """Per-DB-alias bulkhead tests."""

    def test_get_for_database_default(self):
        """Default DB alias bulkhead."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_for_database("default")
        assert bulkhead.name == "database:default"

    def test_get_for_database_replica(self):
        """Replica DB alias bulkhead."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_for_database("replica")
        assert bulkhead.name == "database:replica"

    def test_database_bulkheads_are_separate(self):
        """Each DB alias bulkhead is independent."""
        registry = BulkheadRegistry()

        default_bh = registry.get_for_database("default")
        replica_bh = registry.get_for_database("replica")

        assert default_bh is not replica_bh
        assert default_bh.name != replica_bh.name


class TestBulkheadRegistryGetForCache:
    """Per-cache-instance bulkhead tests."""

    def test_get_for_cache_default(self):
        """Default cache instance bulkhead."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_for_cache("default")
        assert bulkhead.name == "cache:default"

    def test_get_for_cache_session(self):
        """Session cache instance bulkhead."""
        registry = BulkheadRegistry()

        bulkhead = registry.get_for_cache("session")
        assert bulkhead.name == "cache:session"


class TestBulkheadRegistryRegisterUnregister:
    """Register/unregister tests, async-twin invalidation, and built-in guard."""

    def test_register_custom_bulkhead(self):
        """A custom bulkhead is registered."""
        registry = BulkheadRegistry()
        custom_bh = SemaphoreBulkhead("my_custom", max_concurrent=25)

        registry.register(custom_bh)

        retrieved = registry.get("my_custom")
        assert retrieved is custom_bh

    def test_unregister_bulkhead(self):
        """A custom bulkhead is unregistered."""
        registry = BulkheadRegistry()
        custom_bh = SemaphoreBulkhead("to_remove", max_concurrent=5)
        registry.register(custom_bh)

        result = registry.unregister("to_remove")
        assert result is True

        with pytest.raises(KeyError):
            registry.get("to_remove")

    def test_unregister_nonexistent_returns_false(self):
        """Unregistering a non-existent custom name returns False."""
        registry = BulkheadRegistry()

        result = registry.unregister("nonexistent")
        assert result is False

    def test_register_invalidates_async_twin(self):
        """Re-registering with new capacity invalidates the stale async twin (D4)."""
        # Given — a provisioned custom domain with an async twin at capacity 3
        registry = BulkheadRegistry()
        registry.get_or_create("inv_register", max_concurrent=3)
        async_before = registry.get_async("inv_register")
        assert async_before.get_state().max_concurrent == 3

        # When — re-register the same name with a new capacity
        registry.register(SemaphoreBulkhead("inv_register", max_concurrent=8))

        # Then — the next async lookup reflects the new capacity (fresh twin)
        async_after = registry.get_async("inv_register")
        assert async_after.get_state().max_concurrent == 8
        assert async_after is not async_before

    def test_unregister_invalidates_async_twin(self):
        """Unregistering a custom domain pops its async twin (D4)."""
        # Given — a provisioned custom domain with an async twin
        registry = BulkheadRegistry()
        registry.get_or_create("inv_unregister", max_concurrent=4)
        registry.get_async("inv_unregister")
        assert "inv_unregister" in registry._async_bulkheads

        # When — unregister the domain
        result = registry.unregister("inv_unregister")

        # Then — both the sync entry and the async twin are gone
        assert result is True
        assert "inv_unregister" not in registry._async_bulkheads

    @pytest.mark.parametrize(
        "conn_type",
        list(ConnectionType),
        ids=[ct.value for ct in ConnectionType],
    )
    def test_unregister_builtin_raises_value_error(self, conn_type):
        """Unregistering any built-in compartment raises ValueError (D8)."""
        registry = BulkheadRegistry()

        with pytest.raises(ValueError, match="built-in"):
            registry.unregister(conn_type.value)

        # The compartment stays intact.
        assert registry.get(conn_type.value).name == conn_type.value

    def test_register_builtin_warns_and_overwrites_and_pops_twin(self):
        """Overwriting a built-in name lands, warns, and pops the async twin (D8)."""
        # Given — a built-in with a materialized async twin
        registry = BulkheadRegistry()
        registry.get_async("database")
        assert "database" in registry._async_bulkheads
        replacement = SemaphoreBulkhead("database", max_concurrent=99)

        # When — register over the built-in name
        with capture_logs() as logs:
            registry.register(replacement)

        # Then — the overwrite lands, the async twin is popped, a WARNING is emitted
        assert registry.get("database") is replacement
        assert "database" not in registry._async_bulkheads
        warns = [
            e for e in logs if e.get("event") == "bulkhead_registry.builtin_overwritten"
        ]
        assert len(warns) == 1
        assert warns[0]["bulkhead_name"] == "database"
        assert warns[0]["log_level"] == "warning"


class TestBulkheadRegistryGetAllStates:
    """Aggregate state tests."""

    def test_get_all_states(self):
        """All bulkhead states are returned."""
        registry = BulkheadRegistry()

        states = registry.get_all_states()

        assert "database" in states
        assert "cache" in states
        assert "external_api" in states
        assert "message_queue" in states

        db_state = states["database"]
        assert db_state.bulkhead_type == BulkheadType.SEMAPHORE


class TestBulkheadRegistrySingleton:
    """Singleton tests (resolution chain, fallback leg).

    The getter is the resolution chain: a populated provider slot wins.
    These tests pin the fallback-leg semantics, so the slot is forced
    empty via the documented mock point (ProviderRegistry.bulkhead_registry).
    """

    @pytest.fixture(autouse=True)
    def _empty_provider_slot(self, monkeypatch):
        """Force the chain onto its fallback leg (slot resolves to None)."""
        from baldur.factory.registry import ProviderRegistry

        monkeypatch.setattr(
            ProviderRegistry.bulkhead_registry, "safe_get", lambda name=None: None
        )

    def test_get_bulkhead_registry_returns_same_instance(self):
        """The singleton instance is returned."""
        registry1 = get_bulkhead_registry()
        registry2 = get_bulkhead_registry()

        assert registry1 is registry2

    def test_reset_clears_singleton(self):
        """reset clears the singleton."""
        registry1 = get_bulkhead_registry()
        reset_bulkhead_registry()
        registry2 = get_bulkhead_registry()

        assert registry1 is not registry2

    def test_slot_populated_wins_over_fallback(self, monkeypatch):
        """A populated provider slot is returned instead of the base singleton."""
        from baldur.factory.registry import ProviderRegistry

        sentinel = BulkheadRegistry()
        monkeypatch.setattr(
            ProviderRegistry.bulkhead_registry,
            "safe_get",
            lambda name=None: sentinel,
        )

        assert get_bulkhead_registry() is sentinel


class TestBulkheadRegistryWithCustomSettings:
    """Registry-with-custom-settings tests."""

    def test_custom_settings_applied(self):
        """Custom settings are applied."""
        settings = BulkheadSettings(
            database_max_concurrent=25,
            cache_max_concurrent=50,
        )
        registry = BulkheadRegistry(settings=settings)

        db_bh = registry.get(ConnectionType.DATABASE)
        cache_bh = registry.get(ConnectionType.CACHE)

        assert db_bh.get_state().max_concurrent == 25
        assert cache_bh.get_state().max_concurrent == 50
