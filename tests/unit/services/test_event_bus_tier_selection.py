"""
EventBus tier selection + RedisEventBus unit tests (doc 389).

Testable units:
- Unified get_event_bus()/reset_event_bus() factory (convenience.py)
- RedisEventBus signature alignment (emit, get_history, subscribe, publish)
- Double-fire prevention (_instance_id + _origin filtering)
- _publish_distributed() TOCTOU safety
- _connect_redis() via RedisConnectionFactory
- _try_reconnect() reconnect behavior
- reset() lifecycle
- RedisEventBusShutdownHandler + integrate_with_shutdown_coordinator()
- EVENT_TYPE_TO_CHANNEL extension (Circuit Mesh, Rollback)
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent
from baldur.services.event_bus.redis_bus import RedisEventBus


def _make_bus() -> RedisEventBus:
    """Create RedisEventBus without Redis connection for testing."""
    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    bus._redis_client = None
    return bus


def _make_bus_with_redis(mock_redis: MagicMock) -> RedisEventBus:
    """Create RedisEventBus with mocked Redis client."""
    with patch.object(RedisEventBus, "_connect_redis", return_value=True):
        bus = RedisEventBus()
    bus._redis_client = mock_redis
    return bus


# =============================================================================
# Contract: RedisEventBus __all__ exports (doc 389)
# =============================================================================


class TestRedisEventBusModuleContract:
    """redis_bus module __all__ contract verification."""

    def test_module_all_exports(self):
        """__all__ contains exactly the 5 specified symbols (doc 389)."""
        from baldur.services.event_bus import redis_bus

        assert set(redis_bus.__all__) == {
            "RedisEventBus",
            "EventChannel",
            "BALDUR_EVENT_CHANNELS",
            "EVENT_TYPE_TO_CHANNEL",
            "CRITICAL_EVENT_TYPES",
        }


class TestRedisEventBusInstanceIdContract:
    """_instance_id contract verification."""

    def test_instance_id_is_full_uuid4_hex(self):
        """_instance_id uses full uuid4().hex (128-bit, 32 hex chars, doc 389)."""
        bus = _make_bus()
        assert len(bus._instance_id) == 32
        assert all(c in "0123456789abcdef" for c in bus._instance_id)

    def test_different_instances_have_unique_ids(self):
        """Each RedisEventBus instance has a unique _instance_id."""
        bus1 = _make_bus()
        bus2 = _make_bus()
        assert bus1._instance_id != bus2._instance_id


class TestReconnectIntervalContract:
    """_RECONNECT_INTERVAL contract verification."""

    def test_reconnect_interval_is_30_seconds(self):
        """_RECONNECT_INTERVAL = 30.0 seconds (doc 389)."""
        assert RedisEventBus._RECONNECT_INTERVAL == 30.0


# =============================================================================
# Contract: EVENT_TYPE_TO_CHANNEL extension (doc 389)
# =============================================================================


class TestEventTypeToChannelExtensionContract:
    """EVENT_TYPE_TO_CHANNEL new mappings from doc 389."""

    def test_circuit_mesh_override_applied_maps_to_circuit_breaker(self):
        """CIRCUIT_MESH_OVERRIDE_APPLIED -> CIRCUIT_BREAKER (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.CIRCUIT_MESH_OVERRIDE_APPLIED]
            == EventChannel.CIRCUIT_BREAKER
        )

    def test_circuit_mesh_override_expired_maps_to_circuit_breaker(self):
        """CIRCUIT_MESH_OVERRIDE_EXPIRED -> CIRCUIT_BREAKER (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.CIRCUIT_MESH_OVERRIDE_EXPIRED]
            == EventChannel.CIRCUIT_BREAKER
        )

    def test_circuit_mesh_escalation_triggered_maps_to_global(self):
        """CIRCUIT_MESH_ESCALATION_TRIGGERED -> GLOBAL (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.CIRCUIT_MESH_ESCALATION_TRIGGERED]
            == EventChannel.GLOBAL
        )

    def test_rollback_requested_maps_to_global(self):
        """ROLLBACK_REQUESTED -> GLOBAL (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.ROLLBACK_REQUESTED] == EventChannel.GLOBAL
        )

    def test_rollback_completed_maps_to_global(self):
        """ROLLBACK_COMPLETED -> GLOBAL (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert (
            EVENT_TYPE_TO_CHANNEL[EventType.ROLLBACK_COMPLETED] == EventChannel.GLOBAL
        )

    def test_rollback_failed_maps_to_global(self):
        """ROLLBACK_FAILED -> GLOBAL (doc 389)."""
        from baldur.services.event_bus.redis_bus import (
            EVENT_TYPE_TO_CHANNEL,
            EventChannel,
        )

        assert EVENT_TYPE_TO_CHANNEL[EventType.ROLLBACK_FAILED] == EventChannel.GLOBAL


# =============================================================================
# Behavior: Unified factory get_event_bus() / reset_event_bus()
# =============================================================================


class TestUnifiedFactoryBehavior:
    """Unified get_event_bus() factory behavior."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Reset event bus singleton before each test."""
        from baldur.services.event_bus.bus.convenience import reset_event_bus

        reset_event_bus(cleanup=False)
        yield
        reset_event_bus(cleanup=False)

    def test_memory_backend_returns_baldur_event_bus(self):
        """backend='memory' returns BaldurEventBus instance."""
        from baldur.services.event_bus.bus.convenience import get_event_bus
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")
            bus = get_event_bus()

        assert isinstance(bus, BaldurEventBus)

    def test_redis_backend_returns_redis_event_bus(self):
        """backend='redis' returns RedisEventBus instance."""
        from baldur.services.event_bus.bus.convenience import get_event_bus

        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_settings,
            patch.object(
                RedisEventBus,
                "_connect_redis",
                return_value=False,
            ),
            patch.object(
                RedisEventBus,
                "start_listener",
            ),
        ):
            mock_settings.return_value = MagicMock(backend="redis")
            bus = get_event_bus()

        assert isinstance(bus, RedisEventBus)

    def test_singleton_returns_same_instance(self):
        """get_event_bus() returns the same instance on repeated calls."""
        from baldur.services.event_bus.bus.convenience import get_event_bus

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")
            first = get_event_bus()
            second = get_event_bus()

        assert first is second

    def test_reset_clears_singleton(self):
        """reset_event_bus() clears the cached instance."""
        from baldur.services.event_bus.bus.convenience import (
            get_event_bus,
            reset_event_bus,
        )

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")
            first = get_event_bus()
            reset_event_bus()
            second = get_event_bus()

        assert first is not second

    def test_reset_calls_bus_reset(self):
        """reset_event_bus() calls reset() on the existing bus."""
        from baldur.services.event_bus.bus.convenience import (
            get_event_bus,
            reset_event_bus,
        )

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")
            bus = get_event_bus()

        with patch.object(bus, "reset") as mock_reset:
            reset_event_bus()
            mock_reset.assert_called_once()

    def test_concurrent_get_returns_same_instance(self):
        """Multi-threaded get_event_bus() returns the same instance."""
        from baldur.services.event_bus.bus.convenience import get_event_bus

        results = []

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")

            def worker():
                results.append(get_event_bus())

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert all(r is results[0] for r in results)


