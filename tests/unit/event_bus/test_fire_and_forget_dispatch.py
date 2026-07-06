"""BaldurEventBus fire-and-forget dispatch tests (636 D1/D2).

Test targets:
- ``EventSubscription.await_result`` default + ``subscribe(await_result=...)``
  forwarding + ``get_subscriptions()`` exposing the flag (D1 contract).
- ``BaldurEventBus.publish()`` of an ``await_result=False`` handler returns
  while the handler is still blocked (non-blocking proof) and does NOT
  increment ``_timeout_count`` (D1 behavior).
- ``_dispatch_fire_and_forget`` across ``dispatch_mode`` ∈ {sync,
  thread_per_emit, async_pool}: sync runs inline on the caller thread;
  the two async modes never block the publisher (D1 behavior).
- ``_run_handler_logging_exceptions`` logs an uncaught handler exception as
  ``event_bus.fire_and_forget_handler_failed`` without affecting the
  publisher (D1 behavior).
- Gate→fire-and-forget-replay ``event.data[INTEGRITY_FAILED_KEY]`` cross-thread
  handoff: the awaited CRITICAL gate's write is visible to the NORMAL
  fire-and-forget replay handler submitted after it (D2 ordering).
- ``RedisEventBus.subscribe`` forwards ``await_result`` to the local bus.

Deterministic precedent for the non-blocking/ordering proofs: the
``zombie_release = threading.Event()`` pattern in
``test_dispatch_resilience.py`` — a test-controlled ``threading.Event``
replaces wall-clock ``time.sleep`` so the assertions never race CI load
(UNIT_TEST_GUIDELINES §6.5.6).

Compliance:
- UNIT_TEST_GUIDELINES §8.4 (side effects), §8.5 (interaction), §8.7
  (concurrency / thread safety).
- Behavior assertions exercise the default ``async_pool`` dispatch except
  where a mode is explicitly selected via ``BALDUR_EVENT_BUS_DISPATCH_MODE``.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from baldur.services.event_bus.bus.event_types import EventPriority, EventType
from baldur.services.event_bus.bus.models import BaldurEvent, EventSubscription

# =============================================================================
# Fixtures + isolation
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_event_bus_state():
    """Each test starts with a cleared dispatch executor + settings cache."""
    from baldur.services.event_bus.bus.event_bus import BaldurEventBus
    from baldur.settings.event_bus import reset_event_bus_settings

    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()
    yield
    BaldurEventBus.shutdown_dispatch_executor()
    reset_event_bus_settings()


def _make_event(event_type: EventType = EventType.CONFIG_UPDATED) -> BaldurEvent:
    return BaldurEvent(
        event_type=event_type,
        data={},
        source="fire_and_forget_test",
        priority=EventPriority.NORMAL,
    )


# =============================================================================
# Subscription contract — await_result default + get_subscriptions() exposure
# =============================================================================


class TestSubscriptionAwaitResultContract:
    """636 D1: ``await_result`` defaults True and is exposed for assertion."""

    def test_event_subscription_default_await_result_is_true(self):
        """``EventSubscription`` field default is True (awaited by default)."""
        sub = EventSubscription(
            event_type=EventType.CONFIG_UPDATED,
            handler=lambda e: None,
            handler_name="h",
        )
        assert sub.await_result is True

    def test_subscribe_default_await_result_is_true(self):
        """``subscribe()`` without the kwarg produces an awaited subscription."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        sub = bus.subscribe(EventType.CONFIG_UPDATED, lambda e: None)
        assert sub.await_result is True

    def test_subscribe_await_result_false_marks_subscription(self):
        """``subscribe(await_result=False)`` marks the subscription fire-and-forget."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        sub = bus.subscribe(
            EventType.CONFIG_UPDATED, lambda e: None, await_result=False
        )
        assert sub.await_result is False

    def test_get_subscriptions_exposes_await_result_flag(self):
        """``get_subscriptions()`` dict carries ``await_result`` per subscription."""
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()

        def awaited(event: BaldurEvent) -> None: ...

        def fire_and_forget(event: BaldurEvent) -> None: ...

        bus.subscribe(EventType.CONFIG_UPDATED, awaited, await_result=True)
        bus.subscribe(EventType.CONFIG_UPDATED, fire_and_forget, await_result=False)

        by_name = {
            s["handler_name"]: s
            for s in bus.get_subscriptions(EventType.CONFIG_UPDATED)
        }
        assert by_name["awaited"]["await_result"] is True
        assert by_name["fire_and_forget"]["await_result"] is False


# =============================================================================
# Fire-and-forget dispatch behavior (default async_pool mode)
# =============================================================================


class TestFireAndForgetDispatchBehavior:
    """636 D1: ``publish()`` of a fire-and-forget handler never blocks the publisher."""

    def test_publish_returns_while_fire_and_forget_handler_still_blocked(self):
        """``publish()`` returns with the handler still in-flight (not awaited).

        Deterministic non-blocking proof: the handler blocks on a
        test-controlled ``threading.Event``. The main thread waits only for
        the handler to *enter* the worker, then asserts the handler has NOT
        completed — which is guaranteed because ``release`` is never set
        before the assertion. Had ``publish()`` awaited the handler, it could
        not have returned at all (the handler is blocked), so reaching the
        assertion is itself the proof.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def blocking_handler(event: BaldurEvent) -> None:
            entered.set()
            release.wait(timeout=5.0)
            completed.set()

        bus.subscribe(EventType.CONFIG_UPDATED, blocking_handler, await_result=False)

        try:
            # When — publish a fire-and-forget handler that blocks on release.
            result = bus.publish(_make_event())

            # Then — publish() returned; the handler is genuinely in-flight
            # (entered) but has NOT completed → publish() did not await it.
            assert entered.wait(timeout=5.0)
            assert not completed.is_set()
            assert result == 1
        finally:
            release.set()

    def test_fire_and_forget_handler_does_not_increment_timeout_count(self):
        """A still-blocked fire-and-forget handler never trips ``_timeout_count``.

        A fire-and-forget handler cannot "time out" from the publisher's
        view — the publisher never waits on it — so the counter stays 0 even
        while the handler is blocked well past any handler timeout.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        entered = threading.Event()
        release = threading.Event()

        def blocking_handler(event: BaldurEvent) -> None:
            entered.set()
            release.wait(timeout=5.0)

        bus.subscribe(EventType.CONFIG_UPDATED, blocking_handler, await_result=False)

        try:
            bus.publish(_make_event())
            assert entered.wait(timeout=5.0)
            assert bus._timeout_count == 0
        finally:
            release.set()

    def test_awaited_handler_completes_before_publish_returns(self):
        """Contrast: an ``await_result=True`` handler is fully run before return.

        The inverse of the fire-and-forget path — ``publish()`` blocks on the
        awaited handler, so its side effect is observable synchronously once
        ``publish()`` returns.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        completed: list[bool] = []

        def handler(event: BaldurEvent) -> None:
            completed.append(True)

        bus.subscribe(EventType.CONFIG_UPDATED, handler, await_result=True)
        result = bus.publish(_make_event())

        assert completed == [True]
        assert result == 1

    def test_raising_fire_and_forget_handler_does_not_affect_publisher(self):
        """A fire-and-forget handler that raises is swallowed; publisher unaffected.

        The worker-side exception is caught by the logging wrapper and never
        reaches the publisher, so ``publish()`` returns normally and counts
        the handler as dispatched.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus

        bus = BaldurEventBus()
        ran = threading.Event()

        def raising(event: BaldurEvent) -> None:
            ran.set()
            raise ValueError("boom on worker")

        bus.subscribe(EventType.CONFIG_UPDATED, raising, await_result=False)

        result = bus.publish(_make_event())
        assert result == 1

        # Drain the worker so the handler body (and its raise) actually runs.
        BaldurEventBus.shutdown_dispatch_executor()
        assert ran.is_set()


# =============================================================================
# Fire-and-forget across dispatch modes
# =============================================================================


class TestFireAndForgetDispatchModesBehavior:
    """636 D1: ``_dispatch_fire_and_forget`` per ``dispatch_mode``."""

    def test_sync_mode_runs_fire_and_forget_inline_on_caller_thread(self, monkeypatch):
        """``sync`` mode runs the fire-and-forget handler inline (deterministic).

        Sync dispatch preserves single-thread determinism for tests/CLI — the
        handler runs on the caller thread rather than being offloaded.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        reset_event_bus_settings()

        bus = BaldurEventBus()
        observed: list[int] = []

        def handler(event: BaldurEvent) -> None:
            observed.append(threading.get_ident())

        bus.subscribe(EventType.CONFIG_UPDATED, handler, await_result=False)
        caller = threading.get_ident()
        result = bus.publish(_make_event())

        assert observed == [caller]
        assert result == 1

    @pytest.mark.parametrize(
        "mode",
        ["thread_per_emit", "async_pool"],
        ids=["thread_per_emit", "async_pool"],
    )
    def test_async_modes_dispatch_without_blocking_publisher(self, mode, monkeypatch):
        """``thread_per_emit`` / ``async_pool`` never block the publisher thread.

        Both offload the fire-and-forget handler (daemon thread / pool
        submit), so ``publish()`` returns while the handler is still blocked.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", mode)
        reset_event_bus_settings()

        bus = BaldurEventBus()
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def blocking_handler(event: BaldurEvent) -> None:
            entered.set()
            release.wait(timeout=5.0)
            completed.set()

        bus.subscribe(EventType.CONFIG_UPDATED, blocking_handler, await_result=False)

        try:
            result = bus.publish(_make_event())
            assert entered.wait(timeout=5.0)
            assert not completed.is_set()
            assert result == 1
        finally:
            release.set()


# =============================================================================
# Fire-and-forget exception logging
# =============================================================================


class TestFireAndForgetExceptionLogging:
    """636 D1: an uncaught fire-and-forget handler exception is logged."""

    def test_fire_and_forget_handler_exception_logs_under_documented_event(
        self, monkeypatch
    ):
        """A raising handler emits ``event_bus.fire_and_forget_handler_failed``.

        Run under ``sync`` mode so the logging wrapper executes inline on the
        caller thread (deterministic — no worker-thread/caplog race), and
        assert on a mocked module logger rather than ``caplog`` to avoid the
        xdist structlog-capture flakiness documented in §6.5.9.
        """
        from baldur.services.event_bus.bus import event_bus as event_bus_module
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_DISPATCH_MODE", "sync")
        reset_event_bus_settings()

        bus = BaldurEventBus()

        def raising(event: BaldurEvent) -> None:
            raise ValueError("expected-test-error")

        bus.subscribe(EventType.CONFIG_UPDATED, raising, await_result=False)

        mock_logger = MagicMock()
        monkeypatch.setattr(event_bus_module, "logger", mock_logger)

        # When — publish; the sync fire-and-forget path runs the wrapper inline.
        result = bus.publish(_make_event())

        # Then — the wrapper logged the failure; the publisher still counts the
        # handler as dispatched (the exception never propagated).
        assert mock_logger.exception.called
        logged_event = mock_logger.exception.call_args[0][0]
        assert logged_event == "event_bus.fire_and_forget_handler_failed"
        assert result == 1


# =============================================================================
# Gate → fire-and-forget replay cross-thread handoff (D2)
# =============================================================================


class TestGateToReplayHandoffBehavior:
    """636 D2: the awaited gate's ``event.data`` write is visible to the FnF replay."""

    def test_fire_and_forget_replay_observes_awaited_gate_flag(self):
        """The fire-and-forget replay handler reads the gate's ``event.data`` write.

        Mirrors the D2 wiring: a CRITICAL awaited gate writes
        ``event.data[INTEGRITY_FAILED_KEY]`` inline, then a NORMAL
        fire-and-forget replay handler is submitted *after* the gate returned.
        The ``executor.submit()`` boundary provides the happens-before edge, so
        the replay observes the flag once the executor is drained.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.services.event_bus.integrity_gate import INTEGRITY_FAILED_KEY

        bus = BaldurEventBus()
        observed: list[object] = []

        def gate(event: BaldurEvent) -> None:
            event.data[INTEGRITY_FAILED_KEY] = True

        def replay(event: BaldurEvent) -> None:
            observed.append(event.data.get(INTEGRITY_FAILED_KEY))

        # Gate: CRITICAL + awaited (runs first, inline). Replay: NORMAL +
        # fire-and-forget (submitted after the gate set the flag).
        bus.subscribe(
            EventType.CIRCUIT_BREAKER_CLOSED,
            gate,
            priority=EventPriority.CRITICAL,
            await_result=True,
        )
        bus.subscribe(
            EventType.CIRCUIT_BREAKER_CLOSED,
            replay,
            priority=EventPriority.NORMAL,
            await_result=False,
        )

        bus.publish(_make_event(EventType.CIRCUIT_BREAKER_CLOSED))

        # Drain the fire-and-forget future before asserting its observation.
        BaldurEventBus.shutdown_dispatch_executor()

        assert observed == [True]


# =============================================================================
# RedisEventBus forwarding
# =============================================================================


class TestRedisEventBusForwardingBehavior:
    """636 D1: ``RedisEventBus.subscribe`` forwards ``await_result`` to the local bus."""

    def test_subscribe_forwards_await_result_to_local_bus(self, monkeypatch):
        """``await_result`` is passed through to ``BaldurEventBus.subscribe`` verbatim.

        Both backends run local handlers through the same dispatch path, so
        the Redis backend must forward the flag unchanged.
        """
        from baldur.services.event_bus.bus.event_bus import BaldurEventBus
        from baldur.services.event_bus.redis_bus import RedisEventBus

        # Construct without a live Redis connection.
        monkeypatch.setattr(RedisEventBus, "_connect_redis", lambda self: False)
        redis_bus = RedisEventBus()

        mock_local = MagicMock(spec=BaldurEventBus)
        redis_bus._local_bus = mock_local

        def handler(event: BaldurEvent) -> None: ...

        redis_bus.subscribe(
            EventType.CIRCUIT_BREAKER_OPENED, handler, await_result=False
        )

        mock_local.subscribe.assert_called_once_with(
            EventType.CIRCUIT_BREAKER_OPENED,
            handler,
            priority=EventPriority.NORMAL,
            await_result=False,
        )
