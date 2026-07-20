"""
Event Bus Package - Component Decoupling System.

Provides loose coupling between components via an event-driven architecture.

Modules:
    - bus: In-memory event bus (BaldurEventBus)
    - redis_bus: Redis Pub/Sub-based distributed event bus (RedisEventBus)

Usage:
    from baldur.services.event_bus import (
        get_event_bus,
        BaldurEvent,
        EventType,
    )

.. versionadded:: 2.1.0
    Converted from the flat ``event_bus.py`` file to the ``event_bus/`` package.
"""

# Expose every attribute of the bus module (including private handlers) at the
# package level, keeping the existing
# `from baldur.services.event_bus import _on_*` pattern working.
import sys as _sys

from baldur.services.event_bus import bus as _bus_module
from baldur.services.event_bus.bus import (
    BaldurEvent,
    BaldurEventBus,
    EventPriority,
    EventSubscription,
    EventType,
    create_event,
    emit_circuit_breaker_state_changed,
    emit_emergency_level_changed,
    emit_error_budget_critical,
    get_event_bus,
    register_default_handlers,
)
from baldur.services.event_bus.emitter import EventEmitterMixin

_pkg = _sys.modules[__name__]
for _name in dir(_bus_module):
    if not _name.startswith("__") and not hasattr(_pkg, _name):
        setattr(_pkg, _name, getattr(_bus_module, _name))
del _name, _pkg

__all__ = [
    # Types & Enums
    "EventType",
    "EventPriority",
    "BaldurEvent",
    "EventSubscription",
    # Core
    "BaldurEventBus",
    # Factory
    "create_event",
    # Mixin
    "EventEmitterMixin",
    # Singleton & Convenience
    "get_event_bus",
    "register_default_handlers",
    "emit_emergency_level_changed",
    "emit_error_budget_critical",
    "emit_circuit_breaker_state_changed",
]


def __getattr__(name: str):
    """Delegate to bus sub-package for lazy-loaded attributes (e.g. private handlers)."""
    val = getattr(_bus_module, name)
    # Cache on this module to avoid repeated __getattr__ calls
    _sys.modules[__name__].__dict__[name] = val
    return val
