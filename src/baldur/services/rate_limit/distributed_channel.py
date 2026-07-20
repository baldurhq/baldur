"""
Distributed rate limit event channel.

Propagates 429 events across the entire cluster via Kafka.
When a single pod receives a 429 response from an external API, every other
pod also holds back requests to that API (collective defense).

Features:
    - Cluster-wide 429 event propagation over Kafka
    - Ordering guaranteed by partition key (same API key -> same partition)
    - Multiple handler support

Usage:
    from baldur.services.rate_limit import DistributedRateLimitChannel

    # Initialize the channel
    channel = DistributedRateLimitChannel()

    # Propagate to the whole cluster when a 429 occurs
    channel.broadcast_rate_limit_429(
        key="payment_api",
        consecutive_429s=3,
        cooldown_until=time.time() + 60,
        calculated_delay=60.0,
    )

    # Subscribe (called on each pod)
    def my_handler(event_data: dict) -> None:
        print(f"Received 429 for {event_data['key']}")

    channel.subscribe_rate_limit_429(my_handler)
    channel.start()
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    # 528 D10-v2: type hints reference the OSS-side Protocols so this
    # module stays compile-clean even when baldur_dormant is absent. The
    # concrete classes live in baldur_dormant.adapters.kafka.* (loaded
    # lazily via ProviderRegistry.kafka_eventbus or direct import).
    from baldur.interfaces.event_bus import (
        ConsumedEventProtocol as ConsumedEvent,
    )
    from baldur.interfaces.event_bus import (
        KafkaEventBusProtocol as KafkaEventBus,
    )

logger = structlog.get_logger()

# Kafka Topic for Rate Limit 429 events
RATE_LIMIT_TOPIC = "baldur.rate_limit.events"


class DistributedRateLimitChannel:
    """
    Kafka-based distributed rate limit event channel.

    Unlike the in-memory EventBus, this propagates 429 events
    to the entire cluster through a Kafka topic.

    Attributes:
        _kafka_bus: Kafka EventBus instance
        _handlers: Registered event handlers
        _running: Channel running state
    """

    _instance: DistributedRateLimitChannel | None = None
    _instance_lock = threading.Lock()

    def __init__(self, kafka_bus: KafkaEventBus | None = None):
        """
        Initialize the distributed rate limit channel.

        Args:
            kafka_bus: Kafka EventBus (created with defaults if None)
        """
        self._kafka_bus: KafkaEventBus | None = kafka_bus
        self._handlers: list[Callable[[dict[str, Any]], None]] = []
        self._running = False
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> DistributedRateLimitChannel:
        """Return the singleton instance."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (test use)."""
        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.stop()
            cls._instance = None

    def _ensure_kafka_bus(self) -> KafkaEventBus:
        """Lazy-init the Kafka EventBus from baldur_dormant."""
        if self._kafka_bus is None:
            # 528 D10-v2: KafkaEventBus relocated to baldur_dormant.
            try:
                from baldur_dormant.adapters.kafka.event_bus import (
                    KafkaEventBus as _KafkaEventBus,
                )

                self._kafka_bus = _KafkaEventBus()
            except ImportError as e:
                logger.exception(
                    "distributed_rate_limit_channel.kafka_unavailable",
                    error=e,
                )
                raise RuntimeError(
                    "Kafka adapter not available; install baldur-pro[kafka]"
                ) from e

        return self._kafka_bus

    def broadcast_rate_limit_429(
        self,
        key: str,
        consecutive_429s: int,
        cooldown_until: float,
        calculated_delay: float,
    ) -> bool:
        """
        Broadcast a 429 event asynchronously to the entire cluster.

        confluent-kafka produce() puts the event into an internal buffer and
        returns immediately, so a Kafka broker outage does not propagate into
        API response latency (fire-and-forget).

        Args:
            key: Rate limit key (e.g. "payment_api")
            consecutive_429s: Number of consecutive 429s
            cooldown_until: Cooldown end time (Unix timestamp)
            calculated_delay: Calculated delay (seconds)

        Returns:
            Whether the send to the internal buffer succeeded
        """
        try:
            kafka_bus = self._ensure_kafka_bus()

            event = {
                "event_type": "RATE_LIMIT_429",
                "key": key,
                "consecutive_429s": consecutive_429s,
                "cooldown_until": cooldown_until,
                "calculated_delay": calculated_delay,
            }

            return kafka_bus.publish(
                topic=RATE_LIMIT_TOPIC,
                event=event,
                key=key,  # same key -> same partition, preserving order
                on_delivery=self._on_broadcast_delivery,
            )

        except Exception as e:
            logger.exception(
                "distributed_rate_limit_channel.broadcast_error",
                error=e,
            )
            return False

    @staticmethod
    def _on_broadcast_delivery(report) -> None:
        """Kafka delivery result callback (fire-and-forget)."""
        if report.error:
            logger.warning(
                "distributed_rate_limit_channel.delivery_failed",
                error=str(report.error),
                topic=report.topic,
            )
        else:
            logger.debug(
                "distributed_rate_limit_channel.delivery_confirmed",
                topic=report.topic,
            )

    def subscribe_rate_limit_429(
        self,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """
        Register a subscription for 429 events.

        Args:
            handler: Event handler (receives an event_data dict)
        """
        with self._lock:
            self._handlers.append(handler)
            logger.info(
                "distributed_rate_limit_channel.handler_registered_total",
                handlers_count=len(self._handlers),
            )

        # Set up the Kafka subscription if it is not configured yet
        try:
            kafka_bus = self._ensure_kafka_bus()
            kafka_bus.subscribe(RATE_LIMIT_TOPIC, self._dispatch_to_handlers)
        except Exception as e:
            logger.warning(
                "distributed_rate_limit_channel.subscribe_setup_failed",
                error=e,
            )

    def _dispatch_to_handlers(self, event: ConsumedEvent) -> bool:
        """
        Deliver a Kafka event to the registered handlers.

        Args:
            event: Event received from Kafka

        Returns:
            Whether processing succeeded
        """
        event_data = event.value if hasattr(event, "value") else event

        with self._lock:
            handlers = list(self._handlers)

        success = True
        for handler in handlers:
            try:
                handler(event_data)  # type: ignore[arg-type]
            except Exception as e:
                logger.exception(
                    "distributed_rate_limit_channel.handler_error",
                    error=e,
                )
                success = False

        return success

    def start(self) -> None:
        """Start the Kafka consumer."""
        if self._running:
            logger.warning("distributed_rate_limit_channel.already_running")
            return

        try:
            kafka_bus = self._ensure_kafka_bus()
            kafka_bus.start()
            self._running = True
            logger.info("distributed_channel.started")
        except Exception as e:
            logger.exception(
                "distributed_rate_limit_channel.start_failed",
                error=e,
            )

    def stop(self) -> None:
        """Stop the Kafka consumer."""
        if not self._running:
            return

        try:
            if self._kafka_bus:
                self._kafka_bus.stop()
            self._running = False
            logger.info("distributed_channel.stopped")
        except Exception as e:
            logger.exception(
                "distributed_rate_limit_channel.stop_failed",
                error=e,
            )

    @property
    def is_running(self) -> bool:
        """Check the channel running state."""
        return self._running

    @property
    def handler_count(self) -> int:
        """Number of registered handlers."""
        with self._lock:
            return len(self._handlers)


def get_distributed_rate_limit_channel() -> DistributedRateLimitChannel:
    """
    Return the distributed rate limit channel singleton.

    Returns:
        DistributedRateLimitChannel instance
    """
    return DistributedRateLimitChannel.get_instance()
