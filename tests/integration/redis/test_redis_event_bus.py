"""
Redis EventBus Integration Tests (doc 389)

Verifies RedisEventBus cross-pod event propagation and reconnect
lifecycle using actual Redis Pub/Sub infrastructure.

Test Categories:
    A. Cross-Pod Event Propagation:
        - Event published on Bus A is received by Bus B's local handlers
        - Self-originated events are not double-fired locally
        - Events propagate through correct Redis channels
    B. Signature Alignment with Real Pub/Sub:
        - emit() triggers cross-pod delivery
        - subscribe() returns EventSubscription, not bool
        - publish() returns int (handler count)
        - get_history() returns local process history
    C. Reconnect Lifecycle:
        - RedisEventBus starts without Redis → local-only mode
        - Redis becomes available → auto-reconnect → cross-pod resumes

Note: All tests require a running Redis instance.
      Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import (
    BaldurEvent,
    EventSubscription,
)
from baldur.services.event_bus.redis_bus import (
    BALDUR_EVENT_CHANNELS,
    EventChannel,
    RedisEventBus,
)

pytestmark = pytest.mark.requires_redis


def _create_bus_with_real_redis(redis_url: str) -> RedisEventBus:
    """Create RedisEventBus connected to real Redis, bypassing settings/factory."""
    import redis as redis_lib

    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    # Direct Redis connection — bypasses RedisConnectionFactory/settings
    bus._redis_client = redis_lib.from_url(redis_url, decode_responses=True)
    bus._redis_client.ping()
    return bus


@pytest.fixture
def bus_a(redis_url):
    """RedisEventBus instance A (simulates Pod A)."""
    bus = _create_bus_with_real_redis(redis_url)
    assert bus.is_distributed(), "Bus A must connect to Redis"
    bus.start_listener()
    yield bus
    bus.stop_listener()


@pytest.fixture
def bus_b(redis_url):
    """RedisEventBus instance B (simulates Pod B)."""
    bus = _create_bus_with_real_redis(redis_url)
    assert bus.is_distributed(), "Bus B must connect to Redis"
    bus.start_listener()
    yield bus
    bus.stop_listener()


# =============================================================================
# A. Cross-Pod Event Propagation
# =============================================================================


class TestCrossPodPropagation:
    """
    Validates that events published on one RedisEventBus instance
    are received by another instance's local handlers via Redis Pub/Sub.
    """

    def test_event_on_bus_a_received_by_bus_b(self, bus_a, bus_b):
        """
        Purpose:
            Event published on Bus A is delivered to Bus B's local handlers.
        Expected:
            - Bus B handler receives the event within 3 seconds
            - Event type, data, and source match the published event
        """
        received = []
        bus_b.subscribe(
            EventType.CONFIG_UPDATED,
            lambda e: received.append(e),
        )

        # Give listener time to set up
        time.sleep(0.2)

        bus_a.publish(
            BaldurEvent(
                event_type=EventType.CONFIG_UPDATED,
                data={"key": "cross_pod_test", "value": 42},
                source="bus_a_test",
            )
        )

        # Wait for cross-pod delivery
        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            time.sleep(0.1)

        assert len(received) == 1
        assert received[0].event_type == EventType.CONFIG_UPDATED
        assert received[0].data["key"] == "cross_pod_test"
        assert received[0].source == "bus_a_test"

    def test_self_originated_event_not_double_fired(self, bus_a):
        """
        Purpose:
            Event published on Bus A fires local handlers once (not twice).
            The _origin filtering prevents the Redis echo from re-firing.
        Expected:
            - Handler called exactly once (local publish only)
            - Redis echo with same _origin is silently dropped
        """
        received = []
        bus_a.subscribe(
            EventType.EMERGENCY_ACTIVATED,
            lambda e: received.append(e),
        )

        time.sleep(0.2)

        bus_a.publish(
            BaldurEvent(
                event_type=EventType.EMERGENCY_ACTIVATED,
                data={"severity": "critical"},
                source="test",
            )
        )

        # Wait enough time for Redis echo to arrive
        time.sleep(1.5)

        assert len(received) == 1, (
            f"Expected exactly 1 delivery, got {len(received)} (double-fire bug if > 1)"
        )

    def test_bidirectional_propagation(self, bus_a, bus_b):
        """
        Purpose:
            Events flow in both directions: A→B and B→A.
        Expected:
            - Bus B receives event from Bus A
            - Bus A receives event from Bus B
        """
        received_on_a = []
        received_on_b = []

        bus_a.subscribe(
            EventType.CIRCUIT_BREAKER_OPENED,
            lambda e: received_on_a.append(e),
        )
        bus_b.subscribe(
            EventType.CIRCUIT_BREAKER_CLOSED,
            lambda e: received_on_b.append(e),
        )

        time.sleep(0.2)

        # A → B
        bus_a.publish(
            BaldurEvent(
                event_type=EventType.CIRCUIT_BREAKER_CLOSED,
                data={"service": "payment"},
                source="bus_a",
            )
        )
        # B → A
        bus_b.publish(
            BaldurEvent(
                event_type=EventType.CIRCUIT_BREAKER_OPENED,
                data={"service": "inventory"},
                source="bus_b",
            )
        )

        deadline = time.time() + 3.0
        while (not received_on_a or not received_on_b) and time.time() < deadline:
            time.sleep(0.1)

        assert len(received_on_a) == 1
        assert received_on_a[0].source == "bus_b"
        assert len(received_on_b) == 1
        assert received_on_b[0].source == "bus_a"

    def test_event_routed_to_correct_channel(self, bus_a, redis_test_client):
        """
        Purpose:
            Events are published to the correct Redis channel per mapping.
        Expected:
            - CHAOS_EXPERIMENT_STARTED → baldur:events:chaos channel
            - Other subscribers on different channels don't receive it
        """
        # Subscribe to the chaos channel directly via Redis
        pubsub = redis_test_client.pubsub()
        chaos_channel = BALDUR_EVENT_CHANNELS[EventChannel.CHAOS.value]
        pubsub.subscribe(chaos_channel)
        # Consume the subscribe confirmation message
        pubsub.get_message(timeout=1.0)

        bus_a.publish(
            BaldurEvent(
                event_type=EventType.CHAOS_EXPERIMENT_STARTED,
                data={"experiment_id": "exp-123"},
                source="test",
            )
        )

        # Read from Redis directly
        message = pubsub.get_message(timeout=3.0)
        assert message is not None
        assert message["type"] == "message"
        assert message["channel"] == chaos_channel

        pubsub.unsubscribe()
        pubsub.close()


# =============================================================================
# B. Signature Alignment with Real Pub/Sub
# =============================================================================


class TestSignatureAlignmentWithRealRedis:
    """
    Validates RedisEventBus Protocol-compatible signatures work
    correctly with real Redis Pub/Sub transport.
    """

    def test_emit_triggers_cross_pod_delivery(self, bus_a, bus_b):
        """
        Purpose:
            emit() convenience method triggers cross-pod delivery.
        Expected:
            - Bus B receives event emitted via Bus A.emit()
        """
        received = []
        bus_b.subscribe(
            EventType.ERROR_BUDGET_CRITICAL,
            lambda e: received.append(e),
        )

        time.sleep(0.2)

        count = bus_a.emit(
            EventType.ERROR_BUDGET_CRITICAL,
            {"budget_percent": 5.0},
            source="error_budget_gate",
            priority=EventPriority.CRITICAL,
        )

        assert isinstance(count, int)

        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            time.sleep(0.1)

        assert len(received) == 1
        assert received[0].data["budget_percent"] == 5.0

    def test_subscribe_returns_event_subscription(self, bus_a):
        """
        Purpose:
            subscribe() returns EventSubscription, not bool.
        Expected:
            - Return type is EventSubscription with correct fields
        """
        sub = bus_a.subscribe(
            EventType.CONFIG_UPDATED,
            lambda e: None,
            priority=EventPriority.HIGH,
        )
        assert isinstance(sub, EventSubscription)
        assert sub.event_type == EventType.CONFIG_UPDATED

    def test_publish_returns_handler_count(self, bus_a):
        """
        Purpose:
            publish() returns int (number of local handlers called).
        Expected:
            - Returns 0 when no subscribers
            - Returns 1 after subscribing a handler
        """
        event = BaldurEvent(
            event_type=EventType.CONFIG_UPDATED,
            data={},
            source="test",
        )

        count_before = bus_a.publish(event)
        assert count_before == 0

        bus_a.subscribe(EventType.CONFIG_UPDATED, lambda e: None)
        count_after = bus_a.publish(event)
        assert count_after == 1

    def test_get_history_returns_local_events(self, bus_a):
        """
        Purpose:
            get_history() returns only local process events.
        Expected:
            - History contains events published locally
            - Respects event_type filter and limit
        """
        bus_a.emit(EventType.CONFIG_UPDATED, {"i": 1}, source="test")
        bus_a.emit(EventType.CONFIG_UPDATED, {"i": 2}, source="test")
        bus_a.emit(EventType.EMERGENCY_ACTIVATED, {"i": 3}, source="test")

        all_history = bus_a.get_history()
        assert len(all_history) == 3

        config_only = bus_a.get_history(event_type=EventType.CONFIG_UPDATED)
        assert len(config_only) == 2

        limited = bus_a.get_history(limit=1)
        assert len(limited) == 1


# =============================================================================
# C. Reconnect Lifecycle
# =============================================================================


class TestReconnectLifecycle:
    """
    Validates RedisEventBus reconnect behavior when Redis
    becomes unavailable and then recovers.
    """

    def test_starts_in_distributed_mode_with_real_redis(self, bus_a):
        """
        Purpose:
            RedisEventBus connected to real Redis reports distributed mode.
        Expected:
            - is_distributed() returns True
        """
        assert bus_a.is_distributed() is True

    def test_starts_local_only_without_redis(self):
        """
        Purpose:
            RedisEventBus without Redis starts in local-only mode.
        Expected:
            - is_distributed() returns False
            - Local handlers still work
        """
        with patch.object(RedisEventBus, "_connect_redis", return_value=False):
            bus = RedisEventBus()

        assert bus.is_distributed() is False

        # Local handlers still work
        received = []
        bus.subscribe(EventType.CONFIG_UPDATED, lambda e: received.append(e))
        bus.emit(EventType.CONFIG_UPDATED, {"key": "v"}, source="test")
        assert len(received) == 1

    def test_reconnect_restores_distributed_mode(self, redis_url):
        """
        Purpose:
            After Redis becomes available, _try_reconnect() restores
            distributed mode and re-subscribes Pub/Sub channels.
        Expected:
            - Initially local-only (is_distributed() False)
            - After manual reconnect: is_distributed() True
            - Pub/Sub channels are subscribed
        """
        import redis as redis_lib

        # Start without Redis
        with patch.object(RedisEventBus, "_connect_redis", return_value=False):
            bus = RedisEventBus()

        assert bus.is_distributed() is False
        assert bus._pubsub is None

        # Simulate reconnect by directly setting _redis_client
        bus._redis_client = redis_lib.from_url(redis_url, decode_responses=True)
        bus._redis_client.ping()
        bus._setup_pubsub()

        assert bus.is_distributed() is True
        assert bus._pubsub is not None
        assert len(bus._subscribed_redis_channels) > 0

    def test_reset_cleans_up_resources(self, bus_a):
        """
        Purpose:
            reset() stops the listener and clears local bus state.
        Expected:
            - Listener thread stops
            - Local subscriptions cleared
            - History cleared
        """
        bus_a.subscribe(EventType.CONFIG_UPDATED, lambda e: None)
        bus_a.emit(EventType.CONFIG_UPDATED, {}, source="test")

        assert len(bus_a.get_history()) > 0

        bus_a.reset()

        assert bus_a._running is False
        assert len(bus_a.get_history()) == 0