# =============================================================================
# Behavior: RedisEventBus signature alignment
# =============================================================================


class TestRedisEventBusSignatureAlignmentBehavior:
    """RedisEventBus Protocol-compatible signature verification."""

    def test_subscribe_returns_event_subscription(self):
        """subscribe() returns EventSubscription (not bool)."""
        from baldur.services.event_bus.bus.models import EventSubscription

        bus = _make_bus()
        sub = bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)
        assert isinstance(sub, EventSubscription)

    def test_publish_returns_int(self):
        """publish() returns int (handler count)."""
        bus = _make_bus()
        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
        )
        result = bus.publish(event)
        assert isinstance(result, int)

    def test_emit_returns_int(self):
        """emit() returns int (handler count)."""
        bus = _make_bus()
        result = bus.emit(EventType.CONFIG_UPDATED, {}, source="test")
        assert isinstance(result, int)

    def test_emit_creates_event_and_publishes(self):
        """emit() creates event via create_event() and fires local handlers."""
        bus = _make_bus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        count = bus.emit(
            EventType.CONFIG_UPDATED,
            {"key": "v"},
            source="test",
            priority=EventPriority.HIGH,
        )

        assert count == 1
        assert received[0].event_type == EventType.CONFIG_UPDATED
        assert received[0].priority == EventPriority.HIGH

    def test_get_history_delegates_to_local_bus(self):
        """get_history() returns local bus history."""
        bus = _make_bus()
        bus.emit(EventType.CONFIG_UPDATED, {"key": "v"}, source="test")

        history = bus.get_history()
        assert len(history) == 1
        assert history[0]["event_type"] == EventType.CONFIG_UPDATED.value

    def test_get_history_with_event_type_filter(self):
        """get_history() filters by event_type."""
        bus = _make_bus()
        bus.emit(EventType.CONFIG_UPDATED, {}, source="test")
        bus.emit(EventType.EMERGENCY_ACTIVATED, {}, source="test")

        history = bus.get_history(event_type=EventType.CONFIG_UPDATED)
        assert len(history) == 1

    def test_get_history_with_limit(self):
        """get_history() respects limit parameter."""
        bus = _make_bus()
        for i in range(5):
            bus.emit(EventType.CONFIG_UPDATED, {"i": i}, source="test")

        history = bus.get_history(limit=3)
        assert len(history) == 3

    def test_unsubscribe_removes_handler(self):
        """unsubscribe() removes handler and returns True."""
        bus = _make_bus()
        handler = lambda e: None  # noqa: E731
        bus.subscribe(EventType.CONFIG_UPDATED, handler)
        result = bus.unsubscribe(EventType.CONFIG_UPDATED, handler)
        assert result is True


