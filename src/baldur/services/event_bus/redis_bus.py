# verified-by: test_self_originated_event_not_double_fired
"""
Redis Event Bus — cross-pod event propagation via Redis Pub/Sub.

Provides L2 (multi-instance) event propagation on top of the L1
in-memory BaldurEventBus. All events are delivered locally first,
then propagated to Redis Pub/Sub for cross-pod delivery.

Delivery guarantee: **at-most-once**.
- Redis Pub/Sub is fire-and-forget; messages are dropped if a subscriber
  is disconnected at the time of publish.
- During reconnect windows, cross-pod events are lost by design.
  Local handlers continue to fire normally.
- Anti-entropy mechanisms (state sync, periodic reconciliation) in
  higher-level services are responsible for eventual consistency.
- L3 protection: Critical events (REGION_PRIMARY_CHANGED, EMERGENCY_*,
  KILL_SWITCH_*) fall back to Kafka + WAL when Redis is unavailable.

Channels:
- chaos: Chaos Engineering events
- config: Configuration change events
- emergency: Emergency mode events
- circuit_breaker: CB state change events
- throttle: Throttle events
- global: Cross-service events (Error Budget, Security, Cell Topology, etc.)

Reference:
- docs/baldur/middleware_system/70_MULTI_CLUSTER_ARCHITECTURE.md
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from baldur.core.shutdown_coordinator import ShutdownHandler
from baldur.services.event_bus.bus import (
    BaldurEvent,
    BaldurEventBus,
    EventPriority,
    EventType,
)

if TYPE_CHECKING:
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )
from baldur.services.event_bus.bus.models import (
    EventSubscription,
    create_event,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads

logger = structlog.get_logger()

# Listener loop cadence AND the DaemonWorkerHandle heartbeat declaration derive
# from this single constant — staleness detection compares heartbeat age against
# tick x staleness_multiplier, so the declaration must track the actual loop wait.
_TICK_INTERVAL_SECONDS = 1.0

__all__ = [
    "RedisEventBus",
    "EventChannel",
    "BALDUR_EVENT_CHANNELS",
    "EVENT_TYPE_TO_CHANNEL",
    "CRITICAL_EVENT_TYPES",
]

# Critical events that must propagate even during infra failures
CRITICAL_EVENT_TYPES: frozenset[EventType] = frozenset(
    {
        EventType.REGION_PRIMARY_CHANGED,
        EventType.EMERGENCY_ACTIVATED,
        EventType.KILL_SWITCH_ACTIVATED,
    }
)


# =============================================================================
# Channel Definitions
# =============================================================================


class EventChannel(str, Enum):
    """Redis Pub/Sub channel definitions."""

    CHAOS = "chaos"
    CONFIG = "config"
    EMERGENCY = "emergency"
    CIRCUIT_BREAKER = "circuit_breaker"
    THROTTLE = "throttle"
    GLOBAL = "global"


# Channel-to-Redis key mapping
BALDUR_EVENT_CHANNELS: dict[str, str] = {
    EventChannel.CHAOS.value: "baldur:events:chaos",
    EventChannel.CONFIG.value: "baldur:events:config",
    EventChannel.EMERGENCY.value: "baldur:events:emergency",
    EventChannel.CIRCUIT_BREAKER.value: "baldur:events:cb",
    EventChannel.THROTTLE.value: "baldur:events:throttle",
    EventChannel.GLOBAL.value: "baldur:global:events",
}

# EventType → Channel mapping
EVENT_TYPE_TO_CHANNEL: dict[EventType, EventChannel] = {
    # Chaos Events
    EventType.CHAOS_EXPERIMENT_STARTED: EventChannel.CHAOS,
    EventType.CHAOS_EXPERIMENT_STOPPED: EventChannel.CHAOS,
    EventType.CHAOS_EXPERIMENT_BLOCKED: EventChannel.CHAOS,
    # Config Events
    EventType.CONFIG_UPDATED: EventChannel.CONFIG,
    EventType.KILL_SWITCH_ACTIVATED: EventChannel.CONFIG,
    EventType.KILL_SWITCH_DEACTIVATED: EventChannel.CONFIG,
    # Emergency Events
    EventType.EMERGENCY_LEVEL_CHANGED: EventChannel.EMERGENCY,
    EventType.EMERGENCY_ACTIVATED: EventChannel.EMERGENCY,
    EventType.EMERGENCY_RECOVERY_STARTED: EventChannel.EMERGENCY,
    EventType.EMERGENCY_RECOVERY_COMPLETED: EventChannel.EMERGENCY,
    # Circuit Breaker Events
    EventType.CIRCUIT_BREAKER_OPENED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_BREAKER_CLOSED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_BREAKER_HALF_OPENED: EventChannel.CIRCUIT_BREAKER,
    # Error Budget → Global (cross-cluster concern)
    EventType.ERROR_BUDGET_CRITICAL: EventChannel.GLOBAL,
    EventType.ERROR_BUDGET_WARNING: EventChannel.GLOBAL,
    EventType.ERROR_BUDGET_RECOVERED: EventChannel.GLOBAL,
    # Security → Global
    EventType.SECURITY_VIOLATION_DETECTED: EventChannel.GLOBAL,
    EventType.SECURITY_VIOLATION_CRITICAL: EventChannel.GLOBAL,
    # Throttle Events
    EventType.THROTTLE_LIMIT_CHANGED: EventChannel.THROTTLE,
    EventType.THROTTLE_SLA_WARNING: EventChannel.THROTTLE,
    EventType.THROTTLE_SLA_CRITICAL: EventChannel.GLOBAL,
    EventType.THROTTLE_LIMIT_RECOVERED: EventChannel.THROTTLE,
    # Throttle + DLQ integration
    EventType.THROTTLE_REJECTION_STORED: EventChannel.THROTTLE,
    EventType.THROTTLE_REJECTION_REPLAY_STARTED: EventChannel.THROTTLE,
    EventType.THROTTLE_REJECTION_REPLAY_COMPLETED: EventChannel.THROTTLE,
    EventType.THROTTLE_REJECTION_REPLAY_FAILED: EventChannel.THROTTLE,
    # Cell Topology Events → Global (cross-pod propagation required)
    EventType.CELL_STATE_CHANGED: EventChannel.GLOBAL,
    EventType.CELL_EVACUATION_STARTED: EventChannel.GLOBAL,
    EventType.CELL_EVACUATION_COMPLETED: EventChannel.GLOBAL,
    EventType.CELL_RESTORED: EventChannel.GLOBAL,
    EventType.CELL_EVACUATION_CANCELLED: EventChannel.GLOBAL,
    # Circuit Mesh → circuit_breaker channel (CB domain sub-concept)
    EventType.CIRCUIT_MESH_OVERRIDE_APPLIED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_MESH_OVERRIDE_EXPIRED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_MESH_OVERRIDE_RELEASED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_MESH_MAX_OVERRIDES_REACHED: EventChannel.CIRCUIT_BREAKER,
    EventType.CIRCUIT_MESH_ESCALATION_TRIGGERED: EventChannel.GLOBAL,
    # Rollback → Global (cross-service impact)
    EventType.ROLLBACK_REQUESTED: EventChannel.GLOBAL,
    EventType.ROLLBACK_STARTED: EventChannel.GLOBAL,
    EventType.ROLLBACK_COMPLETED: EventChannel.GLOBAL,
    EventType.ROLLBACK_FAILED: EventChannel.GLOBAL,
    EventType.ROLLBACK_CANCELLED: EventChannel.GLOBAL,
    # Precomputed Cache → Config (config/state change category)
    EventType.PRECOMPUTED_CACHE_INVALIDATED: EventChannel.CONFIG,
}


class RedisEventBus:
    """
    Redis Pub/Sub distributed event bus.

    Synchronizes events in real-time across multi-instance environments.
    Provides the same interface as BaldurEventBus while propagating
    events to other instances via Redis.

    Multi-channel support:
    - chaos: Chaos Engineering events
    - config: Configuration change events
    - emergency: Emergency mode events
    - circuit_breaker: CB state change events
    - throttle: Throttle events
    - global: Cross-service events (Error Budget, Security, etc.)

    Usage:
        from baldur.services.event_bus import get_event_bus

        bus = get_event_bus()
        bus.subscribe(EventType.CHAOS_EXPERIMENT_STARTED, my_handler)
        bus.emit(EventType.CHAOS_EXPERIMENT_STARTED,
                 data={"experiment_id": "exp-123"},
                 source="scheduler")
    """

    _RECONNECT_INTERVAL: float = 30.0  # seconds between reconnect attempts

    def __init__(
        self,
        channels: dict[str, str] | None = None,
        subscribe_channels: list[EventChannel] | None = None,
    ):
        """
        Initialize RedisEventBus.

        Args:
            channels: Channel definitions (defaults to BALDUR_EVENT_CHANNELS)
            subscribe_channels: Channels to subscribe (defaults to all channels)
        """
        self._instance_id: str = uuid4().hex
        self._channels = channels or BALDUR_EVENT_CHANNELS
        self._subscribe_channels = subscribe_channels or list(EventChannel)
        self._local_bus = BaldurEventBus()
        self._redis_client: Any | None = None
        self._pubsub: Any | None = None
        self._listener_thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.RLock()
        self._subscribed_redis_channels: set[str] = set()
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Connect to Redis
        self._connect_redis()

    def _connect_redis(self) -> bool:
        """Connect to Redis via RedisConnectionFactory (Standalone/Sentinel/Cluster)."""
        try:
            from baldur.adapters.redis.connection_factory import (
                get_redis_connection_factory,
            )
            from baldur.settings.event_bus import get_event_bus_settings
            from baldur.settings.redis import get_redis_settings

            url = get_event_bus_settings().redis_url or get_redis_settings().url
            if not url:
                logger.info("redis_event_bus.no_redis_url_configured")
                return False

            factory = get_redis_connection_factory()
            self._redis_client = factory.create(url, decode_responses=True)
            self._redis_client.ping()
            logger.info("redis_event_bus.connected_redis")
            return True
        except ImportError:
            logger.warning("redis_event_bus.redis_package_not_installed")
            return False
        except Exception as e:
            logger.warning("redis_event_bus.redis_connection_failed", error=e)
            if self._redis_client is not None:
                try:
                    self._redis_client.close()
                except Exception:
                    pass
            self._redis_client = None
            return False

    # -------------------------------------------------------------------------
    # Listener Management
    # -------------------------------------------------------------------------

    def start_listener(self) -> None:
        """Start listener thread (runs regardless of Redis availability)."""
        from baldur.meta.daemon_worker import DaemonWorkerHandle
        from baldur.metrics.recorders.daemon_worker import register_daemon_worker

        with self._lock:
            if self._running:
                return
            self._running = True
            if self._redis_client:
                self._setup_pubsub()
            self._spawn_listener_thread()
            assert self._listener_thread is not None  # spawn always sets non-None
            self._handle = DaemonWorkerHandle(
                thread=self._listener_thread,
                tick_interval_seconds=_TICK_INTERVAL_SECONDS,
                restart_callback=self._spawn_listener_thread,
            )
            register_daemon_worker("RedisEventBusListener", self._handle)
            logger.info(
                "redis_event_bus.listener_started",
                subscribed_redis_channels=list(self._subscribed_redis_channels),
            )

    def _spawn_listener_thread(self) -> None:
        """Construct + start a fresh listener thread (impl 489 D9)."""
        self._listener_thread = threading.Thread(
            target=self._listen_loop_with_crash_capture,
            daemon=True,
            name="RedisEventBusListener",
        )
        self._listener_thread.start()
        if self._handle is not None:
            self._handle.thread = self._listener_thread

    def _listen_loop_with_crash_capture(self) -> None:
        try:
            self._listen_loop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as e:
            if self._handle is not None:
                self._handle.record_crash(e)
            raise

    def stop_listener(self) -> None:
        """Stop Redis Pub/Sub listener."""
        from baldur.metrics.recorders.daemon_worker import unregister_daemon_worker

        with self._lock:
            if self._handle is not None:
                self._handle.is_stopping = True
            self._running = False
            if self._pubsub:
                try:
                    self._pubsub.unsubscribe()
                    self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None
        # Best-effort join + unregister (lock dropped — listener thread checks _running)
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=2.0)
            unregister_daemon_worker("RedisEventBusListener")
            if self._listener_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="RedisEventBusListener",
                    join_timeout_seconds=2.0,
                )
        logger.info("redis_event_bus.listener_stopped")

    def _setup_pubsub(self) -> None:
        """Initialize Pub/Sub subscriptions (called on connect/reconnect)."""
        assert self._redis_client is not None  # caller checks self._redis_client truthy
        self._pubsub = self._redis_client.pubsub()
        for channel_enum in self._subscribe_channels:
            channel_name = self._channels.get(channel_enum.value)
            if channel_name:
                self._pubsub.subscribe(channel_name)
                self._subscribed_redis_channels.add(channel_name)

    def _listen_loop(self) -> None:
        """Message receive loop + reconnect on failure."""
        import time as _time

        while self._running:
            iter_start = _time.monotonic()
            if self._pubsub:
                try:
                    message = self._pubsub.get_message(timeout=_TICK_INTERVAL_SECONDS)
                    if message and message["type"] == "message":
                        self._handle_redis_message(message["data"])
                except Exception as e:
                    if self._running:
                        logger.exception("redis_event_bus.listener_error", error=e)
                        # Attempt cleanup before entering reconnect loop
                        pubsub = self._pubsub
                        self._redis_client = None
                        self._pubsub = None
                        if pubsub:
                            try:
                                pubsub.unsubscribe()
                                pubsub.close()
                            except Exception:
                                pass
            else:
                # Redis unavailable — periodic reconnect attempt
                if self._try_reconnect():
                    logger.info(
                        "redis_event_bus.reconnected",
                        subscribed_channels=list(self._subscribed_redis_channels),
                    )
                else:
                    time.sleep(self._RECONNECT_INTERVAL)

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

    def _try_reconnect(self) -> bool:
        """Attempt Redis reconnection (pattern: RedisCacheAdapter.reconnect)."""
        if self._redis_client is not None:
            return True
        if not self._connect_redis():
            return False
        with self._lock:
            self._setup_pubsub()
        return True

    def _handle_redis_message(self, data: str) -> None:
        """Process incoming Redis message with self-message filtering."""
        try:
            event_dict = fast_loads(data)
            # Skip self-originated message (double-fire prevention)
            if event_dict.pop("_origin", None) == self._instance_id:
                return
            event = BaldurEvent(
                event_type=EventType(event_dict["event_type"]),
                data=event_dict["data"],
                source=event_dict["source"],
                timestamp=datetime.fromisoformat(event_dict["timestamp"]),
                priority=EventPriority(event_dict.get("priority", 2)),
                correlation_id=event_dict.get("correlation_id"),
            )
            self._local_bus.publish(event)
        except Exception as e:
            logger.exception(
                "redis_event_bus.process_message_failed",
                error=e,
            )

    # -------------------------------------------------------------------------
    # Event Publishing
    # -------------------------------------------------------------------------

    def publish(self, event: BaldurEvent) -> int:
        """Publish event locally and propagate to Redis.

        Args:
            event: Event to publish

        Returns:
            int: Number of local handlers called
        """
        count = self._local_bus.publish(event)
        self._publish_distributed(event)
        return count

    def emit(
        self,
        event_type: EventType,
        data: dict[str, Any],
        source: str = "unknown",
        priority: EventPriority = EventPriority.NORMAL,
        correlation_id: str | None = None,
    ) -> int:
        """Convenience event publishing with automatic trace context.

        Args:
            event_type: Event type
            data: Event data
            source: Event source
            priority: Priority
            correlation_id: Correlation ID

        Returns:
            int: Number of handlers called
        """
        event = create_event(event_type, data, source, priority, correlation_id)
        return self.publish(event)

    def _publish_distributed(self, event: BaldurEvent) -> None:
        """Propagate event to Redis, with Kafka/WAL fallback for critical events."""
        event_dict = event.to_dict()
        event_dict["_origin"] = self._instance_id

        # Local ref snapshot — avoids TOCTOU race
        client = self._redis_client
        if client is not None:
            try:
                channel = self._get_channel_for_event(event.event_type)
                client.publish(
                    channel,
                    fast_dumps_str(event_dict, default=str),
                )
                return
            except Exception as e:
                logger.warning("redis_event_bus.redis_publish_failed", error=e)

        # Kafka fallback (critical events only)
        if self._is_critical_event(event):
            try:
                self._publish_to_kafka_fallback(event)
                return
            except Exception as e:
                logger.exception("redis_event_bus.kafka_fallback_failed", error=e)
            self._write_to_wal(event)

    def _is_critical_event(self, event: BaldurEvent) -> bool:
        """Determine if event must propagate even during infra failures."""
        return event.event_type in CRITICAL_EVENT_TYPES

    def _publish_to_kafka_fallback(self, event: BaldurEvent) -> None:
        """Kafka fallback via KafkaAuditProducer singleton (fire-and-forget).

        Uses the framework's existing Kafka producer infrastructure instead of
        creating ad-hoc producers per event. The singleton handles connection
        pooling, async delivery callbacks, and WAL-backed error recovery.
        """
        # 528 D10-v2: Kafka producer relocated to baldur_dormant. Falls
        # open with a clear error when baldur_dormant or its kafka extra
        # is not installed.
        try:
            from baldur_dormant.adapters.kafka.producer import get_kafka_producer
        except ImportError as e:
            from baldur.core.exceptions import AdapterError

            raise AdapterError(
                "Kafka fallback requires baldur-pro[kafka]; install with: "
                "pip install baldur-pro[kafka]"
            ) from e

        producer = get_kafka_producer()
        success = producer.publish(
            topic="baldur.routing.events",
            event=event.to_dict(),
        )

        if success:
            logger.info(
                "redis_event_bus.event_published_kafka_fallback",
                event_type=event.event_type.value,
            )
        else:
            logger.warning(
                "redis_event_bus.kafka_fallback_publish_failed",
                event_type=event.event_type.value,
            )
            self._write_to_wal(event)

    def _write_to_wal(self, event: BaldurEvent) -> None:
        """Write event to local WAL (last resort safety net).

        WAL entries can be replayed to Redis/Kafka when infrastructure recovers.
        Reuses the audit/wal module's WriteAheadLog pattern.
        """
        try:
            from baldur.audit.wal import WriteAheadLog
            from baldur.audit.wal._models import WALConfig

            config = WALConfig(
                wal_dir="/var/log/baldur/event_bus_wal",
                file_prefix="event_bus_wal",
                max_file_size_mb=50,
                sync_on_write=True,
                max_files=5,
            )
            wal = WriteAheadLog(config=config)
            wal.write(event.to_dict())

            logger.warning(
                "redis_event_bus.critical_event_written_wal",
                event_type=event.event_type.value,
            )
        except Exception as e:
            logger.exception(
                "redis_event_bus.wal_write_failed_critical",
                event_type=event.event_type.value,
                error=e,
            )

    def _get_channel_for_event(self, event_type: EventType) -> str:
        """Return Redis channel for an EventType."""
        channel_enum = EVENT_TYPE_TO_CHANNEL.get(event_type, EventChannel.GLOBAL)
        return self._channels.get(
            channel_enum.value, self._channels[EventChannel.GLOBAL.value]
        )

    # -------------------------------------------------------------------------
    # Subscription Management
    # -------------------------------------------------------------------------

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
        priority: EventPriority = EventPriority.NORMAL,
        *,
        await_result: bool = True,
    ) -> EventSubscription:
        """Subscribe to an event type.

        Args:
            event_type: Event type to subscribe to
            handler: Handler function
            priority: Handler priority
            await_result: When False, the local bus dispatches the handler
                fire-and-forget (publisher thread never blocks on the
                handler body). Forwarded to ``BaldurEventBus.subscribe`` —
                both backends run local handlers through the same dispatch
                path.

        Returns:
            EventSubscription: Subscription info
        """
        return self._local_bus.subscribe(
            event_type, handler, priority=priority, await_result=await_result
        )

    def unsubscribe(
        self,
        event_type: EventType,
        handler: Callable[[BaldurEvent], None],
    ) -> bool:
        """Unsubscribe a handler from an event type."""
        return self._local_bus.unsubscribe(event_type, handler)

    # -------------------------------------------------------------------------
    # History & Introspection
    # -------------------------------------------------------------------------

    def get_history(
        self,
        event_type: EventType | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Delegate to local bus history (local process events only)."""
        return self._local_bus.get_history(event_type, limit)

    def get_channel(self, channel: EventChannel) -> str:
        """Return Redis key for a specific channel."""
        return self._channels.get(channel.value, "")

    def get_local_bus(self) -> BaldurEventBus:
        """Return local event bus."""
        return self._local_bus

    def is_distributed(self) -> bool:
        """Whether distributed mode (Redis connected) is active."""
        return self._redis_client is not None

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def reset(self) -> None:
        """Reset state (for testing)."""
        self.stop_listener()
        self._local_bus.reset()


