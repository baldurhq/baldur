"""Singleton management and convenience emit functions for the Event Bus."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.utils.singleton import make_singleton_factory

from .event_types import EventPriority, EventType

if TYPE_CHECKING:
    from baldur.interfaces.event_bus import EventBusProtocol

logger = structlog.get_logger()

__all__ = [
    "get_event_bus",
    "configure_event_bus",
    "reset_event_bus",
    "emit_emergency_level_changed",
    "emit_error_budget_critical",
    "emit_circuit_breaker_state_changed",
]

# =============================================================================
# Singleton
# =============================================================================


def _create_event_bus() -> EventBusProtocol:
    """Create EventBus instance based on EventBusSettings."""
    from baldur.settings.event_bus import get_event_bus_settings

    settings = get_event_bus_settings()
    if settings.backend == "redis":
        from baldur.services.event_bus.redis_bus import RedisEventBus

        bus = RedisEventBus()
        bus.start_listener()
        if not bus.is_distributed():
            logger.warning(
                "event_bus.redis_unavailable_local_only",
                msg="Redis unavailable — running in local-only mode, "
                "will auto-reconnect when Redis recovers",
            )
        return bus
    from .event_bus import BaldurEventBus

    return BaldurEventBus()


get_event_bus, configure_event_bus, reset_event_bus = make_singleton_factory(
    "event_bus",
    _create_event_bus,
    cleanup_fn=lambda bus: bus.reset(),
)


# =============================================================================
# Convenience Functions
# =============================================================================


def emit_emergency_level_changed(
    level: int,
    previous_level: int,
    reason: str = "",
    source: str = "emergency_manager",
) -> int:
    """Emit emergency mode level changed event (convenience function)."""
    return get_event_bus().emit(
        event_type=EventType.EMERGENCY_LEVEL_CHANGED,
        data={
            "level": level,
            "previous_level": previous_level,
            "reason": reason,
            "is_escalation": level > previous_level,
        },
        source=source,
        priority=EventPriority.HIGH,
    )


def emit_error_budget_critical(
    budget_percent: float,
    threshold: float = 20.0,
    source: str = "error_budget_gate",
) -> int:
    """Emit error budget critical threshold event (convenience function)."""
    return get_event_bus().emit(
        event_type=EventType.ERROR_BUDGET_CRITICAL,
        data={
            "budget_percent": budget_percent,
            "threshold": threshold,
        },
        source=source,
        priority=EventPriority.CRITICAL,
    )


def emit_circuit_breaker_state_changed(
    service_name: str,
    new_state: str,
    previous_state: str,
    source: str = "circuit_breaker_service",
) -> int:
    """Emit circuit breaker state changed event (convenience function)."""
    # Determine event type based on state
    if new_state.upper() == "OPEN":
        event_type = EventType.CIRCUIT_BREAKER_OPENED
    elif new_state.upper() == "CLOSED":
        event_type = EventType.CIRCUIT_BREAKER_CLOSED
    elif new_state.upper() in ("HALF_OPEN", "HALF-OPEN"):
        event_type = EventType.CIRCUIT_BREAKER_HALF_OPENED
    else:
        logger.warning(
            "event_bus.unknown_cb_state_skipped",
            new_state=new_state,
            previous_state=previous_state,
        )
        return 0

    return get_event_bus().emit(
        event_type=event_type,
        data={
            "service_name": service_name,
            "new_state": new_state,
            "previous_state": previous_state,
        },
        source=source,
    )
