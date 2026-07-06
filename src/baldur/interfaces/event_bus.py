"""EventBus Protocols and OSS-side NoOp defaults.

Two distinct contracts live here:

1. ``EventBusProtocol`` — the in-process / Redis event bus contract used by
   OSS services (``BaldurEventBus``, ``RedisEventBus``). Stable, OSS-tier.
2. ``KafkaEventBusProtocol`` / ``ConsumedEventProtocol`` — OSS-side typing
   targets for the Kafka adapter family that lives in ``baldur_dormant``.
   Callers that type-hint against these Protocols
   stay compile-clean on a clean-OSS install where ``baldur_dormant`` is
   not present. The Protocol module is named ``event_bus`` (not ``kafka``)
   to keep backend-neutral naming in the OSS interface layer.

OSS NoOp defaults — ``NoOpKafkaEventBus`` — let callers route through
``ProviderRegistry.kafka_eventbus.get()`` unconditionally without
``is not None`` guards even when ``baldur_dormant`` is absent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from baldur.services.event_bus.bus.event_types import EventPriority, EventType
    from baldur.services.event_bus.bus.models import (
        BaldurEvent,
        EventSubscription,
    )

__all__ = [
    "EventBusProtocol",
    "KafkaEventBusProtocol",
    "ConsumedEventProtocol",
    "NoOpKafkaEventBus",
]


class EventBusProtocol(Protocol):
    """Protocol for event bus implementations.

    Both BaldurEventBus (L1 in-memory) and RedisEventBus (L2 distributed)
    implement this protocol. Used as the return type of the unified
    get_event_bus() factory.
    """

    def emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        source: str = ...,
        priority: EventPriority = ...,
        correlation_id: str | None = ...,
    ) -> int: ...

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
        priority: EventPriority = ...,
        *,
        await_result: bool = ...,
    ) -> EventSubscription: ...

    def unsubscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
    ) -> bool: ...

    def publish(self, event: BaldurEvent) -> int: ...

    def get_history(
        self,
        event_type: EventType | None = ...,
        limit: int = ...,
    ) -> list[dict[str, Any]]: ...

    def reset(self) -> None: ...


# =============================================================================
# Kafka adapter Protocols (OSS typing surface for baldur_dormant.adapters.kafka)
# =============================================================================
# Doc 528 D10-v2 "OSS interfaces extracted": the concrete classes live in
# ``baldur_dormant.adapters.kafka.{producer,consumer,event_bus}``. OSS callers
# in ``server.py`` / ``services/event_bus/redis_bus.py`` /
# ``services/rate_limit/distributed_channel.py`` reference these Protocols
# instead of the concrete classes so type-checking stays clean on the public
# install surface. Methods cover only the OSS-caller usage axis — not the
# full surface of the concrete Kafka implementations.


@runtime_checkable
class ConsumedEventProtocol(Protocol):
    """Value-shape Protocol for events consumed from a Kafka topic.

    Mirrors the field set on the Dormant ``ConsumedEvent`` class. Pure
    attribute Protocol — no methods. Used by OSS
    callers that pattern-match on event payload shape without importing
    the Dormant concrete class.
    """

    topic: str
    partition: int
    offset: int
    key: str | None
    value: dict[str, Any]
    headers: dict[str, bytes]
    timestamp: float


@runtime_checkable
class KafkaEventBusProtocol(Protocol):
    """Protocol for the Kafka-backed event bus (Producer + Consumer combo).

    Implementations: the Dormant ``KafkaEventBus`` class. The OSS-facing
    surface is publish/subscribe + lifecycle
    methods (start/stop/close/flush).
    """

    def publish(
        self,
        topic: str,
        event: dict[str, Any],
        key: str | None = ...,
        on_delivery: Callable[..., None] | None = ...,
    ) -> bool: ...

    def subscribe(
        self,
        topic: str,
        handler: Callable[[ConsumedEventProtocol], bool],
    ) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...

    def flush(self, timeout: float = ...) -> None: ...


# =============================================================================
# OSS NoOp default for the kafka_eventbus ProviderRegistry slot
# =============================================================================


class NoOpKafkaEventBus:
    """No-op fallback for the ``kafka_eventbus`` registry slot (OSS-safe).

    Returned by ``ProviderRegistry.kafka_eventbus.get()`` when
    ``baldur_dormant`` is not installed. publish/subscribe silently no-op
    so OSS callers can use the registry result unconditionally; nothing
    is ever sent to a broker. Logs at DEBUG to surface accidental wiring
    on clean-OSS installs (typical Baldur pattern: NoOp logs are quiet).

    Satisfies the "NoOp default registration requirement".
    """

    def publish(
        self,
        topic: str,
        event: dict[str, Any],
        key: str | None = None,
        on_delivery: Callable[..., None] | None = None,
    ) -> bool:
        import structlog

        structlog.get_logger().debug(
            "kafka_eventbus.noop_publish",
            topic=topic,
            hint="baldur_dormant not installed; Kafka publish dropped silently",
        )
        return False

    def subscribe(
        self,
        topic: str,
        handler: Callable[[ConsumedEventProtocol], bool],
    ) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def close(self) -> None:
        return None

    def flush(self, timeout: float = 10.0) -> None:
        return None