# =============================================================================
# Behavior: Double-fire prevention (_origin filtering)
# =============================================================================


class TestDoubleFirePreventionBehavior:
    """Self-message filtering in _handle_redis_message()."""

    def test_self_originated_message_is_skipped(self):
        """Message with matching _origin is not published to local bus."""
        bus = _make_bus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        # Simulate a Redis message from this same instance
        from baldur.utils.serialization import fast_dumps_str

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={"key": "v"},
            source="test",
        )
        event_dict = event.to_dict()
        event_dict["_origin"] = bus._instance_id
        data = fast_dumps_str(event_dict, default=str)

        bus._handle_redis_message(data)
        assert len(received) == 0

    def test_remote_message_is_published_to_local_bus(self):
        """Message with different _origin is published to local bus."""
        bus = _make_bus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        from baldur.utils.serialization import fast_dumps_str

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={"key": "v"},
            source="remote",
        )
        event_dict = event.to_dict()
        event_dict["_origin"] = "different-instance-id"
        data = fast_dumps_str(event_dict, default=str)

        bus._handle_redis_message(data)
        assert len(received) == 1
        assert received[0].source == "remote"

    def test_message_without_origin_is_published(self):
        """Message without _origin field is published to local bus."""
        bus = _make_bus()
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))

        from baldur.utils.serialization import fast_dumps_str

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="legacy",
        )
        data = fast_dumps_str(event.to_dict(), default=str)

        bus._handle_redis_message(data)
        assert len(received) == 1


# =============================================================================
# Behavior: _publish_distributed() TOCTOU safety + _origin injection
# =============================================================================


