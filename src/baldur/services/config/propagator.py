"""
Global Config Propagator.

Propagates global-namespace config changes to every cluster.

Code basis:
- event_bus_redis.py: RedisEventBus exists, Chaos-only
- event_bus.py: the CONFIG_UPDATED event type is already defined

Extension direction:
- Reuse RedisEventBus for config propagation as well
- Separate the global channel from the regional channel
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.core.cluster_identity import ClusterIdentity
    from baldur.meta.daemon_worker import (  # noqa: F401
        DaemonWorkerHandle,
    )

logger = structlog.get_logger()

# Listener loop cadence AND the DaemonWorkerHandle heartbeat declaration derive
# from this single constant — staleness detection compares heartbeat age against
# tick x staleness_multiplier, so the declaration must track the actual loop wait.
_TICK_INTERVAL_SECONDS = 1.0


class ConfigScope(str, Enum):
    """Config application scope."""

    LOCAL = "local"  # Current cluster only
    REGIONAL = "regional"  # All clusters in the same region
    GLOBAL = "global"  # All clusters


class PropagationTier(str, Enum):
    """Propagation consistency tier (SLA-based)."""

    TIER_1_IMMEDIATE = "tier_1"  # Propagation guaranteed within 1s (Audit/Governance)
    TIER_2_EVENTUAL = "tier_2"  # Propagation within 30s allowed (Metrics/Stats)


@dataclass
class GlobalConfigChange(SerializableMixin):
    """Global config change event."""

    config_type: str  # circuit_breaker, dlq, emergency, etc.
    config_key: str  # Config key
    new_value: Any  # New value
    previous_value: Any  # Previous value
    scope: ConfigScope  # Application scope
    tier: PropagationTier  # Propagation tier
    source_cluster: str  # Cluster where the change originated
    timestamp: datetime = field(default_factory=lambda: utc_now())

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = utc_now()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlobalConfigChange:
        """Build a GlobalConfigChange from a dict."""
        return cls(
            config_type=data["config_type"],
            config_key=data["config_key"],
            new_value=data["new_value"],
            previous_value=data["previous_value"],
            scope=ConfigScope(data["scope"]),
            tier=PropagationTier(data["tier"]),
            source_cluster=data["source_cluster"],
            timestamp=(
                datetime.fromisoformat(data["timestamp"])
                if data.get("timestamp")
                else utc_now()
            ),
        )


class GlobalConfigPropagator:
    """
    Global config propagator.

    Extends RedisEventBus to propagate config changes to every cluster.
    """

    # Channel definitions
    GLOBAL_CONFIG_CHANNEL = "baldur:global:config"
    REGIONAL_CONFIG_CHANNEL_TEMPLATE = "baldur:{region}:config"

    def __init__(
        self,
        redis_client: Any | None = None,
        cluster_identity: ClusterIdentity | None = None,
    ):
        """
        Initialize GlobalConfigPropagator.

        Args:
            redis_client: Redis client (taken from TieredRedisProvider if omitted)
            cluster_identity: cluster identity info (singleton used if omitted)
        """
        self._redis = redis_client
        self._identity = cluster_identity
        self._handlers: dict[str, list[Callable[[GlobalConfigChange], None]]] = {}
        self._running = False
        self._lock = threading.RLock()
        self._pubsub: Any | None = None
        self._listener_thread: threading.Thread | None = None
        self._handle: DaemonWorkerHandle | None = None  # impl 489 D9

        # Lazy initialization
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Perform lazy initialization."""
        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            # Initialize ClusterIdentity
            if self._identity is None:
                try:
                    from baldur.core.cluster_identity import get_cluster_identity

                    self._identity = get_cluster_identity()
                except Exception as e:
                    logger.warning(
                        "global_config_propagator.get_cluster_identity_failed",
                        error=e,
                    )

            # Initialize the Redis client
            if self._redis is None:
                try:
                    from baldur.core.tiered_redis import (
                        RedisScope,
                        TieredRedisProvider,
                    )

                    provider = TieredRedisProvider()
                    self._redis = provider.get_redis(RedisScope.GLOBAL)
                except Exception as e:
                    logger.warning(
                        "global_config_propagator.initialize_redis_failed",
                        error=e,
                    )

            self._initialized = True

    def propagate(self, change: GlobalConfigChange) -> bool:
        """
        Propagate a config change.

        Args:
            change: config change event

        Returns:
            Whether the propagation succeeded
        """
        self._ensure_initialized()

        # Quarantine Mode check
        try:
            from baldur.core.cluster_identity import is_quarantine_mode

            if is_quarantine_mode():
                logger.warning(
                    "global_config_propagator.quarantine_mode_active_skipping"
                )
                return False
        except ImportError:
            pass

        if not self._redis:
            logger.warning(
                "global_config_propagator.redis_available_skipping_propagation"
            )
            return False

        try:
            # Select the channel based on the scope
            if change.scope == ConfigScope.GLOBAL:
                channel = self.GLOBAL_CONFIG_CHANNEL
            elif change.scope == ConfigScope.REGIONAL:
                region = self._identity.region if self._identity else "default"
                channel = self.REGIONAL_CONFIG_CHANNEL_TEMPLATE.format(region=region)
            else:
                # LOCAL needs no propagation
                logger.debug(
                    "global_config_propagator.local_scope_skipping_propagation"
                )
                return True

            # Propagate
            payload = fast_dumps_str(change.to_dict(), default=str)
            subscribers = self._redis.publish(channel, payload)

            logger.info(
                "global_config_propagator.propagated_subscribers_via",
                change=change.config_type,
                config_key=change.config_key,
                subscribers=subscribers,
                channel=channel,
            )
            return True

        except Exception as e:
            logger.exception(
                "global_config_propagator.propagation_failed",
                error=e,
            )
            return False

    def subscribe(
        self, config_type: str, handler: Callable[[GlobalConfigChange], None]
    ) -> None:
        """
        Subscribe to config changes.

        Args:
            config_type: config type to subscribe to (e.g. "circuit_breaker", "dlq")
            handler: change event handler function
        """
        with self._lock:
            if config_type not in self._handlers:
                self._handlers[config_type] = []
            self._handlers[config_type].append(handler)
            logger.debug(
                "global_config_propagator.subscribed_handler",
                config_type=config_type,
            )

    def unsubscribe(
        self, config_type: str, handler: Callable[[GlobalConfigChange], None]
    ) -> None:
        """
        Unsubscribe from config changes.

        Args:
            config_type: config type to unsubscribe from
            handler: handler function to remove
        """
        with self._lock:
            if config_type in self._handlers:
                try:
                    self._handlers[config_type].remove(handler)
                except ValueError:
                    pass

    def start_listener(self) -> None:
        """
        Start the Redis Pub/Sub listener.

        Subscribes to the global/regional channels on a background thread and
        dispatches received config changes to the local handlers.
        """
        self._ensure_initialized()

        if not self._redis:
            logger.warning("global_config_propagator.redis_available_cannot_start")
            return

        with self._lock:
            if self._running:
                return

            self._running = True
            self._pubsub = self._redis.pubsub()

            # Subscribe to the global channel
            channels = [self.GLOBAL_CONFIG_CHANNEL]

            # Also subscribe to the regional channel (if any)
            if self._identity and self._identity.region:
                regional_channel = self.REGIONAL_CONFIG_CHANNEL_TEMPLATE.format(
                    region=self._identity.region
                )
                channels.append(regional_channel)

            self._pubsub.subscribe(*channels)

            from baldur.meta.daemon_worker import DaemonWorkerHandle
            from baldur.metrics.recorders.daemon_worker import (
                register_daemon_worker,
            )

            self._spawn_listener_thread()
            assert self._listener_thread is not None  # spawn always sets non-None
            self._handle = DaemonWorkerHandle(
                thread=self._listener_thread,
                tick_interval_seconds=_TICK_INTERVAL_SECONDS,
                restart_callback=self._spawn_listener_thread,
            )
            register_daemon_worker("GlobalConfigPropagatorListener", self._handle)
            logger.info(
                "global_config_propagator.listener_started_channels",
                channels=channels,
            )

    def _spawn_listener_thread(self) -> None:
        """Construct + start a fresh listener thread (impl 489 D9)."""
        self._listener_thread = threading.Thread(
            target=self._listen_loop_with_crash_capture,
            daemon=True,
            name="GlobalConfigPropagatorListener",
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
        """Stop the Redis Pub/Sub listener."""
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
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=2.0)
            unregister_daemon_worker("GlobalConfigPropagatorListener")
            if self._listener_thread.is_alive():
                logger.critical(
                    "daemon_worker.stop_join_timeout",
                    worker_name="GlobalConfigPropagatorListener",
                    join_timeout_seconds=2.0,
                )
        logger.info("global_config_propagator.listener_stopped")

    def _listen_loop(self) -> None:
        """Redis message receive loop."""
        import time as _time

        while self._running and self._pubsub:
            iter_start = _time.monotonic()
            try:
                message = self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_TICK_INTERVAL_SECONDS
                )
                if message and message["type"] == "message":
                    self._handle_message(message["data"])
            except Exception as e:
                if self._running:
                    logger.exception(
                        "global_config_propagator.listen_error",
                        error=e,
                    )

            if self._handle is not None:
                self._handle.observe_iteration(_time.monotonic() - iter_start)
                self._handle.heartbeat()

    def _handle_message(self, data: str) -> None:
        """Handle a Redis message."""
        try:
            change = GlobalConfigChange.from_dict(fast_loads(data))

            # Ignore messages published by this cluster itself
            if self._identity and change.source_cluster == self._identity.cluster_id:
                logger.debug("global_config_propagator.ignoring_own_message")
                return

            # Invoke the handlers
            handlers = self._handlers.get(change.config_type, [])
            for handler in handlers:
                try:
                    handler(change)
                except Exception as e:
                    logger.exception(
                        "global_config_propagator.handler_error",
                        error=e,
                    )

            logger.info(
                "global_config_propagator.received_config_change",
                change=change.config_type,
                config_key=change.config_key,
                source_cluster=change.source_cluster,
            )

        except Exception as e:
            logger.exception(
                "global_config_propagator.message_parsing_failed",
                error=e,
            )


# =============================================================================
# Singleton
# =============================================================================

_propagator: GlobalConfigPropagator | None = None
_propagator_lock = threading.Lock()


def get_global_config_propagator() -> GlobalConfigPropagator:
    """Return the GlobalConfigPropagator singleton."""
    global _propagator
    if _propagator is None:
        with _propagator_lock:
            if _propagator is None:
                _propagator = GlobalConfigPropagator()
    return _propagator


def reset_global_config_propagator() -> None:
    """Reset the singleton (for tests)."""
    global _propagator
    if _propagator:
        _propagator.stop_listener()
    _propagator = None
