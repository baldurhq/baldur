"""BaldurEventBus dispatch graceful-shutdown integration test (impl 487 D4).

Mock-based — no infra. Wires ``BaldurEventBusDispatchShutdownHandler`` into
``GracefulShutdownCoordinator`` and asserts the drain semantics across a
real concurrent emit + coordinator-driven shutdown.

Covers (per impl 487 Test Assessment § Integration Tests):
- Drain path: in-flight handler tasks complete before
  ``ShutdownPhase.TERMINATED`` is reached.
- Force-shutdown path: when drain timeout expires the executor is
  released without waiting for slow handlers.
"""

from __future__ import annotations

import threading
import time

import pytest

from baldur.core.shutdown_coordinator import (
    GracefulShutdownCoordinator,
    RequestTracker,
    ShutdownPhase,
)
from baldur.services.event_bus.bus.event_bus import (
    BaldurEventBus,
    BaldurEventBusDispatchShutdownHandler,
    integrate_dispatch_with_shutdown_coordinator,
)
from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent


@pytest.fixture(autouse=True)
def _isolate_dispatch_executor():
    """Each test starts and ends with the EventBus executor cleared."""
    BaldurEventBus.shutdown_dispatch_executor()
    yield
    BaldurEventBus.shutdown_dispatch_executor()


def _make_event() -> BaldurEvent:
    return BaldurEvent(
        event_type=EventType.CONFIG_UPDATED,
        data={},
        source="dispatch_shutdown_integration",
        priority=EventPriority.NORMAL,
    )


class TestDispatchShutdownDrainIntegration:
    """487 D4: coordinator drain blocks until in-flight handlers complete."""

    def test_handler_registered_via_factory(self):
        """``integrate_dispatch_with_shutdown_coordinator()`` returns the handler."""
        handler = integrate_dispatch_with_shutdown_coordinator()
        assert isinstance(handler, BaldurEventBusDispatchShutdownHandler)

    def test_drain_completes_after_in_flight_handler_finishes(self):
        """Slow handler running concurrently → coordinator drains it before TERMINATED."""
        bus = BaldurEventBus()

        handler_started = threading.Event()
        handler_completed = threading.Event()

        def slow_handler(event: BaldurEvent) -> None:
            handler_started.set()
            time.sleep(0.2)
            handler_completed.set()

        bus.subscribe(EventType.CONFIG_UPDATED, slow_handler)

        # Kick off an emit on a background thread so we can shut down
        # the coordinator while the handler is mid-flight in the
        # executor worker.
        emit_thread = threading.Thread(target=bus.publish, args=(_make_event(),))
        emit_thread.start()

        # Wait until the executor worker has actually started running
        # the handler.
        assert handler_started.wait(timeout=5.0)

        coordinator = GracefulShutdownCoordinator(
            request_tracker=RequestTracker(),
            drain_timeout=5.0,
            check_interval=0.05,
        )
        coordinator.register_handler(integrate_dispatch_with_shutdown_coordinator())

        coordinator.initiate_shutdown()
        assert coordinator.wait_for_shutdown(timeout=10.0)
        emit_thread.join(timeout=10.0)

        assert coordinator.phase == ShutdownPhase.TERMINATED
        # ``wait=True`` on ``shutdown_dispatch_executor`` guarantees the
        # in-flight handler ran to completion before drain returned.
        assert handler_completed.is_set()
        assert BaldurEventBus._executor is None

    def test_force_shutdown_does_not_wait_for_slow_handler(self):
        """Drain-timeout expiry triggers ``on_force_shutdown`` (wait=False).

        Forces the timeout path by holding an in-flight tracked request
        through the drain window — without it, the drain loop would
        succeed immediately (default ``is_drain_complete`` returns True
        and no pending HTTP requests remain) and ``on_drain_complete``
        (wait=True) would block on the slow handler instead.
        """
        bus = BaldurEventBus()

        handler_started = threading.Event()
        release = threading.Event()

        def very_slow(event: BaldurEvent) -> None:
            handler_started.set()
            release.wait(timeout=10.0)

        bus.subscribe(EventType.CONFIG_UPDATED, very_slow)

        emit_thread = threading.Thread(target=bus.publish, args=(_make_event(),))
        emit_thread.start()
        assert handler_started.wait(timeout=5.0)

        tracker = RequestTracker()
        # Pin a pending HTTP request so http_drained=False and the drain
        # loop runs to its timeout, exercising the force-shutdown path.
        tracker.start_request("blocker-1", endpoint="/test")

        coordinator = GracefulShutdownCoordinator(
            request_tracker=tracker,
            drain_timeout=0.3,
            check_interval=0.05,
        )
        coordinator.register_handler(integrate_dispatch_with_shutdown_coordinator())

        try:
            start = time.monotonic()
            coordinator.initiate_shutdown()
            assert coordinator.wait_for_shutdown(timeout=5.0)
            elapsed = time.monotonic() - start

            # Force shutdown returned despite the still-running handler.
            assert coordinator.phase == ShutdownPhase.TERMINATED
            assert BaldurEventBus._executor is None
            # The handler is still blocked, so we did NOT wait for it.
            assert elapsed < 3.0
        finally:
            release.set()
            emit_thread.join(timeout=10.0)