class TestPublishDistributedBehavior:
    """_publish_distributed() behavior verification."""

    def test_injects_origin_into_event_dict(self):
        """_publish_distributed() injects _origin = _instance_id into event data."""
        mock_redis = MagicMock()
        bus = _make_bus_with_redis(mock_redis)

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={"key": "v"},
            source="test",
        )

        bus._publish_distributed(event)

        # Verify _origin is in the serialized data
        call_args = mock_redis.publish.call_args
        from baldur.utils.serialization import fast_loads

        published_data = fast_loads(call_args[0][1])
        assert published_data["_origin"] == bus._instance_id

    def test_toctou_safety_with_concurrent_redis_none(self):
        """_publish_distributed() uses local ref snapshot for TOCTOU safety."""
        bus = _make_bus()
        # _redis_client is None — should not raise AttributeError
        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
        )
        # Should not raise
        bus._publish_distributed(event)

    def test_redis_failure_triggers_kafka_for_critical(self):
        """Redis publish failure + critical event triggers Kafka fallback."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )

        with patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka:
            bus._publish_distributed(event)
            mock_kafka.assert_called_once_with(event)

    def test_redis_failure_no_kafka_for_non_critical(self):
        """Redis publish failure + non-critical event does not trigger Kafka."""
        mock_redis = MagicMock()
        mock_redis.publish.side_effect = Exception("Redis down")
        bus = _make_bus_with_redis(mock_redis)

        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
        )

        with patch.object(bus, "_publish_to_kafka_fallback") as mock_kafka:
            bus._publish_distributed(event)
            mock_kafka.assert_not_called()


# =============================================================================
# Behavior: _connect_redis() via RedisConnectionFactory
# =============================================================================


class TestConnectRedisBehavior:
    """_connect_redis() behavior with RedisConnectionFactory."""

    def test_uses_eventbus_redis_url_when_set(self):
        """Prefers EventBusSettings.redis_url over RedisSettings.url."""
        mock_factory = MagicMock()
        mock_client = MagicMock()
        mock_factory.create.return_value = mock_client

        bus = _make_bus()
        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_eb_settings,
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
            ) as mock_redis_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=mock_factory,
            ),
        ):
            mock_eb_settings.return_value = MagicMock(
                redis_url="redis://dedicated:6379/0"
            )
            mock_redis_settings.return_value = MagicMock(url="redis://shared:6379/0")
            result = bus._connect_redis()

        assert result is True
        mock_factory.create.assert_called_once_with(
            "redis://dedicated:6379/0", decode_responses=True
        )

    def test_falls_back_to_redis_settings_url(self):
        """Falls back to RedisSettings.url when EventBusSettings.redis_url is None."""
        mock_factory = MagicMock()
        mock_client = MagicMock()
        mock_factory.create.return_value = mock_client

        bus = _make_bus()
        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_eb_settings,
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
            ) as mock_redis_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=mock_factory,
            ),
        ):
            mock_eb_settings.return_value = MagicMock(redis_url=None)
            mock_redis_settings.return_value = MagicMock(url="redis://shared:6379/0")
            result = bus._connect_redis()

        assert result is True
        mock_factory.create.assert_called_once_with(
            "redis://shared:6379/0", decode_responses=True
        )

    def test_returns_false_when_no_url_configured(self):
        """Returns False when both redis_url and RedisSettings.url are empty."""
        bus = _make_bus()
        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_eb_settings,
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
            ) as mock_redis_settings,
        ):
            mock_eb_settings.return_value = MagicMock(redis_url=None)
            mock_redis_settings.return_value = MagicMock(url=None)
            result = bus._connect_redis()

        assert result is False

    def test_returns_false_on_connection_failure(self):
        """Returns False and sets _redis_client=None on connection failure."""
        mock_factory = MagicMock()
        mock_factory.create.side_effect = Exception("Connection refused")

        bus = _make_bus()
        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_eb_settings,
            patch(
                "baldur.settings.redis.get_redis_settings",
                autospec=True,
            ) as mock_redis_settings,
            patch(
                "baldur.adapters.redis.connection_factory.get_redis_connection_factory",
                return_value=mock_factory,
            ),
        ):
            mock_eb_settings.return_value = MagicMock(redis_url="redis://bad:6379")
            mock_redis_settings.return_value = MagicMock(url=None)
            result = bus._connect_redis()

        assert result is False
        assert bus._redis_client is None


# =============================================================================
# Behavior: _try_reconnect()
# =============================================================================


class TestTryReconnectBehavior:
    """_try_reconnect() behavior verification."""

    def test_already_connected_returns_true(self):
        """When _redis_client is not None, returns True immediately."""
        mock_redis = MagicMock()
        bus = _make_bus_with_redis(mock_redis)
        assert bus._try_reconnect() is True

    def test_reconnect_success_sets_up_pubsub(self):
        """Successful reconnect calls _setup_pubsub()."""
        bus = _make_bus()

        def fake_connect():
            bus._redis_client = MagicMock()
            return True

        with (
            patch.object(bus, "_connect_redis", side_effect=fake_connect),
            patch.object(bus, "_setup_pubsub") as mock_setup,
        ):
            result = bus._try_reconnect()

        assert result is True
        mock_setup.assert_called_once()

    def test_reconnect_failure_returns_false(self):
        """Failed reconnect returns False."""
        bus = _make_bus()
        with patch.object(bus, "_connect_redis", return_value=False):
            result = bus._try_reconnect()
        assert result is False


# =============================================================================
# Behavior: RedisEventBus.reset()
# =============================================================================


class TestRedisEventBusResetBehavior:
    """RedisEventBus.reset() lifecycle verification."""

    def test_reset_stops_listener_and_resets_local_bus(self):
        """reset() calls stop_listener() and _local_bus.reset()."""
        bus = _make_bus()
        with (
            patch.object(bus, "stop_listener") as mock_stop,
            patch.object(bus._local_bus, "reset") as mock_local_reset,
        ):
            bus.reset()

        mock_stop.assert_called_once()
        mock_local_reset.assert_called_once()


# =============================================================================
# Behavior: RedisEventBusShutdownHandler
# =============================================================================


class TestRedisEventBusShutdownHandlerBehavior:
    """RedisEventBusShutdownHandler behavior verification."""

    def test_on_shutdown_start_sets_running_false(self):
        """on_shutdown_start() sets _running = False."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        bus._running = True
        handler = RedisEventBusShutdownHandler(bus)

        handler.on_shutdown_start()
        assert bus._running is False

    def test_is_drain_complete_when_no_thread(self):
        """is_drain_complete() returns True when no listener thread."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        bus._listener_thread = None
        handler = RedisEventBusShutdownHandler(bus)

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_when_thread_dead(self):
        """is_drain_complete() returns True when listener thread is not alive."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        bus._listener_thread = mock_thread
        handler = RedisEventBusShutdownHandler(bus)

        assert handler.is_drain_complete() is True

    def test_is_drain_complete_when_thread_alive(self):
        """is_drain_complete() returns False when listener thread is alive."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        bus._listener_thread = mock_thread
        handler = RedisEventBusShutdownHandler(bus)

        assert handler.is_drain_complete() is False

    def test_on_drain_complete_calls_stop_listener(self):
        """on_drain_complete() calls stop_listener()."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        handler = RedisEventBusShutdownHandler(bus)

        with patch.object(bus, "stop_listener") as mock_stop:
            handler.on_drain_complete()
            mock_stop.assert_called_once()

    def test_on_force_shutdown_calls_stop_listener(self):
        """on_force_shutdown() calls stop_listener()."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        bus = _make_bus()
        handler = RedisEventBusShutdownHandler(bus)

        with patch.object(bus, "stop_listener") as mock_stop:
            handler.on_force_shutdown([])
            mock_stop.assert_called_once()

    def test_shutdown_handler_inherits_shutdown_handler_abc(self):
        """RedisEventBusShutdownHandler is a ShutdownHandler subclass."""
        from baldur.core.shutdown_coordinator import ShutdownHandler
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
        )

        assert issubclass(RedisEventBusShutdownHandler, ShutdownHandler)


# =============================================================================
# Behavior: integrate_with_shutdown_coordinator()
# =============================================================================


class TestIntegrateWithShutdownCoordinatorBehavior:
    """integrate_with_shutdown_coordinator() factory behavior."""

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Reset event bus singleton before each test."""
        from baldur.services.event_bus.bus.convenience import reset_event_bus

        reset_event_bus(cleanup=False)
        yield
        reset_event_bus(cleanup=False)

    def test_returns_handler_when_redis_bus(self):
        """Returns RedisEventBusShutdownHandler when bus is RedisEventBus."""
        from baldur.services.event_bus.redis_bus import (
            RedisEventBusShutdownHandler,
            integrate_with_shutdown_coordinator,
        )

        with (
            patch(
                "baldur.settings.event_bus.get_event_bus_settings",
                autospec=True,
            ) as mock_settings,
            patch.object(RedisEventBus, "_connect_redis", return_value=False),
            patch.object(RedisEventBus, "start_listener"),
        ):
            mock_settings.return_value = MagicMock(backend="redis")
            handler = integrate_with_shutdown_coordinator()

        assert isinstance(handler, RedisEventBusShutdownHandler)

    def test_returns_none_when_memory_bus(self):
        """Returns None when bus is BaldurEventBus."""
        from baldur.services.event_bus.redis_bus import (
            integrate_with_shutdown_coordinator,
        )

        with patch(
            "baldur.settings.event_bus.get_event_bus_settings",
            autospec=True,
        ) as mock_settings:
            mock_settings.return_value = MagicMock(backend="memory")
            handler = integrate_with_shutdown_coordinator()

        assert handler is None


