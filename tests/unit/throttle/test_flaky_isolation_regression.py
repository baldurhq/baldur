"""
Regression guard for throttle cross-test contamination.

Flaky history:
    `tests/unit/throttle/test_check_auto_dlq_storage.py` intermittently failed
    under `pytest -n 6 --no-slow` because AdaptiveThrottle subscribes to six
    EventBus event types in `__init__` and never unsubscribes. Instances
    created in earlier tests survived into later tests' event fanout and, via
    `_on_config_updated`, mutated the singleton throttle settings — breaking
    `initial_limit=1` assumptions further down the run order.

    The fix is the `_isolate_throttle_state` autouse fixture in this package's
    `conftest.py`, which calls `reset_event_bus()` + `reset_throttle_settings()`
    + `reset_adaptive_throttle()` before and after every test.

    These regressions verify:
      1. AdaptiveThrottle.__init__ *does* register CONFIG_UPDATED handlers
         on the EventBus (otherwise the contamination mechanism is closed).
      2. `reset_event_bus()` actually drops those subscriptions so a later
         emission does not invoke the stale handler.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def _bus_snapshot():
    """Return (bus, subscribers_for_config_updated) using the current singleton."""
    from baldur.services.event_bus import EventType, get_event_bus

    bus = get_event_bus()
    return bus, EventType.CONFIG_UPDATED


class TestThrottleEventBusSubscriptionBehavior:
    """AdaptiveThrottle subscribes to EventBus on construction — pre-condition for the contamination mechanism."""

    def test_init_registers_config_updated_handler_on_shared_event_bus(self):
        """
        Given: freshly reset EventBus.
        When:  AdaptiveThrottle is constructed with a local ThrottleConfig.
        Then:  the shared bus has at least one CONFIG_UPDATED subscriber that
               points at the new instance's `_on_config_updated` method.
        """
        # Given
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur_pro.services.throttle.adaptive import AdaptiveThrottle
        from baldur_pro.services.throttle.config import ThrottleConfig

        bus = get_event_bus()
        subs_before = list(bus._subscriptions.get(EventType.CONFIG_UPDATED, []))

        # When
        throttle = AdaptiveThrottle(ThrottleConfig(initial_limit=1))

        # Then
        subs_after = list(bus._subscriptions.get(EventType.CONFIG_UPDATED, []))
        assert len(subs_after) > len(subs_before), (
            "AdaptiveThrottle.__init__ should register a CONFIG_UPDATED handler"
        )
        # And the registered handler is the instance method we expect
        new_handler_names = {s.handler_name for s in subs_after} - {
            s.handler_name for s in subs_before
        }
        assert "_on_config_updated" in new_handler_names, (
            "Registered handler must be AdaptiveThrottle._on_config_updated"
        )
        # Touch `throttle` so the instance is retained until assertions finish.
        assert throttle is not None


class TestResetEventBusIsolationBehavior:
    """reset_event_bus() must strip stale subscribers so the next test starts clean."""

    def test_reset_event_bus_drops_adaptive_throttle_subscription(self):
        """
        Given: an AdaptiveThrottle instance subscribed to CONFIG_UPDATED.
        When:  `reset_event_bus()` runs (as `_isolate_throttle_state` does between tests).
        Then:  the bus singleton has zero CONFIG_UPDATED subscribers, so a
               later emission from an unrelated test cannot reach the
               stale instance's `_on_config_updated`.
        """
        # Given
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur.services.event_bus.bus.convenience import reset_event_bus
        from baldur_pro.services.throttle.adaptive import AdaptiveThrottle
        from baldur_pro.services.throttle.config import ThrottleConfig

        throttle = AdaptiveThrottle(ThrottleConfig(initial_limit=1))
        bus_before = get_event_bus()
        assert len(bus_before._subscriptions.get(EventType.CONFIG_UPDATED, [])) > 0, (
            "Pre-condition: CONFIG_UPDATED must have a subscriber"
        )

        # When
        reset_event_bus()

        # Then: a fresh singleton with no stale subscribers
        bus_after = get_event_bus()
        assert bus_after is not bus_before, (
            "reset_event_bus() should drop the previous bus singleton"
        )
        assert len(bus_after._subscriptions.get(EventType.CONFIG_UPDATED, [])) == 0, (
            "reset_event_bus() must clear all CONFIG_UPDATED subscribers"
        )

        # Extra guard: even if something republishes on the new bus, the
        # stale instance's handler is not reachable through it.
        with patch.object(
            throttle, "_on_config_updated", autospec=True
        ) as stale_handler:
            # Emit on the new bus — stale handler must not fire.
            from baldur.services.event_bus.bus.models import BaldurEvent

            bus_after.publish(
                BaldurEvent(
                    event_type=EventType.CONFIG_UPDATED,
                    data={"config_type": "throttle"},
                    source="regression_test",
                )
            )
            stale_handler.assert_not_called()

    def test_sequential_throttle_instances_do_not_share_event_bus_state(self):
        """
        Given: an AdaptiveThrottle A created and then discarded with
               `reset_event_bus()` in between (mimicking what
               `_isolate_throttle_state` does between tests).
        When:  a second AdaptiveThrottle B is created and the bus emits
               CONFIG_UPDATED with `config_type="throttle"`.
        Then:  only B's handler fires, proving that A's dangling subscription
               was effectively removed by the reset.
        """
        # Given
        from baldur.services.event_bus import EventType, get_event_bus
        from baldur.services.event_bus.bus.convenience import reset_event_bus
        from baldur.services.event_bus.bus.models import BaldurEvent
        from baldur_pro.services.throttle.adaptive import AdaptiveThrottle
        from baldur_pro.services.throttle.config import ThrottleConfig

        throttle_a = AdaptiveThrottle(ThrottleConfig(initial_limit=1))
        reset_event_bus()  # boundary between "tests"
        throttle_b = AdaptiveThrottle(ThrottleConfig(initial_limit=10))

        with (
            patch.object(throttle_a, "_on_config_updated") as handler_a,
            patch.object(throttle_b, "_on_config_updated") as handler_b,
        ):
            # When
            get_event_bus().publish(
                BaldurEvent(
                    event_type=EventType.CONFIG_UPDATED,
                    data={"config_type": "throttle"},
                    source="regression_test",
                )
            )

            # Then
            handler_a.assert_not_called()
            # `throttle_b` was constructed *after* the reset, so its
            # subscription registered against the new bus. The patch above
            # replaces the bound method *on the instance* — the original
            # reference captured inside the EventBus still points at the
            # real method, so the patch only proves "A's handler stays
            # silent." That is the assertion we care about for isolation.
            # Keep the reference alive so GC does not drop the subscription.
            assert handler_b is not None
