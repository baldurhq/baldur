"""Real-``fork()`` node for the RedisEventBus fork guards (747 D3/D11).

The unit suite owns the per-branch matrix against a simulated inherited state.
It cannot own the single property the stop-path guard exists for: that the
**parent** is undisturbed by what the child does. That claim is about one real
TCP connection shared across two OS processes and a real Redis server's
subscription table, so no in-process seam can assert it.

Before the guard, a fork child's ``stop_listener()`` — reached through the
inherited shutdown handler and the inherited signal handlers, in a child that
never revived anything — issued ``unsubscribe()`` on the parent's connection.
The Redis server dropped the PARENT from all six channels, and because an
unsubscribe confirmation is not an error, the parent's listen loop never entered
its reconnect path: permanent, silent deafness.

Linux only: Windows has no ``os.fork()``, and macOS forbids the pattern this
exercises. The public CI runs the integration job on ubuntu with a redis service.
"""

from __future__ import annotations

import os
import threading

import pytest

from baldur.services.event_bus.bus.event_types import EventType
from baldur.services.event_bus.bus.models import BaldurEvent
from baldur.services.event_bus.redis_bus import RedisEventBus

pytestmark = [
    pytest.mark.requires_redis,
    pytest.mark.skipif(
        not hasattr(os, "fork"), reason="os.fork() is POSIX-only (Windows dev host)"
    ),
]

# Generous ceilings — every assertion is edge-triggered on an Event or a waitpid,
# so these bound a hang rather than pace the test.
_CHILD_EXIT_TIMEOUT_SECONDS = 15.0
_DELIVERY_TIMEOUT_SECONDS = 10.0
_SUBSCRIBE_SETTLE_TIMEOUT_SECONDS = 5.0


def _create_bus_with_real_redis(redis_url: str) -> RedisEventBus:
    """Bus wired straight to Redis, bypassing the settings/factory chain."""
    from unittest.mock import patch

    import redis as redis_lib

    with patch.object(RedisEventBus, "_connect_redis", return_value=False):
        bus = RedisEventBus()
    bus._redis_client = redis_lib.from_url(redis_url, decode_responses=True)
    bus._redis_client.ping()
    return bus


def _wait_for_subscription(bus: RedisEventBus) -> None:
    """Block until the server confirms this bus's subscriptions.

    ``_setup_pubsub()`` subscribes synchronously, but the server-side
    subscription count is what a publisher actually sees, so poll that.
    """
    channels = list(bus._subscribed_redis_channels)
    assert channels
    deadline = threading.Event()
    for _ in range(int(_SUBSCRIBE_SETTLE_TIMEOUT_SECONDS * 100)):
        counts = bus._redis_client.execute_command("PUBSUB", "NUMSUB", *channels)
        # Flat [channel, count, channel, count, ...]
        if all(int(counts[i]) >= 1 for i in range(1, len(counts), 2)):
            return
        deadline.wait(0.01)
    raise AssertionError("Redis never confirmed the parent's subscriptions")


class TestParentSurvivesChildForkLifecycle:
    """747 D3/D11 across a real process boundary."""

    @pytest.mark.parametrize(
        "child_lifecycle",
        ["stop_only", "revive_then_stop"],
        ids=["never_revived_child", "revived_child"],
    )
    def test_parent_still_receives_events_after_the_childs_lifecycle(
        self, redis_url, child_lifecycle
    ):
        """The child runs an inherited lifecycle and exits; the parent's
        subscription must be intact afterwards.

        ``stop_only`` is the pre-fix parent-killer verbatim: a child that never
        revived anything, reaching ``stop_listener()`` through the inherited
        shutdown handler, takes the abandonment branch because ``_origin_pid``
        still names the parent. ``revive_then_stop`` is the ordinary worker
        lifecycle — the repair claims the pid, so the stop is a normal teardown
        of the child's *own* connection, which must equally leave the parent
        alone.
        """
        # Given a listening parent and a separate publisher.
        parent_bus = _create_bus_with_real_redis(redis_url)
        publisher = _create_bus_with_real_redis(redis_url)
        parent_bus.start_listener()
        self_addressed = parent_bus._instance_id
        try:
            _wait_for_subscription(parent_bus)

            received = threading.Event()
            delivered: list[BaldurEvent] = []

            def _handler(event: BaldurEvent) -> None:
                delivered.append(event)
                received.set()

            parent_bus.subscribe(EventType.CONFIG_UPDATED, _handler)

            # When a fork child revives the listener and then stops it — the
            # shape a preloaded worker takes on shutdown.
            pid = os.fork()
            if pid == 0:
                status = 0
                try:
                    if child_lifecycle == "revive_then_stop":
                        parent_bus.start_listener()  # D3 repair + respawn
                    parent_bus.stop_listener()
                except BaseException:
                    status = 1
                finally:
                    os._exit(status)

            _, wait_status = os.waitpid(pid, 0)
            assert os.WIFEXITED(wait_status)
            assert os.WEXITSTATUS(wait_status) == 0

            # Then the parent is still subscribed and still delivering.
            publisher.publish(
                BaldurEvent(
                    event_type=EventType.CONFIG_UPDATED,
                    data={"key": "parent_survives_fork"},
                    source="publisher",
                )
            )

            assert received.wait(timeout=_DELIVERY_TIMEOUT_SECONDS), (
                "the parent stopped receiving events after the child's lifecycle "
                "— its subscription was torn down across the fork"
            )
            assert delivered[0].data == {"key": "parent_survives_fork"}
            # The event crossed Redis rather than short-circuiting locally: the
            # publisher is a different instance with a different self-origin id.
            assert publisher._instance_id != self_addressed
        finally:
            parent_bus.stop_listener()
            publisher.stop_listener()

    def test_child_draws_its_own_origin_identity(self, redis_url):
        """The child must not keep the parent's self-origin id — with an inherited
        one it classifies every same-host sibling's message as self-sent and drops
        it. Reported back through a pipe because the child cannot assert."""
        # Given a bus the child will inherit.
        parent_bus = _create_bus_with_real_redis(redis_url)
        parent_bus.start_listener()
        parent_id = parent_bus._instance_id
        read_fd, write_fd = os.pipe()
        try:
            # When the child revives it.
            pid = os.fork()
            if pid == 0:
                try:
                    parent_bus.start_listener()
                    os.write(write_fd, parent_bus._instance_id.encode())
                    os._exit(0)
                except BaseException:
                    os._exit(1)

            os.close(write_fd)
            write_fd = -1
            child_id = os.read(read_fd, 64).decode()
            _, wait_status = os.waitpid(pid, 0)
            assert os.WEXITSTATUS(wait_status) == 0

            # Then it reported a different identity than the one it inherited.
            assert child_id
            assert child_id != parent_id
            assert parent_bus._instance_id == parent_id
        finally:
            os.close(read_fd)
            if write_fd != -1:
                os.close(write_fd)
            parent_bus.stop_listener()
