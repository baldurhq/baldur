"""
Event Journal Subscriber.

EventBus에서 Baldur 결정 이벤트를 수신하여 저널에 기록한다.
에러 격리 원칙: 저널링 실패가 Baldur 메인 로직을 중단시키지 않는다.
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
    저널링 전용 경량 CB. 외부 의존 없음.

    CircuitBreakerService를 직접 사용하면 순환 의존이 발생한다:
    CB -> EventBus -> JournalSubscriber -> CB.
    자체 완결형 경량 CB로 Redis 장애 시 빠른 fail-fast를 구현한다.
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
    """EventBus에서 이벤트를 수신하여 저널에 기록한다."""

    def __init__(self, repository: EventJournalRepository):
        self._repository = repository
        self._cb = _JournalCircuitBreaker()
        self._subscribed: bool = False
        self._bus: EventBusProtocol | None = None

    def register(self, bus: EventBusProtocol) -> None:
        """대상 이벤트 타입에 대해 구독을 등록한다."""
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
        이벤트를 JournalEntry로 변환하여 저장한다.

        에러 격리 원칙:
        - 저널링 실패가 Baldur 메인 로직을 중단시켜서는 안 된다.
        - Redis 장애 지속 시 내부 CB가 열려 빠르게 fail-fast 처리.
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
        """이벤트를 JournalEntry로 변환한다. 방어적 직렬화 적용."""
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
