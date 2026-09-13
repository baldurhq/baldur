"""Async outbound 429 coordination — AsyncRetryPolicy x RateLimitCoordinator x storage x EventBus.

Mock-based integration (real ``InMemoryRateLimitStorage`` adapter, real
``RateLimitCoordinator``, real in-process ``EventBus``; no infra).

The unit tier mocks the coordinator or pre-seeds the store. What only the
composition shows: a 429 raised inside the *async* retry loop reaches the store
through the worker-thread report twin and is published to a bus subscriber
that blocks the hop; the next attempt's awaitable wait reads that freshly
installed cooldown back; a synchronous peer's extension lands while the async
waiter sleeps and is honoured by the re-check; and the loop stays free for the
whole of it.

Test Categories:
    A. Cooldown install -> read-back within one async call:
        - A 429 on attempt 1 installs a cooldown attempt 2 defers on
        - A cooldown that fits the budget is awaited, then the retried call succeeds
        - A recovery success resets the consecutive-429 counter in storage
    B. Cross-worker shared state:
        - A second async worker waits on the first worker's cooldown
        - A synchronous peer that extends the cooldown mid-wait is honoured by
          the async waiter (no early wake)
    C. The event bus sees the async stage's 429 and the publish never blocks
       the loop

Note: All tests use the in-memory rate-limit adapter — no infra dependency.
      Waits are real but short (<= 0.5 s). Loop freedom per surface is pinned
      by the unit tier; here only the blocking-subscriber publish carries a
      ticker, with a 2x margin against scheduler noise on a loaded host.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.core.backoff import ConstantBackoff
from baldur.interfaces.resilience_policy import PolicyOutcome
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.event_bus import EventType, get_event_bus
from baldur.services.event_bus.bus.convenience import reset_event_bus
from baldur.services.rate_limit_coordinator import (
    RateLimitCoordinator,
    RateLimitDeferredError,
)
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.services.retry_handler.models import RetryPolicyConfig
from baldur.services.retry_handler.policy import RetryPolicy

# A 429-detectable message (baldur.services.retry_handler.rate_limit_detection).
_RATE_LIMIT_MESSAGE = "429 too many requests"

# Short real cooldowns: the async wait is an asyncio.sleep the tests let run.
_SHORT_COOLDOWN = 0.2
_LONG_COOLDOWN = 300.0


@pytest.fixture(autouse=True)
def _no_cluster_broadcast():
    """Neutralize the out-of-scope cluster 429 broadcast for every test here."""
    with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
        yield


@pytest.fixture(autouse=True)
def _fresh_event_bus():
    """A fresh in-process bus per test so subscriptions cannot leak."""
    reset_event_bus()
    yield
    reset_event_bus()


def _coordinator(storage, *, default_retry_after: float) -> RateLimitCoordinator:
    """Deterministic coordinator (jitter off, no debounce) over a given storage."""
    return RateLimitCoordinator(
        storage=storage,
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
            default_retry_after=default_retry_after,
        ),
    )


def _async_policy(coordinator, *, domain: str, max_elapsed: float) -> AsyncRetryPolicy:
    """Async retry loop wired to the coordinator, zero backoff so budget = the wait."""
    return AsyncRetryPolicy(
        max_retries=2,
        domain=domain,
        max_elapsed=max_elapsed,
        backoff=ConstantBackoff(delay=0.0),
        rate_limit_coordinator=coordinator,
    )


def _sync_policy(coordinator, *, domain: str, max_elapsed: float) -> RetryPolicy:
    """The synchronous peer, sharing the coordinator."""
    return RetryPolicy(
        config=RetryPolicyConfig(
            max_attempts=1, domain=domain, max_elapsed=max_elapsed
        ),
        rate_limit_coordinator=coordinator,
        backoff=ConstantBackoff(delay=0.0),
        sleeper=lambda _: None,
    )


async def _with_ticker(coro):
    """Await ``coro`` beside a 10 ms ticker; return ``(value, max_gap)``."""
    loop = asyncio.get_running_loop()
    samples = [loop.time()]
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            await asyncio.sleep(0.01)
            samples.append(loop.time())

    task = asyncio.create_task(ticker())
    try:
        value = await coro
    finally:
        done.set()
        await task
    gaps = [b - a for a, b in zip(samples, samples[1:], strict=False)]
    return value, max(gaps)


# =============================================================================
# A. Cooldown install -> read-back within one async call
# =============================================================================


class TestAsyncOutboundCooldownLifecycle:
    """The 429 -> cooldown -> next-attempt-reads-it chain on the async stage.

    Validates:
    - A 429 raised inside the async loop reaches storage as a real cooldown
    - The next attempt awaits that cooldown back and decides serve-vs-defer on it
    - A recovery success resets the counter, through the awaitable twins
    """

    def test_429_installs_cooldown_and_next_attempt_defers(self):
        """
        Purpose:
            A real 429 on attempt 1 drives ``aon_rate_limited`` (a worker-thread
            hop), and attempt 2's ``await_if_needed`` reads the just-written
            cooldown back and defers on it — nothing is pre-seeded.
        Expected:
            - Outcome is FAILURE with reason "rate_limit_deferred" and not_before set
            - The error is the real prior 429, not a synthesised deferral error
            - Only attempt 1 is recorded in retry_history
            - Storage holds an active cooldown with consecutive_429s == 1
        """
        storage = InMemoryRateLimitStorage()
        policy = _async_policy(
            _coordinator(storage, default_retry_after=_LONG_COOLDOWN),
            domain="payment",
            max_elapsed=2.0,
        )

        async def func():
            raise Exception(_RATE_LIMIT_MESSAGE)

        result = asyncio.run(policy.execute(func))

        assert result.outcome == PolicyOutcome.FAILURE
        assert result.metadata["reason"] == "rate_limit_deferred"
        assert result.metadata["not_before"] is not None
        assert result.metadata["rate_limit_key"] == "payment"
        assert _RATE_LIMIT_MESSAGE in str(result.error)
        assert len(result.metadata["retry_history"]) == 1

        state = storage.get_state("payment")
        assert state.is_in_cooldown is True
        assert state.consecutive_429s == 1

    def test_429_then_fitting_cooldown_is_awaited_and_call_succeeds(self):
        """
        Purpose:
            A cooldown that fits the budget is awaited in full on the loop,
            then the retried call succeeds.
        Expected:
            - Outcome is SUCCESS carrying the second attempt's value
            - The second attempt ran no earlier than the installed cooldown's expiry
        """
        storage = InMemoryRateLimitStorage()
        policy = _async_policy(
            _coordinator(storage, default_retry_after=_SHORT_COOLDOWN),
            domain="payment",
            max_elapsed=5.0,
        )
        calls: list[float] = []
        expiry: dict[str, float] = {}

        async def flaky():
            calls.append(time.time())
            if len(calls) == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "recovered"

        async def scenario():
            result = await policy.execute(flaky)
            expiry["until"] = storage.get_state("payment").cooldown_until
            return result

        result = asyncio.run(scenario())

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.value == "recovered"
        assert len(calls) == 2
        # The retry ran only once the cooldown attempt 1 installed had elapsed.
        assert calls[1] >= expiry["until"] - 0.02

    def test_recovery_success_resets_the_consecutive_counter(self):
        """
        Purpose:
            After a 429 and a served wait, the success that ends the loop
            resets the storage counter through ``aon_success``.
        Expected:
            - consecutive_429s is back to 0 after the successful attempt
        """
        storage = InMemoryRateLimitStorage()
        policy = _async_policy(
            _coordinator(storage, default_retry_after=_SHORT_COOLDOWN),
            domain="payment",
            max_elapsed=5.0,
        )
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception(_RATE_LIMIT_MESSAGE)
            return "ok"

        result = asyncio.run(policy.execute(flaky))

        assert result.outcome == PolicyOutcome.SUCCESS
        assert storage.get_state("payment").consecutive_429s == 0


# =============================================================================
# B. Cross-worker shared state
# =============================================================================


class TestAsyncCrossWorkerCooldownSharing:
    """One worker's 429 governs another worker's next attempt — sync and async alike."""

    def test_a_second_async_worker_waits_on_the_first_workers_cooldown(self):
        """
        Purpose:
            Worker A's 429 installs a cooldown; worker B, an independent policy
            over the same coordinator, never saw a 429 and still waits it out.
        Expected:
            - B's call runs only after A's cooldown expired
            - B reports SUCCESS with one attempt
        """
        storage = InMemoryRateLimitStorage()
        coordinator = _coordinator(storage, default_retry_after=_SHORT_COOLDOWN)
        worker_a = _async_policy(coordinator, domain="payment", max_elapsed=0.01)
        worker_b = _async_policy(coordinator, domain="payment", max_elapsed=5.0)
        b_ran_at: dict[str, float] = {}

        async def a_throttled():
            raise Exception(_RATE_LIMIT_MESSAGE)

        async def b_ok():
            b_ran_at["t"] = time.time()
            return "ok"

        async def scenario():
            await worker_a.execute(a_throttled)
            expiry = storage.get_state("payment").cooldown_until
            result = await worker_b.execute(b_ok)
            return result, expiry

        result, expiry = asyncio.run(scenario())

        assert result.outcome == PolicyOutcome.SUCCESS
        assert result.total_attempts == 1
        assert b_ran_at["t"] >= expiry - 0.02

    @staticmethod
    def _in_flight_sync_peer(coordinator, *, retry_after: float):
        """A synchronous worker already inside its attempt, held until released.

        Returns ``(thread, entered, release)``. The peer was admitted before
        any cooldown existed (its wait saw an idle key); once released, its
        attempt raises a 429 carrying ``retry_after`` and the sync stage's own
        report path (``on_rate_limited``) writes the extension.
        """
        entered = threading.Event()
        release = threading.Event()

        class ThrottledLonger(Exception):
            pass

        ThrottledLonger.retry_after = retry_after  # type: ignore[attr-defined]

        def peer_call():
            entered.set()
            release.wait(5.0)
            raise ThrottledLonger(_RATE_LIMIT_MESSAGE)

        peer = _sync_policy(coordinator, domain="payment", max_elapsed=5.0)
        thread = threading.Thread(target=peer.execute, args=(peer_call,), daemon=True)
        return thread, entered, release

    def test_a_sync_peers_extension_mid_wait_is_honoured_by_the_async_waiter(self):
        """
        Purpose:
            The async waiter enters with a short cooldown remaining; while it
            sleeps, a synchronous peer already in flight takes a 429 with a
            longer Retry-After. The re-check after the served segment must see
            the later expiry and keep waiting — the stale-sleep defect resumed
            at the old one.
        Expected:
            - The async call runs only after the *extended* expiry
            - The wait reports a served (not deferred) result whose total
              covers both segments
        """
        storage = InMemoryRateLimitStorage()
        coordinator = _coordinator(storage, default_retry_after=_SHORT_COOLDOWN)
        thread, entered, release = self._in_flight_sync_peer(
            coordinator, retry_after=0.5
        )
        thread.start()
        assert entered.wait(5.0)
        storage.set_cooldown("payment", time.time() + _SHORT_COOLDOWN)
        extended: dict[str, float] = {}
        ran_at: dict[str, float] = {}

        async def waiter():
            result = await coordinator.await_if_needed("payment", max_wait=5.0)
            ran_at["t"] = time.time()
            return result

        async def scenario():
            wait_task = asyncio.create_task(waiter())
            # Let the waiter start its first segment, then land the peer's 429
            # from its own thread — the sync stage's report path.
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.to_thread(thread.join, 5.0)
            extended["until"] = storage.get_state("payment").cooldown_until
            return await wait_task

        result = asyncio.run(scenario())

        assert result.deferred is False
        assert result.waited is True
        assert ran_at["t"] >= extended["until"] - 0.02
        assert result.wait_time >= 0.4

    def test_a_sync_peers_extension_past_the_bound_defers_the_async_waiter(self):
        """
        Purpose:
            The same mid-wait extension, but past what is left of the async
            waiter's bound: it must stop after the segment it already slept and
            report a deferral carrying the fresh expiry.
        Expected:
            - deferred=True and waited=True, wait_time ~= the first segment
            - not_before is the peer's extended expiry
        """
        storage = InMemoryRateLimitStorage()
        coordinator = _coordinator(storage, default_retry_after=_SHORT_COOLDOWN)
        thread, entered, release = self._in_flight_sync_peer(
            coordinator, retry_after=30.0
        )
        thread.start()
        assert entered.wait(5.0)
        storage.set_cooldown("payment", time.time() + _SHORT_COOLDOWN)

        async def scenario():
            wait_task = asyncio.create_task(
                coordinator.await_if_needed("payment", max_wait=1.0)
            )
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.to_thread(thread.join, 5.0)
            return await wait_task, storage.get_state("payment").cooldown_until

        result, expiry = asyncio.run(scenario())

        assert result.deferred is True
        assert result.waited is True
        assert 0.1 <= result.wait_time <= 0.3
        assert result.not_before == expiry


# =============================================================================
# C. The event bus sees the async stage's 429 without blocking the loop
# =============================================================================


class TestAsyncRateLimitEventPublication:
    """The async stage's 429 reaches bus subscribers through the report hop."""

    def test_the_bus_receives_the_429_and_a_blocking_subscriber_never_stalls_the_loop(
        self, monkeypatch
    ):
        """
        Purpose:
            A subscriber registered with ``await_result=True`` (the default)
            blocks the publish for a while. The async loop's 429 report must
            still reach it — with the key — and the event loop must keep
            serving other coroutines during the block.
        Expected:
            - One RATE_LIMIT_429 event with data["key"] == "payment"
            - The ticker's largest gap stays at most half the subscriber's block
        """
        from baldur.settings.event_bus import reset_event_bus_settings

        monkeypatch.setenv("BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", "5")
        reset_event_bus_settings()
        reset_event_bus()
        received: list = []
        # A blocked loop would show one gap the size of the whole block; the
        # bound keeps a 2x margin against scheduler noise under a loaded host.
        block = 0.5
        max_gap_allowed = block / 2

        def blocking_subscriber(event):
            threading.Event().wait(block)
            received.append(event)

        get_event_bus().subscribe(
            EventType.RATE_LIMIT_429, blocking_subscriber, await_result=True
        )
        storage = InMemoryRateLimitStorage()
        policy = _async_policy(
            _coordinator(storage, default_retry_after=_LONG_COOLDOWN),
            domain="payment",
            max_elapsed=2.0,
        )

        async def throttled():
            raise Exception(_RATE_LIMIT_MESSAGE)

        try:
            result, max_gap = asyncio.run(_with_ticker(policy.execute(throttled)))
        finally:
            reset_event_bus_settings()

        assert result.metadata["reason"] == "rate_limit_deferred"
        assert len(received) == 1
        assert received[0].data["key"] == "payment"
        assert max_gap < max_gap_allowed

    def test_a_deferral_raised_to_the_caller_carries_the_key_and_not_before(self):
        """
        Purpose:
            The composition's terminal shape for a requeue-capable caller: the
            deferral error that ``@retry`` would raise names the key and time.
        Expected:
            - RateLimitDeferredError with key "payment" and not_before == the store's expiry
        """
        from baldur.resilience.policies.async_retry import _unwrap_or_raise

        storage = InMemoryRateLimitStorage()
        storage.set_cooldown("payment", time.time() + _LONG_COOLDOWN)
        policy = _async_policy(
            _coordinator(storage, default_retry_after=_LONG_COOLDOWN),
            domain="payment",
            max_elapsed=1.0,
        )

        async def never():
            raise AssertionError("must not run")

        result = asyncio.run(policy.execute(never))

        with pytest.raises(RateLimitDeferredError) as exc_info:
            _unwrap_or_raise(result, "never", 3)
        assert exc_info.value.key == "payment"
        assert exc_info.value.not_before == storage.get_state("payment").cooldown_until
