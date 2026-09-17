"""Subscriptions are told apart by the handler, never by its name.

``BaldurEventBus.subscribe`` keeps one subscription per handler and event
type. The handler itself is the key: two lambdas both answer to ``<lambda>``
and two functions from different modules can share a bare ``__name__``, and
keying on the name made the second of each pair vanish without an error —
the shipped demo's own lambda subscriber disappeared under a harness that
subscribed a lambda first. ``unsubscribe`` follows the same key, so removing
one of two same-named handlers leaves the other in place.

Verification techniques applied:
- Behavior: two lambdas on one event type both receive the event
- Behavior: same-named functions from two scopes are two subscriptions
- Behavior: the same handler twice is one subscription, and the first one
  is what the second call returns (functions and bound methods alike)
- Behavior: unsubscribing one same-named handler removes only that one
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent

EVENT = EventType.DLQ_REPLAY_BATCH_COMPLETED


@pytest.fixture(autouse=True)
def _reset_event_bus_state():
    """Each test starts with a clean dispatch executor + settings cache."""
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus
    from baldur.settings.event_bus import reset_event_bus_settings

    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()
    yield
    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()


def _bus():
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus

    # Handlers run on the calling thread so the assertions read a settled list.
    with patch.object(BaldurEventBus, "_load_dispatch_mode", return_value="sync"):
        return BaldurEventBus()


def _event() -> BaldurEvent:
    return BaldurEvent(
        event_type=EVENT,
        data={"total": 7},
        source="subscription_identity_test",
        priority=EventPriority.NORMAL,
    )


def _named_handler(sink: list, tag: str):
    """A function whose ``__name__`` is the same for every call of this factory."""

    def handler(event: BaldurEvent) -> None:
        sink.append(tag)

    return handler


class _Listener:
    def __init__(self, sink: list, tag: str):
        self._sink, self._tag = sink, tag

    def on_event(self, event: BaldurEvent) -> None:
        self._sink.append(self._tag)


class TestSubscriptionIdentityBehavior:
    """The callable is the key; the name is for reporting only."""

    def test_two_lambdas_on_one_event_type_both_receive_it(self):
        bus, seen = _bus(), []
        bus.subscribe(EVENT, lambda event: seen.append("first"))
        bus.subscribe(EVENT, lambda event: seen.append("second"))

        called = bus.publish(_event())

        assert called == 2
        assert sorted(seen) == ["first", "second"]

    def test_same_named_functions_from_two_scopes_are_two_subscriptions(self):
        bus, seen = _bus(), []
        first, second = _named_handler(seen, "a"), _named_handler(seen, "b")
        assert first.__name__ == second.__name__

        bus.subscribe(EVENT, first)
        bus.subscribe(EVENT, second)
        bus.publish(_event())

        assert sorted(seen) == ["a", "b"]

    def test_same_function_twice_is_one_subscription(self):
        bus, seen = _bus(), []
        handler = _named_handler(seen, "once")

        first = bus.subscribe(EVENT, handler)
        second = bus.subscribe(EVENT, handler)
        bus.publish(_event())

        assert second is first
        assert seen == ["once"]

    def test_same_bound_method_twice_is_one_subscription(self):
        """A bound method is a fresh object on every attribute access; equality,
        not identity, is what keeps a listener's re-subscribe idempotent."""
        bus, seen = _bus(), []
        listener = _Listener(seen, "listener")

        first = bus.subscribe(EVENT, listener.on_event)
        second = bus.subscribe(EVENT, listener.on_event)
        bus.publish(_event())

        assert second is first
        assert seen == ["listener"]

    def test_two_listeners_of_one_class_are_two_subscriptions(self):
        bus, seen = _bus(), []
        bus.subscribe(EVENT, _Listener(seen, "one").on_event)
        bus.subscribe(EVENT, _Listener(seen, "two").on_event)

        bus.publish(_event())

        assert sorted(seen) == ["one", "two"]

    def test_unsubscribing_one_same_named_handler_leaves_the_other(self):
        bus, seen = _bus(), []
        first, second = _named_handler(seen, "a"), _named_handler(seen, "b")
        bus.subscribe(EVENT, first)
        bus.subscribe(EVENT, second)

        removed = bus.unsubscribe(EVENT, first)
        bus.publish(_event())

        assert removed is True
        assert seen == ["b"]
        assert [s["handler_name"] for s in bus.get_subscriptions(EVENT)] == ["handler"]
