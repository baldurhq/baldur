"""
Event Bus Package - Component Decoupling System.

이벤트 기반 아키텍처를 통해 컴포넌트 간 느슨한 결합을 제공합니다.

Modules:
    - bus: In-memory event bus (BaldurEventBus)
    - redis_bus: Redis Pub/Sub 기반 분산 이벤트 버스 (RedisEventBus)

Usage:
    from baldur.services.event_bus import (
        get_event_bus,
        BaldurEvent,
        EventType,
    )

.. versionadded:: 2.1.0
    ``event_bus.py`` 플랫 파일에서 ``event_bus/`` 패키지로 전환.
"""

# bus.py의 모든 속성(private handler 포함)을 패키지 레벨에 노출.
# 기존 `from baldur.services.event_bus import _on_*` 패턴 호환 유지.
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