# =============================================================================
# Behavior: Kafka fallback uses singleton producer
# =============================================================================


class TestKafkaFallbackSingletonBehavior:
    """_publish_to_kafka_fallback() uses get_kafka_producer() singleton."""

    @pytest.fixture(autouse=True)
    def _require_dormant(self):
        pytest.importorskip("baldur_dormant")

    def test_publish_success_does_not_trigger_wal(self):
        """Kafka publish success (True) does not trigger WAL."""
        bus = _make_bus()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )

        mock_producer = MagicMock()
        mock_producer.publish.return_value = True

        with (
            patch(
                "baldur_dormant.adapters.kafka.producer.get_kafka_producer",
                return_value=mock_producer,
            ),
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus._publish_to_kafka_fallback(event)

        mock_producer.publish.assert_called_once_with(
            topic="baldur.routing.events",
            event=event.to_dict(),
        )
        mock_wal.assert_not_called()

    def test_publish_failure_triggers_wal(self):
        """Kafka publish failure (False) triggers WAL fallback."""
        bus = _make_bus()
        event = BaldurEvent(
            event_type=EventType.REGION_PRIMARY_CHANGED,
            data={},
            source="test",
        )

        mock_producer = MagicMock()
        mock_producer.publish.return_value = False

        with (
            patch(
                "baldur_dormant.adapters.kafka.producer.get_kafka_producer",
                return_value=mock_producer,
            ),
            patch.object(bus, "_write_to_wal") as mock_wal,
        ):
            bus._publish_to_kafka_fallback(event)

        mock_wal.assert_called_once_with(event)


# =============================================================================
# Behavior: start_listener idempotency
# =============================================================================


class TestStartListenerIdempotencyBehavior:
    """start_listener() idempotency verification."""

    def test_double_start_listener_does_not_create_second_thread(self):
        """Calling start_listener() twice does not start a second thread."""
        bus = _make_bus()
        bus.start_listener()
        first_thread = bus._listener_thread
        bus.start_listener()
        assert bus._listener_thread is first_thread
        bus.stop_listener()
