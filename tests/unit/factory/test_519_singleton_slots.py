"""Unit tests for 519 PR 2 — ProviderRegistry singleton slots + safe_get().

Tests cover:
- ``GenericProviderRegistry.safe_get()`` — returns ``None`` when the slot is
  unregistered, returns the resolved instance when registered, and does NOT
  swallow non-``AdapterNotFoundError`` exceptions raised by a faulty factory.
- ``ProviderRegistry.<14 (c) slots>`` — the 14 new class-level slots exist,
  each is a ``GenericProviderRegistry`` with a matching ``adapter_type``, and
  re-registration overwrites without leaking the prior factory.

Targets per 519 § Test Assessment:
- TestGenericProviderRegistrySafeGetBehavior
- TestProviderRegistry519SingletonSlotsContract
"""

from __future__ import annotations

import pytest

from baldur.factory.base import GenericProviderRegistry
from baldur.factory.registry import ProviderRegistry


class _Dummy:
    """Minimal adapter stub for slot tests."""

    def __init__(self) -> None:
        self.alive = True


_EXPECTED_519_SLOTS: tuple[str, ...] = (
    "emergency_manager",
    "adaptive_throttle",
    "bulkhead_registry",
    "runtime_config_manager",
    "chaos_scheduler",
    "report_generator",
    "safety_guard",
    "dlq_service",
    "dlq_repository",
    "selfhealer_watchdog",
    "error_budget_service",
    "error_budget_gate",
    "canary_rollout_service",
    "blast_radius_manager",
)


class TestGenericProviderRegistrySafeGetBehavior:
    """Behavioral tests for ``GenericProviderRegistry.safe_get()`` (519 PR 2)."""

    def test_safe_get_returns_none_when_no_provider_registered(self):
        """Empty registry: ``safe_get()`` returns ``None`` instead of raising."""
        registry = GenericProviderRegistry[_Dummy](adapter_type="t1")

        assert registry.safe_get() is None

    def test_safe_get_returns_none_for_unknown_name(self):
        """Known name unregistered: ``safe_get('x')`` returns ``None``."""
        registry = GenericProviderRegistry[_Dummy](adapter_type="t2")
        registry.register("known", _Dummy)

        assert registry.safe_get("unknown") is None

    def test_safe_get_returns_resolved_instance_when_registered(self):
        """Provider registered: ``safe_get()`` returns the same instance ``get()`` returns."""
        registry = GenericProviderRegistry[_Dummy](adapter_type="t3")
        registry.register("memory", _Dummy)

        result = registry.safe_get()

        assert isinstance(result, _Dummy)
        assert result is registry.get()

    def test_safe_get_with_explicit_name_returns_that_provider(self):
        """``safe_get('name')`` resolves the matching provider, not the default."""
        registry = GenericProviderRegistry[_Dummy](adapter_type="t4")
        registry.register("default_one", _Dummy)
        registry.register("other", _Dummy)

        # Two distinct cached singletons
        assert registry.safe_get("default_one") is registry.get("default_one")
        assert registry.safe_get("other") is registry.get("other")
        assert registry.safe_get("default_one") is not registry.safe_get("other")

    def test_safe_get_does_not_swallow_factory_exceptions(self):
        """``safe_get()`` only catches ``AdapterNotFoundError`` — factory errors propagate.

        Why: the docstring promises ``safe_get`` is the None-returning sibling
        of ``get`` for the unregistered case. Silencing arbitrary factory
        exceptions would mask production bugs.
        """
        registry = GenericProviderRegistry[_Dummy](adapter_type="t5")

        def _broken_factory():
            raise RuntimeError("factory broke")

        registry.register("broken", _broken_factory)

        with pytest.raises(RuntimeError, match="factory broke"):
            registry.safe_get("broken")

    def test_safe_get_idempotent_returns_same_cached_instance(self):
        """Repeated ``safe_get()`` calls return the cached singleton (DCL pattern)."""
        registry = GenericProviderRegistry[_Dummy](adapter_type="t6")
        registry.register("cached", _Dummy)

        first = registry.safe_get()
        second = registry.safe_get()
        third = registry.safe_get()

        assert first is second is third


class TestProviderRegistry519SingletonSlotsContract:
    """Contract tests for the 14 new (c) singleton slots on ProviderRegistry."""

    @pytest.mark.parametrize("slot_name", _EXPECTED_519_SLOTS)
    def test_slot_exists_on_provider_registry(self, slot_name: str):
        """Each 519 (c) slot is exposed as a class attribute."""
        assert hasattr(ProviderRegistry, slot_name), (
            f"ProviderRegistry is missing 519 slot: {slot_name}"
        )

    @pytest.mark.parametrize("slot_name", _EXPECTED_519_SLOTS)
    def test_slot_is_generic_provider_registry(self, slot_name: str):
        """Each slot is a ``GenericProviderRegistry`` instance."""
        slot = getattr(ProviderRegistry, slot_name)
        assert isinstance(slot, GenericProviderRegistry)

    @pytest.mark.parametrize("slot_name", _EXPECTED_519_SLOTS)
    def test_slot_adapter_type_matches_attribute_name(self, slot_name: str):
        """Each slot's ``adapter_type`` mirrors its attribute name (used by ``AdapterNotFoundError``)."""
        slot = getattr(ProviderRegistry, slot_name)
        assert slot._adapter_type == slot_name

    def test_slot_count_is_14(self):
        """519 PR 2 adds exactly 14 (c) singleton slots (Implementation Deviations PR 2)."""
        assert len(_EXPECTED_519_SLOTS) == 14
        for name in _EXPECTED_519_SLOTS:
            assert hasattr(ProviderRegistry, name)

    def test_re_register_pro_overwrites_without_leaking_prior_factory(self):
        """Re-registering ``'pro'`` replaces the prior factory.

        The autouse fixture in conftest re-registers all 14 slots on each
        test. Confirm the registry honors overwrite semantics so the fixture
        cannot leak stale singletons between tests.
        """
        slot = ProviderRegistry.emergency_manager
        # Snapshot the autouse-fixture registration so we restore it.
        with slot.snapshot():
            sentinel_first = object()
            slot.register("pro", lambda: sentinel_first)
            slot.set_default("pro")
            slot.clear_instances()
            assert slot.get() is sentinel_first

            sentinel_second = object()
            slot.register("pro", lambda: sentinel_second)
            slot.clear_instances()  # invalidate cached instance for re-resolution
            assert slot.get() is sentinel_second

    def test_safe_get_on_empty_slot_returns_none(self):
        """A slot with no provider (after a full reset) returns ``None`` via ``safe_get()``."""
        slot = ProviderRegistry.blast_radius_manager
        with slot.snapshot():
            slot.reset()
            assert slot.safe_get() is None
