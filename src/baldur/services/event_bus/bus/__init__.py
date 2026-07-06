"""Baldur Event Bus — in-memory pub/sub with priority and history."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .convenience import (
        emit_circuit_breaker_state_changed as emit_circuit_breaker_state_changed,
    )
    from .convenience import (
        emit_emergency_level_changed as emit_emergency_level_changed,
    )
    from .convenience import (
        emit_error_budget_critical as emit_error_budget_critical,
    )
    from .convenience import (
        get_event_bus as get_event_bus,
    )
    from .convenience import (
        reset_event_bus as reset_event_bus,
    )
    from .default_handlers import register_default_handlers as register_default_handlers
    from .event_bus import (
        BaldurEventBus as BaldurEventBus,
    )
    from .event_bus import (
        BaldurEventBusDispatchShutdownHandler as BaldurEventBusDispatchShutdownHandler,
    )
    from .event_bus import (
        integrate_dispatch_with_shutdown_coordinator as integrate_dispatch_with_shutdown_coordinator,
    )
    from .event_bus import (
        shutdown_dispatch_executor as shutdown_dispatch_executor,
    )
    from .event_types import EventPriority as EventPriority
    from .event_types import EventType as EventType
    from .models import (
        BaldurEvent as BaldurEvent,
    )
    from .models import (
        EventSubscription as EventSubscription,
    )
    from .models import (
        create_event as create_event,
    )

__all__ = [
    "EventType",
    "EventPriority",
    "BaldurEvent",
    "EventSubscription",
    "BaldurEventBus",
    "BaldurEventBusDispatchShutdownHandler",
    "create_event",
    "get_event_bus",
    "reset_event_bus",
    "register_default_handlers",
    "emit_emergency_level_changed",
    "emit_error_budget_critical",
    "emit_circuit_breaker_state_changed",
    "integrate_dispatch_with_shutdown_coordinator",
    "shutdown_dispatch_executor",
]


def __getattr__(name: str):  # noqa: C901, PLR0912
    if name in ("EventType", "EventPriority"):
        from .event_types import EventPriority, EventType

        globals()["EventType"] = EventType
        globals()["EventPriority"] = EventPriority
        return globals()[name]

    if name in ("BaldurEvent", "EventSubscription", "create_event"):
        from .models import BaldurEvent, EventSubscription, create_event

        globals()["BaldurEvent"] = BaldurEvent
        globals()["EventSubscription"] = EventSubscription
        globals()["create_event"] = create_event
        return globals()[name]

    if name in (
        "BaldurEventBus",
        "BaldurEventBusDispatchShutdownHandler",
        "integrate_dispatch_with_shutdown_coordinator",
        "shutdown_dispatch_executor",
    ):
        from .event_bus import (
            BaldurEventBus,
            BaldurEventBusDispatchShutdownHandler,
            integrate_dispatch_with_shutdown_coordinator,
            shutdown_dispatch_executor,
        )

        globals()["BaldurEventBus"] = BaldurEventBus
        globals()["BaldurEventBusDispatchShutdownHandler"] = (
            BaldurEventBusDispatchShutdownHandler
        )
        globals()["integrate_dispatch_with_shutdown_coordinator"] = (
            integrate_dispatch_with_shutdown_coordinator
        )
        globals()["shutdown_dispatch_executor"] = shutdown_dispatch_executor
        return globals()[name]

    if name in (
        "get_event_bus",
        "reset_event_bus",
        "emit_emergency_level_changed",
        "emit_error_budget_critical",
        "emit_circuit_breaker_state_changed",
    ):
        from .convenience import (
            emit_circuit_breaker_state_changed,
            emit_emergency_level_changed,
            emit_error_budget_critical,
            get_event_bus,
            reset_event_bus,
        )

        globals()["get_event_bus"] = get_event_bus
        globals()["reset_event_bus"] = reset_event_bus
        globals()["emit_emergency_level_changed"] = emit_emergency_level_changed
        globals()["emit_error_budget_critical"] = emit_error_budget_critical
        globals()["emit_circuit_breaker_state_changed"] = (
            emit_circuit_breaker_state_changed
        )
        return globals()[name]

    if name == "register_default_handlers":
        from .default_handlers import register_default_handlers

        globals()["register_default_handlers"] = register_default_handlers
        return register_default_handlers

    # CB handler private functions (used by tests for mock/patch verification)
    _CB_HANDLER_ATTRS = {
        "_on_circuit_breaker_opened_notify",
        "_on_circuit_breaker_closed",
        "_on_circuit_breaker_closed_postmortem",
        "_send_postmortem_notification",
    }
    if name in _CB_HANDLER_ATTRS:
        from . import _cb_handlers

        for _attr in _CB_HANDLER_ATTRS:
            globals()[_attr] = getattr(_cb_handlers, _attr)
        return globals()[name]

    # Emergency postmortem private functions (used by tests)
    _EMERGENCY_POSTMORTEM_ATTRS = {
        "_generate_emergency_postmortem_data",
        "_on_emergency_recovery_completed_postmortem",
    }
    if name in _EMERGENCY_POSTMORTEM_ATTRS:
        from . import _emergency_postmortem

        for _attr in _EMERGENCY_POSTMORTEM_ATTRS:
            globals()[_attr] = getattr(_emergency_postmortem, _attr)
        return globals()[name]

    # Saga handler private functions (used by tests)
    _SAGA_HANDLER_ATTRS = {
        "_on_saga_timed_out",
        "_on_saga_compensation_failed",
        "_on_saga_completed",
        "_on_saga_compensated",
    }
    if name in _SAGA_HANDLER_ATTRS:
        from . import _saga_handlers

        for _attr in _SAGA_HANDLER_ATTRS:
            globals()[_attr] = getattr(_saga_handlers, _attr)
        return globals()[name]

    # Runbook handler private functions (used by tests)
    _RUNBOOK_HANDLER_ATTRS = {
        "_on_runbook_approval_required",
        "_on_runbook_approval_granted",
        "_on_runbook_approval_rejected",
    }
    if name in _RUNBOOK_HANDLER_ATTRS:
        from . import _runbook_handlers

        for _attr in _RUNBOOK_HANDLER_ATTRS:
            globals()[_attr] = getattr(_runbook_handlers, _attr)
        return globals()[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
