"""
GenericProviderRegistry[T] unit tests.

Tests for src/baldur/factory/base.py.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from baldur.core.exceptions import AdapterNotFoundError
from baldur.factory.base import GenericProviderRegistry

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class DummyAdapter:
    """Minimal adapter stub for registry tests."""

    def __init__(self) -> None:
        self.initialized = True


class DummyAdapterWithHealthCheck:
    """Adapter stub that implements health_check()."""

    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def health_check(self) -> bool:
        return self._healthy


class DummyAdapterHealthCheckRaises:
    """Adapter stub whose health_check raises."""

    def health_check(self) -> bool:
        raise RuntimeError("health check failed")


class NonCallableProvider:
    """An already-instantiated object used as a non-callable provider."""

    pass


# We need a non-callable to test the non-callable branch.
# Instances of classes are technically callable (they have __call__ only if defined),
# but an *instance* is not callable unless __call__ is defined.
# So an instance of NonCallableProvider is non-callable.


# ---------------------------------------------------------------------------
# Contract tests
# ---------------------------------------------------------------------------


class TestGenericProviderRegistryContract:
    """Design contract values for GenericProviderRegistry."""

    def test_first_registered_provider_becomes_default(self):
        """First registered provider becomes default (D3 design)."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")

        registry.register("first", DummyAdapter)
        registry.register("second", DummyAdapter)

        assert registry.get_default_name() == "first"

    def test_override_uses_test_override_key_name(self):
        """override() uses '__test_override__' as the instance key."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")
        registry.register("real", DummyAdapter)
        mock_instance = DummyAdapter()

        with registry.override(mock_instance):
            assert registry.has_instance("__test_override__")
            assert registry.get_default_name() == "__test_override__"


# ---------------------------------------------------------------------------
# Behavior tests
# ---------------------------------------------------------------------------


class TestGenericProviderRegistryBehavior:
    """Behavioral tests for GenericProviderRegistry methods."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        """Fresh registry for each test."""
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    # -- register + get basic flow --

    def test_register_and_get_returns_instantiated_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """register() + get() creates and returns a provider instance."""
        registry.register("memory", DummyAdapter)

        instance = registry.get("memory")

        assert isinstance(instance, DummyAdapter)
        assert instance.initialized is True

    # -- error paths --

    def test_get_with_no_default_and_no_name_raises_adapter_not_found(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() with no default set and no name raises AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError):
            registry.get()

    def test_get_with_unknown_name_raises_adapter_not_found(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() with an unregistered name raises AdapterNotFoundError."""
        registry.register("known", DummyAdapter)

        with pytest.raises(AdapterNotFoundError):
            registry.get("unknown")

    def test_get_with_no_default_error_contains_adapter_type(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """AdapterNotFoundError includes adapter_type in extra_context."""
        with pytest.raises(AdapterNotFoundError) as exc_info:
            registry.get()

        ctx = exc_info.value.extra_context()
        assert ctx["adapter_type"] == "test"

    def test_get_with_unknown_name_error_contains_adapter_name(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """AdapterNotFoundError includes adapter_name in extra_context."""
        registry.register("known", DummyAdapter)

        with pytest.raises(AdapterNotFoundError) as exc_info:
            registry.get("unknown")

        ctx = exc_info.value.extra_context()
        assert ctx["adapter_name"] == "unknown"

    # -- singleton caching --

    def test_get_returns_cached_instance_on_second_call(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() caches the instance; second call returns the same object."""
        registry.register("memory", DummyAdapter)

        first = registry.get("memory")
        second = registry.get("memory")

        assert first is second

    # -- callable vs non-callable providers --

    def test_get_with_callable_provider_creates_instance(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() calls a callable provider to create an instance."""
        factory_fn = MagicMock(return_value=DummyAdapter())
        registry.register("factory", factory_fn)

        instance = registry.get("factory")

        factory_fn.assert_called_once()
        assert isinstance(instance, DummyAdapter)

    def test_get_with_non_callable_provider_returns_provider_directly(self):
        """get() returns a non-callable provider as-is."""
        registry = GenericProviderRegistry[NonCallableProvider](adapter_type="test")
        sentinel = NonCallableProvider()
        # Make it non-callable by storing the instance directly
        registry.register("direct", sentinel)  # type: ignore[arg-type]
        registry.set_default("direct")

        result = registry.get("direct")

        assert result is sentinel

    # -- auto_discover --

    def test_auto_discover_invoked_when_provider_not_found(self):
        """auto_discover callback is invoked when provider name is not registered."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )

        # auto_discover should register the provider when called
        def side_effect():
            registry.register("lazy", DummyAdapter)

        discover_fn.side_effect = side_effect

        instance = registry.get("lazy")

        discover_fn.assert_called_once()
        assert isinstance(instance, DummyAdapter)

    def test_auto_discover_not_invoked_when_provider_already_registered(self):
        """auto_discover is NOT called if the provider is already registered."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )
        registry.register("existing", DummyAdapter)

        registry.get("existing")

        discover_fn.assert_not_called()

    # -- set_default --

    def test_set_default_changes_which_provider_get_returns(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """set_default() changes which provider get() returns by default."""
        registry.register("first", DummyAdapter)
        registry.register("second", DummyAdapter)

        # Default is "first" (first registered)
        first_instance = registry.get()
        assert first_instance is registry.get("first")

        # Change default
        registry.set_default("second")
        second_instance = registry.get()
        assert second_instance is registry.get("second")

    # -- list_providers --

    def test_list_providers_returns_registered_names(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """list_providers() returns all registered provider names."""
        registry.register("alpha", DummyAdapter)
        registry.register("beta", DummyAdapter)
        registry.register("gamma", DummyAdapter)

        names = registry.list_providers()

        assert names == ["alpha", "beta", "gamma"]

    # -- clear_instances --

    def test_clear_instances_clears_cache_but_keeps_registrations(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """clear_instances() removes cached instances but keeps provider registrations."""
        registry.register("memory", DummyAdapter)
        first = registry.get("memory")

        registry.clear_instances()

        # Registration still there
        assert "memory" in registry.list_providers()
        # But instance is recreated
        second = registry.get("memory")
        assert first is not second

    # -- reset --

    def test_reset_clears_everything_including_registrations(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """reset() clears providers, instances, and default."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")

        registry.reset()

        assert registry.list_providers() == []
        assert registry.instance_count() == 0
        assert registry.get_default_name() is None

    # -- health_check --

    def test_health_check_returns_true_for_providers_without_health_check(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """health_check() returns True for providers that lack a health_check method."""
        registry.register("simple", DummyAdapter)
        registry.get("simple")

        results = registry.health_check()

        assert results["simple"] is True

    def test_health_check_calls_health_check_on_providers_that_have_it(self):
        """health_check() delegates to the provider's health_check() method."""
        registry = GenericProviderRegistry[DummyAdapterWithHealthCheck](
            adapter_type="test"
        )
        registry.register("healthy", DummyAdapterWithHealthCheck)
        registry.get("healthy")

        results = registry.health_check()

        assert results["healthy"] is True

    def test_health_check_returns_false_when_provider_health_check_raises(self):
        """health_check() returns False when a provider's health_check() raises."""
        registry = GenericProviderRegistry[DummyAdapterHealthCheckRaises](
            adapter_type="test"
        )
        registry.register("broken", DummyAdapterHealthCheckRaises)
        registry.get("broken")

        results = registry.health_check()

        assert results["broken"] is False

    def test_health_check_returns_empty_dict_when_no_instances(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """health_check() returns empty dict when no instances exist."""
        results = registry.health_check()
        assert results == {}

    # -- override context manager --

    def test_override_temporarily_replaces_default_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """override() makes get() return the mock instance."""
        registry.register("real", DummyAdapter)
        mock_instance = DummyAdapter()

        with registry.override(mock_instance):
            result = registry.get()
            assert result is mock_instance

    def test_override_restores_original_on_exit(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """override() restores original default and instances upon exit."""
        registry.register("real", DummyAdapter)
        original = registry.get("real")
        original_default = registry.get_default_name()

        with registry.override(DummyAdapter()):
            pass  # override active

        # After exit, original state is restored
        assert registry.get_default_name() == original_default
        assert registry.get("real") is original
        assert not registry.has_instance("__test_override__")

    def test_override_restores_on_exception(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """override() restores state even when an exception occurs inside."""
        registry.register("real", DummyAdapter)
        original_default = registry.get_default_name()

        with pytest.raises(ValueError, match="deliberate"):
            with registry.override(DummyAdapter()):
                raise ValueError("deliberate")

        assert registry.get_default_name() == original_default
        assert not registry.has_instance("__test_override__")

    # -- isolated_context --

    def test_isolated_context_creates_independent_registry(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """isolated_context() yields a new, independent registry."""
        registry.register("shared", DummyAdapter)

        with registry.isolated_context() as isolated:
            assert isolated is not registry
            assert isinstance(isolated, GenericProviderRegistry)

    def test_isolated_context_copies_registrations_but_not_instances(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """isolated_context() copies providers and default, but not cached instances."""
        registry.register("shared", DummyAdapter)
        original_instance = registry.get("shared")

        with registry.isolated_context() as isolated:
            # Registrations copied
            assert "shared" in isolated.list_providers()
            assert isolated.get_default_name() == registry.get_default_name()

            # Instances not copied — isolated creates its own
            assert isolated.instance_count() == 0
            isolated_instance = isolated.get("shared")
            assert isolated_instance is not original_instance

    def test_isolated_context_mutations_do_not_affect_original(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Mutations inside isolated_context do not affect the original registry."""
        registry.register("original", DummyAdapter)

        with registry.isolated_context() as isolated:
            isolated.register("extra", DummyAdapter)
            isolated.set_default("extra")

        # Original should be unchanged
        assert "extra" not in registry.list_providers()
        assert registry.get_default_name() == "original"

    # -- idempotency --

    def test_register_same_name_twice_overwrites_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Registering the same name twice overwrites the previous provider."""
        registry.register("mem", DummyAdapter)
        registry.register("mem", DummyAdapterWithHealthCheck)

        assert registry.get_provider("mem") is DummyAdapterWithHealthCheck

    def test_get_same_name_twice_returns_same_instance(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Getting the same provider twice returns the identical cached instance."""
        registry.register("mem", DummyAdapter)

        first = registry.get("mem")
        second = registry.get("mem")

        assert first is second

    # -- callable factory error propagation (Cat 4.6) --

    def test_get_propagates_type_error_from_factory(self):
        """get() propagates TypeError from a broken factory (Cat 4.6).

        Symmetric with create_new() — a factory that fails to instantiate must
        surface its error at registration/get time, not be silently treated as
        a pre-constructed instance.
        """

        def bad_factory():
            raise TypeError("cannot instantiate")

        registry = GenericProviderRegistry(adapter_type="test")
        registry.register("bad", bad_factory)

        with pytest.raises(TypeError, match="cannot instantiate"):
            registry.get("bad")


# ---------------------------------------------------------------------------
# Cat 4.6 — Bootstrap with broken provider factory
#
# Scenario plan: the perf-scenario plan, row 4.6 (MUST).
# Verification: factory must raise at registration/get time, not defer to
# first use. create_new() must not silently pass broken factory.
# Code ref: factory/base.py get() + create_new().
# ---------------------------------------------------------------------------


class TestBrokenProviderFactoryBehavior:
    """A factory that fails to instantiate must surface its error at get/create_new
    time rather than be cached as a "pre-constructed instance" the caller would
    only discover broken on first method invocation (Cat 4.6).
    """

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    # -- get() propagates instantiation errors --

    def test_get_propagates_import_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() propagates ImportError raised by the factory."""

        def broken_import_factory():
            raise ImportError("optional dep 'redis' missing")

        registry.register("broken", broken_import_factory)

        with pytest.raises(ImportError, match="optional dep 'redis' missing"):
            registry.get("broken")

    def test_get_propagates_connection_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() propagates ConnectionError raised by the factory."""

        def broken_connection_factory():
            raise ConnectionError("redis unreachable at bootstrap")

        registry.register("broken", broken_connection_factory)

        with pytest.raises(ConnectionError, match="redis unreachable"):
            registry.get("broken")

    def test_get_propagates_type_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() propagates TypeError raised by the factory.

        Pre-Cat-4.6, this was silently swallowed and the factory callable
        itself was cached as the "instance" — deferring the failure to the
        first method invocation.
        """

        def broken_type_factory():
            raise TypeError("missing 1 required positional argument: 'config'")

        registry.register("broken", broken_type_factory)

        with pytest.raises(TypeError, match="missing 1 required positional"):
            registry.get("broken")

    # -- create_new() propagates instantiation errors (symmetric with get) --

    def test_create_new_propagates_import_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() does not silently pass a factory that raises ImportError."""

        def broken_import_factory():
            raise ImportError("optional dep 'redis' missing")

        registry.register("broken", broken_import_factory)

        with pytest.raises(ImportError, match="optional dep 'redis' missing"):
            registry.create_new("broken")

    def test_create_new_propagates_connection_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() does not silently pass a factory that raises ConnectionError."""

        def broken_connection_factory():
            raise ConnectionError("redis unreachable at bootstrap")

        registry.register("broken", broken_connection_factory)

        with pytest.raises(ConnectionError, match="redis unreachable"):
            registry.create_new("broken")

    def test_create_new_propagates_type_error_from_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() propagates TypeError from a broken factory."""

        def broken_type_factory():
            raise TypeError("missing 1 required positional argument: 'config'")

        registry.register("broken", broken_type_factory)

        with pytest.raises(TypeError, match="missing 1 required positional"):
            registry.create_new("broken")

    # -- failure surfaces at get/create_new time, not deferred to first use --

    def test_get_failure_does_not_cache_broken_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """A failed get() must not pollute the instance cache.

        Otherwise a second get() would return the broken cached value (or
        skip the factory call entirely), masking the bootstrap failure.
        """

        def broken_factory():
            raise ImportError("redis missing")

        registry.register("broken", broken_factory)

        with pytest.raises(ImportError):
            registry.get("broken")

        # No silent caching — the failure surface must remain visible to a retry.
        assert registry.has_instance("broken") is False
        assert registry.instance_count() == 0

    def test_get_retries_factory_after_failure_when_dependency_recovers(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """After a transient failure, a subsequent get() re-invokes the factory.

        Verifies that broken-factory failures do not poison the registry: once
        the underlying dependency recovers, the next get() succeeds.
        """
        call_log: list[int] = []

        def flaky_factory():
            call_log.append(1)
            if len(call_log) == 1:
                raise ConnectionError("redis warming up")
            return DummyAdapter()

        registry.register("flaky", flaky_factory)

        with pytest.raises(ConnectionError):
            registry.get("flaky")

        # Second call: dependency recovered → factory re-invoked → success.
        instance = registry.get("flaky")

        assert isinstance(instance, DummyAdapter)
        assert len(call_log) == 2

    def test_get_returns_real_instance_not_factory_callable(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """When the factory succeeds, get() returns the constructed instance — never the factory itself.

        Negative control for the pre-Cat-4.6 bug: a working factory must not
        be confused with a "pre-constructed callable instance".
        """

        def good_factory() -> DummyAdapter:
            return DummyAdapter()

        registry.register("good", good_factory)

        instance = registry.get("good")

        assert isinstance(instance, DummyAdapter)
        assert instance is not good_factory
        assert instance.initialized is True

    def test_get_create_new_symmetric_on_broken_factory(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get() and create_new() raise the same exception type for a broken factory.

        Pre-Cat-4.6, get() silently passed on TypeError while create_new()
        propagated. This asymmetry is removed.
        """

        def broken_factory():
            raise TypeError("bootstrap failed")

        registry.register("broken", broken_factory)

        with pytest.raises(TypeError, match="bootstrap failed"):
            registry.get("broken")

        # Reset cache to allow a clean create_new path (cache is empty after
        # failed get, but be explicit so this test doesn't depend on the
        # no-cache-on-failure invariant).
        registry.clear_instances()

        with pytest.raises(TypeError, match="bootstrap failed"):
            registry.create_new("broken")


class TestGenericProviderRegistryThreadSafety:
    """Thread-safety tests for GenericProviderRegistry."""

    def test_concurrent_get_returns_same_instance(self):
        """10 threads calling get() concurrently all receive the same instance."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")
        registry.register("shared", DummyAdapter)

        results: list[DummyAdapter] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def worker():
            try:
                barrier.wait()
                instance = registry.get("shared")
                results.append(instance)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(results) == 10
        assert all(r is results[0] for r in results)

    def test_concurrent_register_and_get_no_crash(self):
        """Concurrent register and get calls do not crash the registry."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")
        errors: list[Exception] = []

        def registerer(idx: int):
            try:
                registry.register(f"provider_{idx}", DummyAdapter)
            except Exception as e:
                errors.append(e)

        def getter(idx: int):
            try:
                # May or may not find it depending on timing
                registry.get(f"provider_{idx}")
            except (AdapterNotFoundError, TypeError):
                pass  # Expected if not yet registered
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=registerer, args=(i,)))
            threads.append(threading.Thread(target=getter, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors in concurrent register/get: {errors}"


# ---------------------------------------------------------------------------
# Same-slot re-entrancy guard
#
# get() invokes the provider factory while holding the non-reentrant
# registry lock. A factory that (transitively) resolves an uncached name in
# its own slot would deadlock — the guard converts that hang into an
# immediate RuntimeError.
# ---------------------------------------------------------------------------


class TestReentrantResolutionGuardBehavior:
    """A provider factory resolving its own slot raises instead of deadlocking."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="reentrancy")

    def test_factory_resolving_own_name_raises_runtime_error(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """A factory that re-enters get() for its own name raises, not hangs."""

        def self_resolving_factory():
            return registry.get("selfref")

        registry.register("selfref", self_resolving_factory)

        with pytest.raises(RuntimeError, match="Re-entrant provider resolution"):
            registry.get("selfref")

    def test_factory_resolving_other_uncached_name_same_slot_raises(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Transitive same-slot resolution of an uncached name raises.

        Same lock, different name — without the guard this is the same
        deadlock as direct self-resolution.
        """
        registry.register("dep", DummyAdapter)

        def transitive_factory():
            registry.get("dep")  # uncached → would need self._lock → deadlock
            return DummyAdapter()

        registry.register("outer", transitive_factory)

        with pytest.raises(RuntimeError, match="Re-entrant provider resolution"):
            registry.get("outer")

    def test_error_message_names_adapter_type_and_requested_name(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """The error is diagnosable: it names the slot and the re-entrant name."""

        def self_resolving_factory():
            return registry.get("selfref")

        registry.register("selfref", self_resolving_factory)

        with pytest.raises(RuntimeError, match="'reentrancy'.*'selfref'"):
            registry.get("selfref")

    def test_factory_may_read_cached_instance_in_same_slot(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """A factory reading an already-cached same-slot instance is legal.

        The cached fast path is lock-free, so it neither deadlocks nor
        trips the guard — pins that the guard does not overreach.
        """
        registry.register("dep", DummyAdapter)
        dep = registry.get("dep")  # pre-cache

        seen: list[DummyAdapter] = []

        def composing_factory():
            seen.append(registry.get("dep"))  # cached → fast path
            return DummyAdapter()

        registry.register("outer", composing_factory)

        instance = registry.get("outer")

        assert isinstance(instance, DummyAdapter)
        assert seen == [dep]

    def test_cross_slot_resolution_from_factory_allowed(self):
        """A factory resolving a DIFFERENT slot is legal (per-slot guard/lock)."""
        slot_a = GenericProviderRegistry[DummyAdapter](adapter_type="slot_a")
        slot_b = GenericProviderRegistry[DummyAdapter](adapter_type="slot_b")
        slot_b.register("dep", DummyAdapter)

        def cross_slot_factory():
            slot_b.get("dep")  # different registry → different lock
            return DummyAdapter()

        slot_a.register("outer", cross_slot_factory)

        assert isinstance(slot_a.get("outer"), DummyAdapter)

    def test_guard_flag_cleared_after_failure(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """After the guard fires, subsequent legal resolution works.

        The in-progress flag must be cleared on the failure path (finally),
        or every later get() on this thread would false-positive.
        """

        def self_resolving_factory():
            return registry.get("selfref")

        registry.register("selfref", self_resolving_factory)
        registry.register("good", DummyAdapter)

        with pytest.raises(RuntimeError):
            registry.get("selfref")

        # Failed name is not cached; a legal provider still resolves.
        assert registry.has_instance("selfref") is False
        assert isinstance(registry.get("good"), DummyAdapter)

    def test_guard_is_thread_local_concurrent_getter_unaffected(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """A second thread resolving during construction waits normally.

        The flag is per-thread: while thread 1's factory is mid-construction
        (flag set on thread 1), thread 2's get() must NOT trip the guard —
        it blocks on the lock and receives the same cached instance.
        """
        factory_entered = threading.Event()
        release_factory = threading.Event()

        def slow_factory():
            factory_entered.set()
            assert release_factory.wait(timeout=5), "release signal missed"
            return DummyAdapter()

        registry.register("slow", slow_factory)

        results: list[DummyAdapter] = []
        errors: list[Exception] = []

        def t1():
            try:
                results.append(registry.get("slow"))
            except Exception as e:
                errors.append(e)

        def t2():
            try:
                factory_entered.wait(timeout=5)
                results.append(registry.get("slow"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=t1), threading.Thread(target=t2)]
        for t in threads:
            t.start()
        assert factory_entered.wait(timeout=5)
        release_factory.set()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        assert len(results) == 2
        assert results[0] is results[1]


# ---------------------------------------------------------------------------
# has_provider
# ---------------------------------------------------------------------------


class TestHasProviderBehavior:
    """has_provider() returns whether a provider name is registered."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_returns_true_for_registered_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_provider() returns True after register()."""
        registry.register("memory", DummyAdapter)

        assert registry.has_provider("memory") is True

    def test_returns_false_for_unregistered_name(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_provider() returns False for a name never registered."""
        assert registry.has_provider("unknown") is False

    def test_returns_true_after_overwrite(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_provider() returns True even after provider is overwritten."""
        registry.register("mem", DummyAdapter)
        registry.register("mem", DummyAdapterWithHealthCheck)

        assert registry.has_provider("mem") is True

    def test_returns_false_after_reset(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_provider() returns False after reset() clears all providers."""
        registry.register("memory", DummyAdapter)
        registry.reset()

        assert registry.has_provider("memory") is False


# ---------------------------------------------------------------------------
# invalidate_instance
# ---------------------------------------------------------------------------


class TestInvalidateInstanceBehavior:
    """invalidate_instance() removes a cached instance forcing fresh creation."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_next_get_returns_different_object(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """After invalidate_instance(), get() creates a new instance."""
        registry.register("memory", DummyAdapter)
        first = registry.get("memory")

        registry.invalidate_instance("memory")
        second = registry.get("memory")

        assert first is not second

    def test_provider_registration_preserved(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """invalidate_instance() does not remove the provider registration."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")

        registry.invalidate_instance("memory")

        assert registry.has_provider("memory") is True

    def test_noop_for_unknown_name(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """invalidate_instance() for an unknown name does not raise."""
        # Should not raise
        registry.invalidate_instance("nonexistent")

    def test_idempotent_double_invalidate(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Calling invalidate_instance() twice does not raise."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")

        registry.invalidate_instance("memory")
        registry.invalidate_instance("memory")

        # Still works after double invalidate
        instance = registry.get("memory")
        assert isinstance(instance, DummyAdapter)


# ---------------------------------------------------------------------------
# create_new
# ---------------------------------------------------------------------------


class TestCreateNewBehavior:
    """create_new() returns a fresh, uncached provider instance."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_returns_new_instance_each_call(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() returns a different object on each call."""
        registry.register("memory", DummyAdapter)

        first = registry.create_new("memory")
        second = registry.create_new("memory")

        assert first is not second
        assert isinstance(first, DummyAdapter)
        assert isinstance(second, DummyAdapter)

    def test_does_not_pollute_instance_cache(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() does not store the instance in the cache."""
        registry.register("memory", DummyAdapter)

        created = registry.create_new("memory")

        # Cache is still empty — get() creates its own cached instance
        cached = registry.get("memory")
        assert created is not cached

    def test_raises_adapter_not_found_for_unknown_name(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() raises AdapterNotFoundError for unregistered name."""
        with pytest.raises(AdapterNotFoundError):
            registry.create_new("unknown")

    def test_invokes_auto_discover_when_provider_missing(self):
        """create_new() triggers auto_discover if provider is not registered."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )

        def side_effect():
            registry.register("lazy", DummyAdapter)

        discover_fn.side_effect = side_effect

        instance = registry.create_new("lazy")

        discover_fn.assert_called_once()
        assert isinstance(instance, DummyAdapter)

    def test_raises_after_auto_discover_fails_to_register(self):
        """create_new() raises if auto_discover doesn't register the name."""
        discover_fn = MagicMock()  # Does nothing — doesn't register
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )

        with pytest.raises(AdapterNotFoundError):
            registry.create_new("missing")

    def test_calls_factory_function_each_time(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new() calls the factory function on every invocation."""
        factory_fn = MagicMock(side_effect=[DummyAdapter(), DummyAdapter()])
        registry.register("factory", factory_fn)

        registry.create_new("factory")
        registry.create_new("factory")

        assert factory_fn.call_count == 2

    def test_non_callable_provider_returned_as_is(self):
        """create_new() returns non-callable provider directly."""
        registry = GenericProviderRegistry[NonCallableProvider](adapter_type="test")
        sentinel = NonCallableProvider()
        registry.register("direct", sentinel)  # type: ignore[arg-type]

        result = registry.create_new("direct")

        assert result is sentinel

    def test_create_new_none_uses_default_provider(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """create_new(None) resolves to the default provider."""
        registry.register("memory", DummyAdapter)

        instance = registry.create_new(None)

        assert isinstance(instance, DummyAdapter)

    def test_create_new_none_with_no_default_raises(self):
        """create_new(None) raises when no default is set."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")

        with pytest.raises(AdapterNotFoundError):
            registry.create_new(None)

    def test_create_new_none_triggers_auto_discover(self):
        """create_new(None) triggers auto_discover to resolve the default."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )

        def side_effect():
            registry.register("lazy_default", DummyAdapter)

        discover_fn.side_effect = side_effect

        instance = registry.create_new(None)

        discover_fn.assert_called_once()
        assert isinstance(instance, DummyAdapter)


# ---------------------------------------------------------------------------
# get_provider
# ---------------------------------------------------------------------------


class TestGetProviderBehavior:
    """get_provider() returns the raw provider class/factory without instantiation."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_returns_registered_class(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_provider() returns the provider class itself."""
        registry.register("memory", DummyAdapter)

        result = registry.get_provider("memory")

        assert result is DummyAdapter

    def test_returns_factory_function(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_provider() returns a factory function without calling it."""
        factory_fn = MagicMock(return_value=DummyAdapter())
        registry.register("factory", factory_fn)

        result = registry.get_provider("factory")

        assert result is factory_fn
        factory_fn.assert_not_called()

    def test_none_resolves_to_default(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_provider(None) resolves to the default provider."""
        registry.register("memory", DummyAdapter)

        result = registry.get_provider(None)

        assert result is DummyAdapter

    def test_unknown_name_raises_adapter_not_found(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_provider() raises AdapterNotFoundError for unregistered name."""
        with pytest.raises(AdapterNotFoundError):
            registry.get_provider("unknown")

    def test_no_default_no_name_raises_adapter_not_found(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_provider(None) with no default raises AdapterNotFoundError."""
        with pytest.raises(AdapterNotFoundError):
            registry.get_provider(None)

    def test_triggers_auto_discover_when_not_found(self):
        """get_provider() invokes auto_discover when provider is missing."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )

        def side_effect():
            registry.register("lazy", DummyAdapter)

        discover_fn.side_effect = side_effect

        result = registry.get_provider("lazy")

        discover_fn.assert_called_once()
        assert result is DummyAdapter

    def test_does_not_trigger_auto_discover_when_found(self):
        """get_provider() skips auto_discover when provider already registered."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )
        registry.register("existing", DummyAdapter)

        registry.get_provider("existing")

        discover_fn.assert_not_called()


# ---------------------------------------------------------------------------
# has_instance / set_instance / instance_count
# ---------------------------------------------------------------------------


class TestInstanceManipulationBehavior:
    """has_instance(), set_instance(), and instance_count() manage cached instances."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    # -- has_instance --

    def test_has_instance_returns_false_for_empty_registry(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_instance() returns False when no instances are cached."""
        assert registry.has_instance("anything") is False

    def test_has_instance_returns_true_after_get(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_instance() returns True after get() creates and caches an instance."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")

        assert registry.has_instance("memory") is True

    def test_has_instance_returns_false_after_clear(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_instance() returns False after clear_instances()."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")
        registry.clear_instances()

        assert registry.has_instance("memory") is False

    # -- set_instance --

    def test_set_instance_injects_into_cache(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """set_instance() makes the instance retrievable via has_instance()."""
        sentinel = DummyAdapter()
        registry.register("memory", DummyAdapter)

        registry.set_instance("memory", sentinel)

        assert registry.has_instance("memory") is True
        assert registry.get("memory") is sentinel

    def test_set_instance_overwrites_existing(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """set_instance() replaces an existing cached instance."""
        registry.register("memory", DummyAdapter)
        original = registry.get("memory")
        replacement = DummyAdapter()

        registry.set_instance("memory", replacement)

        assert registry.get("memory") is replacement
        assert registry.get("memory") is not original

    # -- instance_count --

    def test_instance_count_zero_when_empty(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """instance_count() returns 0 for a fresh registry."""
        assert registry.instance_count() == 0

    def test_instance_count_increments_on_get(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """instance_count() reflects the number of cached instances."""
        registry.register("a", DummyAdapter)
        registry.register("b", DummyAdapter)

        registry.get("a")
        assert registry.instance_count() == 1

        registry.get("b")
        assert registry.instance_count() == 2

    def test_instance_count_zero_after_clear(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """instance_count() returns 0 after clear_instances()."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")
        registry.clear_instances()

        assert registry.instance_count() == 0


# ---------------------------------------------------------------------------
# get_default_name / has_any_providers
# ---------------------------------------------------------------------------


class TestDefaultNameAndAnyProvidersBehavior:
    """get_default_name() and has_any_providers() expose read-only state."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_get_default_name_none_initially(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_default_name() returns None for fresh registry."""
        assert registry.get_default_name() is None

    def test_get_default_name_after_register(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_default_name() returns first registered name."""
        registry.register("alpha", DummyAdapter)
        registry.register("beta", DummyAdapter)

        assert registry.get_default_name() == "alpha"

    def test_get_default_name_after_set_default(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_default_name() reflects set_default()."""
        registry.register("alpha", DummyAdapter)
        registry.set_default("beta")

        assert registry.get_default_name() == "beta"

    def test_has_any_providers_false_initially(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_any_providers() returns False for fresh registry."""
        assert registry.has_any_providers() is False

    def test_has_any_providers_true_after_register(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_any_providers() returns True after registration."""
        registry.register("memory", DummyAdapter)

        assert registry.has_any_providers() is True

    def test_has_any_providers_false_after_reset(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """has_any_providers() returns False after reset()."""
        registry.register("memory", DummyAdapter)
        registry.reset()

        assert registry.has_any_providers() is False


# ---------------------------------------------------------------------------
# get_cached_instances
# ---------------------------------------------------------------------------


class TestGetCachedInstancesBehavior:
    """get_cached_instances() returns a safe copy of the instance cache."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_returns_empty_dict_for_fresh_registry(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_cached_instances() returns {} when no instances cached."""
        assert registry.get_cached_instances() == {}

    def test_returns_cached_instances(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """get_cached_instances() includes all cached instances."""
        registry.register("a", DummyAdapter)
        registry.register("b", DummyAdapter)
        inst_a = registry.get("a")
        inst_b = registry.get("b")

        cached = registry.get_cached_instances()

        assert cached["a"] is inst_a
        assert cached["b"] is inst_b
        assert len(cached) == 2

    def test_returns_copy_not_reference(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Mutating the returned dict does not affect the registry's internal cache."""
        registry.register("memory", DummyAdapter)
        registry.get("memory")

        # When
        cached = registry.get_cached_instances()
        cached.clear()

        # Then — internal state unaffected
        assert registry.instance_count() == 1
        assert registry.has_instance("memory") is True

    def test_concurrent_get_cached_instances_does_not_crash(self):
        """get_cached_instances() is safe under concurrent modification."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="test")
        for i in range(20):
            registry.register(f"p{i}", DummyAdapter)
            registry.get(f"p{i}")

        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(10)

        def reader():
            try:
                barrier.wait()
                results.append(registry.get_cached_instances())
            except Exception as e:
                errors.append(e)

        def mutator():
            try:
                barrier.wait()
                registry.set_instance(
                    f"new_{id(threading.current_thread())}", DummyAdapter()
                )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        threads += [threading.Thread(target=mutator) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        assert len(results) == 5


# ---------------------------------------------------------------------------
# save_state / restore_state / snapshot
# ---------------------------------------------------------------------------


class TestSaveRestoreStateBehavior:
    """save_state()/restore_state() round-trips all 4 registry attributes."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        return GenericProviderRegistry[DummyAdapter](adapter_type="test")

    def test_save_restore_preserves_providers(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """save_state/restore_state preserves provider registrations."""
        registry.register("memory", DummyAdapter)
        snapshot = registry.save_state()

        registry.reset()
        assert not registry.has_provider("memory")

        registry.restore_state(snapshot)
        assert registry.has_provider("memory")

    def test_save_restore_preserves_instances(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """save_state/restore_state preserves cached instances."""
        registry.register("memory", DummyAdapter)
        original = registry.get("memory")
        snapshot = registry.save_state()

        registry.clear_instances()
        assert registry.instance_count() == 0

        registry.restore_state(snapshot)
        assert registry.get("memory") is original

    def test_save_restore_preserves_default(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """save_state/restore_state preserves the default name."""
        registry.register("memory", DummyAdapter)
        registry.set_default("custom")
        snapshot = registry.save_state()

        registry.reset()
        assert registry.get_default_name() is None

        registry.restore_state(snapshot)
        assert registry.get_default_name() == "custom"

    def test_save_restore_preserves_auto_discover(self):
        """save_state/restore_state preserves the auto_discover callback."""
        discover_fn = MagicMock()
        registry = GenericProviderRegistry[DummyAdapter](
            adapter_type="test", auto_discover=discover_fn
        )
        snapshot = registry.save_state()

        # Wipe auto_discover via a blank restore
        registry.restore_state(
            {
                "providers": {},
                "instances": {},
                "default": None,
                "auto_discover": None,
            }
        )

        # Restore original state
        registry.restore_state(snapshot)

        # Verify auto_discover is functional by triggering it
        def side_effect():
            registry.register("lazy", DummyAdapter)

        discover_fn.side_effect = side_effect
        registry.get("lazy")
        discover_fn.assert_called_once()

    def test_snapshot_saved_is_independent_copy(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Mutations after save_state() do not affect the saved snapshot."""
        registry.register("a", DummyAdapter)
        snapshot = registry.save_state()

        registry.register("b", DummyAdapter)
        registry.get("b")

        registry.restore_state(snapshot)
        assert registry.has_provider("a")
        assert not registry.has_provider("b")
        assert registry.instance_count() == 0

    def test_snapshot_reusable_after_post_restore_mutation(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """Mutations after restore_state() do not pollute the snapshot."""
        # Given — save a snapshot with one provider
        registry.register("original", DummyAdapter)
        snapshot = registry.save_state()

        # When — restore, then mutate the registry
        registry.restore_state(snapshot)
        registry.register("injected", DummyAdapter)
        registry.get("injected")

        # Then — second restore from the same snapshot must not include "injected"
        registry.restore_state(snapshot)
        assert registry.has_provider("original")
        assert not registry.has_provider("injected")
        assert registry.instance_count() == 0


class TestSnapshotContextManagerBehavior:
    """snapshot() context manager auto-restores state on exit."""

    @pytest.fixture
    def registry(self) -> GenericProviderRegistry[DummyAdapter]:
        reg = GenericProviderRegistry[DummyAdapter](adapter_type="test")
        reg.register("original", DummyAdapter)
        return reg

    def test_snapshot_restores_on_normal_exit(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """State is restored after the with block exits normally."""
        with registry.snapshot():
            registry.register("temporary", DummyAdapter)
            registry.set_default("temporary")

        assert not registry.has_provider("temporary")
        assert registry.get_default_name() == "original"

    def test_snapshot_restores_on_exception(
        self, registry: GenericProviderRegistry[DummyAdapter]
    ):
        """State is restored even when an exception occurs inside the block."""
        with pytest.raises(RuntimeError, match="boom"):
            with registry.snapshot():
                registry.reset()
                raise RuntimeError("boom")

        assert registry.has_provider("original")
        assert registry.get_default_name() == "original"

    def test_snapshot_nested(self, registry: GenericProviderRegistry[DummyAdapter]):
        """Nested snapshot() blocks restore to the correct level."""
        with registry.snapshot():
            registry.register("level1", DummyAdapter)

            with registry.snapshot():
                registry.register("level2", DummyAdapter)
                assert registry.has_provider("level2")

            # After inner exit — level2 removed
            assert not registry.has_provider("level2")
            assert registry.has_provider("level1")

        # After outer exit — level1 removed
        assert not registry.has_provider("level1")
        assert registry.has_provider("original")


# ---------------------------------------------------------------------------
# ContextVar-backed instance cache (#450 Phase 3, D5)
# ---------------------------------------------------------------------------


class TestGenericProviderRegistryContextScopingBehavior:
    """``_instances`` reads/writes pass through ``_instances_var`` (ContextVar).

    Per 450 D5, the cached-instance dict lives behind a ContextVar so test
    fixtures can swap in a per-Context isolated dict without disturbing the
    process-shared default. ``_providers`` and ``_default`` remain
    process-global because providers are registered once at import time.
    """

    def test_default_dict_is_shared_across_contexts(self):
        """The ContextVar default is the same dict across every Context.

        Without this guarantee, a plain ``threading.Thread`` worker (which
        does NOT inherit ContextVar values per PEP 567) would see an empty
        instance cache and lazy-create a *separate* singleton — breaking the
        "exactly one instance" contract relied on by background services.
        """
        import contextvars

        registry = GenericProviderRegistry[DummyAdapter](adapter_type="ctx-shared")

        observed_in_copy: list = []

        def child():
            observed_in_copy.append(registry._instances_var.get())

        ctx = contextvars.copy_context()
        ctx.run(child)

        # Same dict object — copy_context() inherits the ContextVar's default.
        assert observed_in_copy[0] is registry._shared_instances

    def test_set_inside_copy_context_does_not_leak_to_parent(self):
        """``ContextVar.set`` in a child Context never mutates the parent's view."""
        import contextvars

        registry = GenericProviderRegistry[DummyAdapter](adapter_type="ctx-isolation")
        registry.register("memory", DummyAdapter)

        # Pre-cache an instance in the parent's view.
        parent_instance = registry.get("memory")

        observed_child: list = []

        def child():
            isolated_dict: dict = {}
            registry._instances_var.set(isolated_dict)
            # Child sees an empty cache → lazy-creates a fresh instance.
            observed_child.append(registry.get("memory"))

        ctx = contextvars.copy_context()
        ctx.run(child)

        # Parent's view is untouched.
        assert registry.get("memory") is parent_instance
        # Child observed a different instance.
        assert observed_child[0] is not parent_instance

    def test_instances_setter_writes_through_context_var(self):
        """Assigning to ``_instances`` swaps the ContextVar value, not an attribute."""
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="ctx-setter")
        registry.register("memory", DummyAdapter)
        original = registry.get("memory")

        # When — install an empty dict via the property setter.
        replacement: dict = {}
        registry._instances = replacement

        # Then — the ContextVar now points at the replacement, and a fresh
        # ``get()`` call repopulates that exact dict (not the original cache).
        assert registry._instances_var.get() is replacement
        recreated = registry.get("memory")
        assert recreated is not original
        assert "memory" in replacement
        assert replacement["memory"] is recreated

    def test_plain_thread_inherits_shared_default_dict(self):
        """A plain ``threading.Thread`` worker sees the parent's cached instances.

        PEP 567: ``threading.Thread`` does not propagate ContextVar values.
        The shared default dict is what allows worker threads to still find
        the singleton the main thread cached.
        """
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="ctx-thread")
        registry.register("memory", DummyAdapter)
        main_instance = registry.get("memory")

        worker_observed: list = []

        def worker():
            # ContextVar slot not inherited; falls back to the shared default
            # dict supplied at ContextVar construction.
            worker_observed.append(registry.get("memory"))

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)

        assert worker_observed[0] is main_instance

    def test_isolated_context_yields_independent_instance_cache(self):
        """``isolated_context()`` produces a registry whose cache starts empty."""
        # Sanity that the existing isolation primitive composes with the new
        # ContextVar-backed storage — registrations copy, instances do not.
        registry = GenericProviderRegistry[DummyAdapter](adapter_type="ctx-isolated")
        registry.register("memory", DummyAdapter)
        registry.get("memory")  # cache it in the parent

        with registry.isolated_context() as isolated:
            assert isolated.instance_count() == 0
            isolated_instance = isolated.get("memory")
            assert isolated_instance is not registry.get("memory")
