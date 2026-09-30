"""``publish_local`` — an observed transition reaches this process's handlers only.

Target: ``BaldurEventBus.publish_local`` and ``RedisEventBus.publish_local`` —
System Control announces a kill-switch transition it observed (rather than
made) to its own process's subscribers. On the in-memory bus that is
``publish``; on the Redis bus it must never reach the channel, or every peer
would receive a second copy of a transition it observes itself.

Verification techniques applied (§8):
  - §8.5 Dependency interaction — the Redis client is never called by a local
    publish, and is called by a distributed one (the control)
  - §8.4 Side effects — local handlers run, the handler count is returned
"""

from __future__ import annotations

from unittest.mock import create_autospec, patch

import redis

from baldur.services.event_bus.bus.event_bus import BaldurEventBus
from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.bus.models import BaldurEvent, create_event
from baldur.services.event_bus.redis_bus import RedisEventBus


def _kill_switch_event() -> BaldurEvent:
    return create_event(
        EventType.KILL_SWITCH_ACTIVATED,
        {"reason": "observed_state_change", "activated_by": "system_control"},
        "system_control",
    )


def _redis_bus_with_client() -> tuple[RedisEventBus, redis.Redis]:
    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    client = create_autospec(redis.Redis, instance=True)
    bus._redis_client = client
    return bus, client


class TestPublishLocalBehavior:
    """Local handlers run; nothing leaves the process."""

    def test_in_memory_publish_local_runs_this_process_handlers(self):
        """The in-memory bus never leaves the process: publish_local is publish."""
        bus = BaldurEventBus()
        received: list[BaldurEvent] = []
        bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, received.append)
        event = _kill_switch_event()

        try:
            called = bus.publish_local(event)
        finally:
            bus.reset()

        assert called == 1
        assert received == [event]

    def test_redis_publish_local_runs_local_handlers_without_touching_redis(self):
        """The observed transition reaches this process only."""
        # Given
        bus, client = _redis_bus_with_client()
        received: list[BaldurEvent] = []
        bus.subscribe(EventType.KILL_SWITCH_ACTIVATED, received.append)
        event = _kill_switch_event()

        # When
        called = bus.publish_local(event)

        # Then
        assert called == 1
        assert received == [event]
        client.publish.assert_not_called()

    def test_redis_publish_reaches_the_channel(self):
        """Control: a transition this process made is distributed."""
        bus, client = _redis_bus_with_client()

        bus.publish(_kill_switch_event())

        client.publish.assert_called_once()
