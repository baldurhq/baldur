"""
Event bus sub-module structure tests.

Verifies modules extracted per 355_INIT_FILE_BUSINESS_LOGIC_EXTRACTION:
- event_types.py: EventType enum, EventPriority
- models.py: BaldurEvent, EventSubscription
- event_bus.py: D2 — __new__ singleton removed
- convenience.py: get/reset_event_bus lifecycle
- PEP 562 lazy import + globals() caching
"""

import pytest


class TestEventTypeEnumContract:
    """EventType enum member count contract."""

    def test_event_type_has_at_least_51_members(self):
        """EventType enum has >= 51 members per design."""
        from baldur.services.event_bus.bus.event_types import EventType

        assert len(EventType) >= 51

    def test_event_type_is_str_enum(self):
        """EventType inherits from (str, Enum) for JSON serialization."""
        from baldur.services.event_bus.bus.event_types import EventType

        assert isinstance(EventType.EMERGENCY_LEVEL_CHANGED, str)
        assert EventType.EMERGENCY_LEVEL_CHANGED == "emergency_level_changed"

    def test_event_priority_values(self):
        """EventPriority has LOW=1, NORMAL=2, HIGH=3, CRITICAL=4."""
        from baldur.services.event_bus.bus.event_types import EventPriority

        assert EventPriority.LOW == 1
        assert EventPriority.NORMAL == 2
        assert EventPriority.HIGH == 3
        assert EventPriority.CRITICAL == 4


class TestBaldurEventModelContract:
    """BaldurEvent dataclass contract."""

    def test_event_to_dict_contains_required_keys(self):
        """to_dict() output has all required keys."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import BaldurEvent

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={"key": "value"},
            source="test",
        )
        d = event.to_dict()
        assert "event_type" in d
        assert "data" in d
        assert "source" in d
        assert "timestamp" in d
        assert "priority" in d
        assert "event_id" in d

    def test_event_id_auto_generated(self):
        """event_id is automatically generated if not provided."""
        from baldur.services.event_bus.bus.event_types import EventType
        from baldur.services.event_bus.bus.models import BaldurEvent

        e1 = BaldurEvent(event_type=EventType.CONFIG_UPDATED, data={}, source="test")
        e2 = BaldurEvent(event_type=EventType.CONFIG_UPDATED, data={}, source="test")
        assert e1.event_id != e2.event_id


class TestEventBusNoNewSingletonBehavior:
    """D2: BaldurEventBus no longer uses __new__ singleton."""

    def test_two_instances_are_distinct(self):
        """BaldurEventBus() creates distinct instances (D2)."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        b1 = BaldurEventBus()
        b2 = BaldurEventBus()
        assert b1 is not b2

    def test_instance_has_no_class_level_singleton_field(self):
        """BaldurEventBus no longer has _instance class attribute."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        assert not hasattr(BaldurEventBus, "_instance")


class TestEventBusConvenienceSingletonBehavior:
    """get_event_bus / reset_event_bus singleton lifecycle."""

    def setup_method(self):
        from baldur.services.event_bus.bus.convenience import reset_event_bus

        reset_event_bus()

    def teardown_method(self):
        from baldur.services.event_bus.bus.convenience import reset_event_bus

        reset_event_bus()

    def test_get_returns_same_instance(self):
        """get_event_bus() returns the same instance on repeated calls."""
        from baldur.services.event_bus.bus.convenience import get_event_bus

        first = get_event_bus()
        second = get_event_bus()
        assert first is second

    def test_reset_clears_cached_instance(self):
        """reset_event_bus() creates a new instance on next get."""
        from baldur.services.event_bus.bus.convenience import (
            get_event_bus,
            reset_event_bus,
        )

        first = get_event_bus()
        reset_event_bus()
        second = get_event_bus()
        assert first is not second

    def test_reset_calls_bus_reset(self):
        """reset_event_bus() calls .reset() on the existing bus."""
        from baldur.services.event_bus.bus.convenience import (
            get_event_bus,
            reset_event_bus,
        )
        from baldur.services.event_bus.bus.event_types import EventType

        bus = get_event_bus()
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)
        assert bus.get_stats()["subscriptions_count"] > 0

        reset_event_bus()
        # Old bus should have been reset
        assert bus.get_stats()["subscriptions_count"] == 0


class TestEventBusInitLazyImportBehavior:
    """PEP 562 lazy import + globals() caching behavior for bus/__init__.py."""

    def test_all_public_symbols_importable_via_init(self):
        """All __all__ symbols importable from bus/__init__.py."""
        from baldur.services.event_bus.bus import (
            BaldurEvent,
            BaldurEventBus,
            EventPriority,
            EventSubscription,
            EventType,
            emit_circuit_breaker_state_changed,
            emit_emergency_level_changed,
            emit_error_budget_critical,
            get_event_bus,
            register_default_handlers,
            reset_event_bus,
        )

        assert EventType is not None
        assert EventPriority is not None
        assert BaldurEvent is not None
        assert EventSubscription is not None
        assert BaldurEventBus is not None
        assert callable(get_event_bus)
        assert callable(reset_event_bus)
        assert callable(register_default_handlers)
        assert callable(emit_emergency_level_changed)
        assert callable(emit_error_budget_critical)
        assert callable(emit_circuit_breaker_state_changed)

    def test_init_all_has_15_public_symbols(self):
        """__all__ declares 15 public symbols (487 added 3 dispatch-shutdown re-exports)."""
        import baldur.services.event_bus.bus as mod

        assert len(mod.__all__) == 15

    def test_unknown_attribute_raises_attribute_error(self):
        """Accessing undefined attribute via __getattr__ raises AttributeError."""
        import baldur.services.event_bus.bus as mod

        with pytest.raises(AttributeError):
            _ = mod.DoesNotExist
