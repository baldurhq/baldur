"""Event data models for the Baldur Event Bus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

from .event_types import EventPriority, EventType

__all__ = [
    "BaldurEvent",
    "EventSubscription",
    "create_event",
]


@dataclass
class BaldurEvent(SerializableMixin):
    """Baldur event data class."""

    event_type: EventType
    data: dict[str, Any]
    source: str
    timestamp: datetime = field(default_factory=lambda: utc_now())
    priority: EventPriority = EventPriority.NORMAL
    correlation_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class EventSubscription:
    """Event subscription information."""

    event_type: EventType
    handler: Callable[[BaldurEvent], None]
    handler_name: str
    priority: EventPriority = EventPriority.NORMAL
    enabled: bool = True
    # When False, the bus dispatches this handler fire-and-forget — it
    # submits the handler without awaiting its result, so the publisher
    # thread is never blocked on the handler body (even when the handler
    # delegates to an inline-executing Celery task). Best-effort
    # side-effect handlers (notify/snapshot/replay/postmortem) opt out of
    # awaiting; synchronous gates that write event.data keep the default.
    await_result: bool = True

    def __hash__(self):
        return hash((self.event_type, self.handler_name))


def create_event(
    event_type: EventType,
    data: dict[str, Any],
    source: str = "unknown",
    priority: EventPriority = EventPriority.NORMAL,
    correlation_id: str | None = None,
) -> BaldurEvent:
    """Create BaldurEvent with automatic trace context enrichment."""
    if correlation_id is None:
        try:
            from baldur.audit.trace import get_trace_id

            correlation_id = get_trace_id()
        except Exception:
            pass
    return BaldurEvent(
        event_type=event_type,
        data=data,
        source=source,
        priority=priority,
        correlation_id=correlation_id,
    )
