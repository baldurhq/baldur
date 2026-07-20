"""
Event Journal Service.

Records Baldur decision events into an append-only journal.
Used as the simulation data source for the Config Shadow Evaluator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from baldur.services.event_journal.subscriber import (
    JOURNALED_EVENT_TYPES,
    JournalSubscriber,
)

if TYPE_CHECKING:
    from baldur.interfaces.event_bus import EventBusProtocol
    from baldur.services.event_bus.bus import BaldurEventBus  # noqa: F401

_journal_subscriber: JournalSubscriber | None = None


def init_event_journal(
    bus: EventBusProtocol | None = None,
) -> JournalSubscriber | None:
    """Initialize the EventJournal subscriber. Called once at app startup."""
    global _journal_subscriber
    if _journal_subscriber is not None:
        return _journal_subscriber

    from baldur.settings.event_journal import get_event_journal_settings

    settings = get_event_journal_settings()
    if not settings.enabled:
        return None

    from baldur.factory import ProviderRegistry
    from baldur.services.event_bus.bus import get_event_bus

    repository = ProviderRegistry.get_event_journal_repo()
    _journal_subscriber = JournalSubscriber(repository=repository)

    if bus is None:
        bus = get_event_bus()
    assert bus is not None  # get_event_bus singleton always returns non-None
    _journal_subscriber.register(bus)

    return _journal_subscriber


def get_event_journal() -> JournalSubscriber | None:
    """Return the initialized JournalSubscriber, or None if not initialized."""
    return _journal_subscriber


def reset_event_journal() -> None:
    """Reset the JournalSubscriber singleton (for testing)."""
    global _journal_subscriber
    if _journal_subscriber is not None:
        _journal_subscriber.close()
    _journal_subscriber = None


__all__ = [
    "JOURNALED_EVENT_TYPES",
    "JournalSubscriber",
    "get_event_journal",
    "init_event_journal",
    "reset_event_journal",
]
