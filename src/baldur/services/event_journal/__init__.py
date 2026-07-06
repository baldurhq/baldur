"""
Event Journal Service.

Baldur 결정 이벤트를 append-only 저널에 기록한다.
Config Shadow Evaluator(299)의 시뮬레이션 데이터 소스로 사용된다.
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
    """EventJournal 구독자를 초기화한다. 앱 시작 시 1회 호출."""
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
    """현재 초기화된 JournalSubscriber를 반환한다. 미초기화 시 None."""
    return _journal_subscriber


def reset_event_journal() -> None:
    """JournalSubscriber 싱글톤을 리셋한다 (테스트용)."""
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
