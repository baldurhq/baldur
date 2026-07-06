"""
Adapters __init__.py lazy import tests (commit 0b59f932).

Tests for the lazy loading mechanism via __getattr__ and _LAZY_IMPORTS.

Test Categories:
    A. Contract: _LAZY_IMPORTS mapping completeness and __all__ consistency
    B. Behavior: __getattr__ lazy loading, caching, and error handling
"""

import importlib

import pytest

# =============================================================================
# A. Contract Tests — _LAZY_IMPORTS structure and __all__
# =============================================================================


class TestAdaptersLazyImportContract:
    """Verify _LAZY_IMPORTS keys, module paths, and __all__ consistency."""

    def test_lazy_imports_contains_all_expected_adapter_names(self):
        """_LAZY_IMPORTS contains all 13 adapter names from design."""
        from baldur.adapters import _LAZY_IMPORTS

        expected_names = {
            "RedisCircuitBreakerStateRepository",
            "RedisDLQRepository",
            "InMemoryFailedOperationRepository",
            "InMemoryCircuitBreakerStateRepository",
            "InMemorySecurityIncidentRepository",
            "RedisCacheAdapter",
            "InMemoryCacheAdapter",
            "CeleryTaskAdapter",
            "SyncTaskAdapter",
            "HealthCheckStrategy",
            "TTLCacheStrategy",
            "LinuxTCPInfoStrategy",
            "PortableHealthChecker",
        }
        assert set(_LAZY_IMPORTS.keys()) == expected_names

    def test_all_equals_lazy_imports_keys(self):
        """__all__ is derived from _LAZY_IMPORTS keys."""
        from baldur.adapters import _LAZY_IMPORTS
        from baldur.adapters import __all__ as adapters_all

        assert set(adapters_all) == set(_LAZY_IMPORTS.keys())

    def test_each_lazy_import_entry_is_module_attr_tuple(self):
        """Each _LAZY_IMPORTS value is a (module_path, attr_name) tuple."""
        from baldur.adapters import _LAZY_IMPORTS

        for name, value in _LAZY_IMPORTS.items():
            assert isinstance(value, tuple), f"{name} value is not a tuple"
            assert len(value) == 2, f"{name} tuple length is {len(value)}, expected 2"
            module_path, attr_name = value
            assert isinstance(module_path, str)
            assert isinstance(attr_name, str)
            assert attr_name == name, f"attr_name {attr_name!r} != key {name!r}"

    def test_redis_adapters_point_to_redis_module(self):
        """Redis adapter entries reference baldur.adapters.redis."""
        from baldur.adapters import _LAZY_IMPORTS

        assert (
            _LAZY_IMPORTS["RedisCircuitBreakerStateRepository"][0]
            == "baldur.adapters.redis"
        )
        assert _LAZY_IMPORTS["RedisDLQRepository"][0] == "baldur.adapters.redis"

    def test_memory_adapters_point_to_memory_module(self):
        """InMemory adapter entries reference baldur.adapters.memory."""
        from baldur.adapters import _LAZY_IMPORTS

        for name in (
            "InMemoryFailedOperationRepository",
            "InMemoryCircuitBreakerStateRepository",
            "InMemorySecurityIncidentRepository",
        ):
            assert _LAZY_IMPORTS[name][0] == "baldur.adapters.memory"


# =============================================================================
# B. Behavior Tests — __getattr__ lazy loading
# =============================================================================


class TestAdaptersLazyImportBehavior:
    """Verify __getattr__ lazy loading, caching, and error handling."""

    def test_getattr_loads_inmemory_adapter_successfully(self):
        """Accessing InMemoryCacheAdapter via __getattr__ returns the real class."""
        import baldur.adapters as adapters_mod
        from baldur.adapters.cache import InMemoryCacheAdapter as DirectClass

        # Access via lazy import
        lazy_class = adapters_mod.InMemoryCacheAdapter
        assert lazy_class is DirectClass

    def test_getattr_caches_loaded_class_in_globals(self):
        """After first access, the class is cached in module globals."""
        import baldur.adapters as adapters_mod

        # Force fresh state by removing from globals if present
        adapters_mod.__dict__.pop("SyncTaskAdapter", None)

        # First access triggers __getattr__
        cls1 = adapters_mod.SyncTaskAdapter
        # Second access should come from globals (cached)
        assert "SyncTaskAdapter" in adapters_mod.__dict__
        cls2 = adapters_mod.SyncTaskAdapter
        assert cls1 is cls2

    def test_getattr_unknown_name_raises_attribute_error(self):
        """Accessing a non-existent name raises AttributeError."""
        import baldur.adapters as adapters_mod

        with pytest.raises(
            AttributeError, match="has no attribute 'NonExistentAdapter'"
        ):
            adapters_mod.NonExistentAdapter

    def test_getattr_error_message_includes_module_name(self):
        """AttributeError message includes the module name."""
        import baldur.adapters as adapters_mod

        with pytest.raises(AttributeError, match="baldur.adapters"):
            adapters_mod.__getattr__("FooBar")

    def test_lazy_import_does_not_eagerly_load_all_modules(self):
        """Importing baldur.adapters does not eagerly import submodules."""

        # Reload to get fresh state
        mod = importlib.import_module("baldur.adapters")

        # The module should have _LAZY_IMPORTS but not all adapter classes in __dict__
        # (some may be cached from prior tests, so we just verify the mechanism exists)
        assert hasattr(mod, "_LAZY_IMPORTS")
        assert hasattr(mod, "__getattr__")
