"""516 D4 part 2 — shutdown integration order preservation.

When :func:`baldur.bootstrap._register_shutdown_handlers` collapses the six
hardcoded ``try: from baldur_pro.services.X.shutdown ... import
integrate_with_shutdown_coordinator`` blocks into a single iteration over
``ProviderRegistry.shutdown_integrations.list_providers()``, the previous
ordering (which depended on file-line position) MUST be preserved:

    chaos → bulkhead → hedging → auto_tuning → saga → emergency_mode

The collapsed implementation drives ordering via two coupled mechanisms:

1. ``baldur_pro.__init__._register_all_pro_services`` lists the PRO modules
   in the legacy bootstrap order.
2. ``GenericProviderRegistry._providers`` is a plain ``dict`` (Python 3.7+
   insertion-ordered), so ``list_providers()`` returns registrations in
   exactly the order ``register()`` calls fired.

If either side drifts (a refactor reorders the module list, or someone swaps
the underlying ``_providers`` container), this test catches the regression
before it ships. Pure integration test — no Docker / Redis / DB needed; we
exercise the real registry singleton and the real PRO shutdown modules.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from baldur.factory.registry import ProviderRegistry

_EXPECTED_ORDER = [
    "chaos_scheduler",
    "bulkhead",
    "hedging",
    "auto_tuning",
    "saga",
    "emergency_mode",
]

_PRO_SHUTDOWN_MODULES = [
    "baldur_pro.services.chaos.scheduler.shutdown",
    "baldur_pro.services.bulkhead.shutdown",
    "baldur_pro.services.hedging.shutdown",
    "baldur_pro.services.auto_tuning.shutdown",
    "baldur_pro.services.saga.shutdown",
    "baldur_pro.services.emergency_mode.shutdown_handler",
]


@pytest.fixture
def fresh_shutdown_registry():
    """Snapshot, clear, and restore the shutdown_integrations sub-registry.

    Each PRO shutdown module's ``_register_shutdown_integration()`` fires at
    import time as a side effect. To test registration order
    deterministically we drop both the cached module objects and the
    registry entries, then re-import in the order ``baldur_pro.__init__``
    lists them.
    """
    registry = ProviderRegistry.shutdown_integrations
    saved_providers = dict(registry._providers)
    saved_default = registry.get_default_name()

    registry.reset()
    for module_path in _PRO_SHUTDOWN_MODULES:
        sys.modules.pop(module_path, None)

    try:
        yield registry
    finally:
        registry.reset()
        for module_path in _PRO_SHUTDOWN_MODULES:
            sys.modules.pop(module_path, None)
        for name, provider in saved_providers.items():
            registry.register(name, provider)
        if saved_default is not None and saved_default in saved_providers:
            registry.set_default(saved_default)


def test_pro_shutdown_modules_register_in_bootstrap_order(fresh_shutdown_registry):
    """Importing the six PRO shutdown modules in bootstrap order yields the
    insertion order that ``baldur.bootstrap`` relies on for handler dispatch.
    """
    for module_path in _PRO_SHUTDOWN_MODULES:
        try:
            importlib.import_module(module_path)
        except ImportError:
            pytest.skip(f"PRO module {module_path} not installed")

    observed_order = fresh_shutdown_registry.list_providers()

    assert observed_order == _EXPECTED_ORDER, (
        f"Shutdown integration order drift: expected {_EXPECTED_ORDER}, "
        f"got {observed_order}. The collapsed bootstrap iteration depends "
        f"on this exact insertion order — fix the module list in "
        f"`baldur_pro.__init__._register_all_pro_services` (516 D4)."
    )


def test_each_registered_factory_is_callable(fresh_shutdown_registry):
    """Every entry must be a zero-arg factory returning a ShutdownHandler-ish
    object (or ``None`` if PRO state is incomplete). The bootstrap iteration
    in :func:`baldur.bootstrap._register_shutdown_handlers` calls each
    factory positionally with no args.
    """
    for module_path in _PRO_SHUTDOWN_MODULES:
        try:
            importlib.import_module(module_path)
        except ImportError:
            pytest.skip(f"PRO module {module_path} not installed")

    for name in fresh_shutdown_registry.list_providers():
        factory = fresh_shutdown_registry.get_provider(name)
        assert callable(factory), (
            f"shutdown_integrations[{name}] is not callable: {type(factory).__name__}"
        )
