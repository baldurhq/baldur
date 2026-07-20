"""
Event Journal Subscriber.

Receives Baldur decision events from the EventBus and records them in the journal.
Error-isolation principle: a journaling failure never halts Baldur's main logic.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.event_journal import (
    EventJournalRepository,
    JournalEntry,
)
from baldur.services.event_bus.bus import BaldurEvent, EventType

logger = structlog.get_logger()

if TYPE_CHECKING:
    from baldur.interfaces.event_bus import EventBusProtocol


JOURNALED_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.CIRCUIT_BREAKER_OPENED,
        EventType.CIRCUIT_BREAKER_CLOSED,
        EventType.CIRCUIT_BREAKER_HALF_OPENED,
        EventType.ERROR_BUDGET_CRITICAL,
        EventType.ERROR_BUDGET_WARNING,
        EventType.ERROR_BUDGET_RECOVERED,
        EventType.EMERGENCY_LEVEL_CHANGED,
    }
)


class _JournalCircuitBreaker:
    """
    Lightweight CB dedicated to journaling. No external dependencies.

    Using CircuitBreakerService directly would create a dependency cycle:
    CB -> EventBus -> JournalSubscriber -> CB.
    A self-contained lightweight CB gives fast fail-fast on Redis outages.
    """

    def __init__(self, failure_threshold: int = 5, recovery_seconds: float = 30):
        self._failures = 0
        self._threshold = failure_threshold
        self._open_until: float = 0
        self._recovery = recovery_seconds

    def is_open(self) -> bool:
        if self._failures < self._threshold:
            return False
        return time.monotonic() < self._open_until

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._recovery

    def record_success(self) -> None:
        self._failures = 0


class JournalSubscriber:
    """Receives events from the EventBus and records them in the journal."""

    def __init__(self, repository: EventJournalRepository):
        self._repository = repository
        self._cb = _JournalCircuitBreaker()
        self._subscribed: bool = False
        self._bus: EventBusProtocol | None = None

    def register(self, bus: EventBusProtocol) -> None:
        """Subscribe to the target event types."""
        if self._subscribed:
            return

        for event_type in JOURNALED_EVENT_TYPES:
            bus.subscribe(event_type, self._handle_event)

        self._bus = bus
        self._subscribed = True

    def close(self) -> None:
        """Unsubscribe all EventBus handlers.

        Idempotent: safe to call multiple times.
        """
        if not self._subscribed:
            return

        try:
            bus = self._bus
            if bus is None:
                from baldur.services.event_bus.bus import get_event_bus

                bus = get_event_bus()
            assert bus is not None  # get_event_bus singleton always returns non-None

            for event_type in JOURNALED_EVENT_TYPES:
                bus.unsubscribe(event_type, self._handle_event)

            self._subscribed = False
            self._bus = None
            logger.debug("event_journal.subscriber_unsubscribed")
        except ImportError:
            pass
        except Exception:
            self._subscribed = False
            self._bus = None

    def _handle_event(self, event: BaldurEvent) -> None:
        """
        Convert the event into a JournalEntry and store it.

        Error-isolation principle:
        - A journaling failure must never halt Baldur's main logic.
        - On a sustained Redis outage the internal CB opens for fast fail-fast.
        """
        if self._cb.is_open():
            return

        try:
            entry = self._build_entry(event)
            self._repository.append(entry)
            self._cb.record_success()
        except (TypeError, ValueError) as e:
            logger.warning(
                "journal.serialization_failed",
                event_type=event.event_type.value,
                error=str(e),
            )
        except Exception as e:
            self._cb.record_failure()
            logger.warning(
                "journal.append_failed",
                event_type=event.event_type.value,
                error=str(e),
            )

    def _build_entry(self, event: BaldurEvent) -> JournalEntry:
        """Convert the event into a JournalEntry, using defensive serialization."""
        safe_context = json.loads(json.dumps(event.data, default=str))
        return JournalEntry(
            sequence=0,
            event_type=event.event_type.value,
            source=event.source,
            timestamp=event.timestamp,
            service_name=event.data.get("service_name", ""),
            context=safe_context,
            region=event.data.get("region", ""),
            tier_id=event.data.get("tier_id", ""),
        )