# =============================================================================
# Graceful Shutdown Integration
# =============================================================================


class RedisEventBusShutdownHandler(ShutdownHandler):
    """Drain RedisEventBus listener on graceful shutdown."""

    def __init__(self, bus: RedisEventBus):
        self._bus = bus

    def on_shutdown_start(self) -> None:
        """Stop accepting new Redis messages (non-blocking)."""
        self._bus._running = False
        logger.info("redis_event_bus.shutdown_started")

    def is_drain_complete(self) -> bool:
        """Check if listener thread has exited."""
        thread = self._bus._listener_thread
        return thread is None or not thread.is_alive()

    def on_drain_complete(self) -> None:
        """Clean up Pub/Sub resources after drain."""
        self._bus.stop_listener()
        logger.info("redis_event_bus.shutdown_drained")

    def on_force_shutdown(self, pending_requests: list) -> None:
        """Force stop on timeout."""
        self._bus.stop_listener()
        logger.warning("redis_event_bus.shutdown_forced")


def integrate_with_shutdown_coordinator() -> RedisEventBusShutdownHandler | None:
    """Factory for shutdown integration (pattern: chaos/scheduler/shutdown.py).

    Usage (application bootstrap — see adapters/django/apps.py):
        from baldur.core.shutdown_coordinator import get_shutdown_coordinator

        coordinator = get_shutdown_coordinator()
        handler = integrate_with_shutdown_coordinator()
        if handler:
            coordinator.register_handler(handler)
    """
    from baldur.services.event_bus.bus.convenience import get_event_bus

    bus = get_event_bus()
    if not isinstance(bus, RedisEventBus):
        return None
    return RedisEventBusShutdownHandler(bus)
