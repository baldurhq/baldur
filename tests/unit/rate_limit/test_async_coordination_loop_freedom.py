"""The event loop stays free while an async surface waits on, or reports, a 429.

Target: the four async coordination surfaces
- ``AsyncRetryPolicy`` (the async retry stage) waiting on a shared cooldown
- ``AsyncTenacityBridgePolicy`` waiting through its coroutine ``before``
- ``@rate_limit_aware`` on an ``async def`` waiting through ``await_if_needed``
- ``RateLimitCoordinator.aon_rate_limited`` publishing to a slow subscriber

Evidence rather than wiring: a ``to_thread`` spy proves a hop was made, not
that the loop kept serving. Each row holds a real 0.3 s cooldown in an
in-process store (no sleep patch) and runs a concurrent ticker coroutine
sampling every 10 ms; the loop is free iff the ticker's largest inter-sample
gap stays far below the wait. A wait that blocked the loop would leave one
gap the size of the whole wait — the frozen-worker shape the parity work
removed. Real wall-clock time is the subject here, so the waits are not mocked.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import pytest
import tenacity

from baldur.adapters.rate_limit.memory_adapter import InMemoryRateLimitStorage
from baldur.bridges.tenacity.policy import AsyncTenacityBridgePolicy
from baldur.resilience.policies.async_retry import AsyncRetryPolicy
from baldur.services.event_bus import EventType, get_event_bus
from baldur.services.event_bus.bus.convenience import reset_event_bus
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from baldur.settings.event_bus import reset_event_bus_settings

# The wait every row imposes, and the loop-freedom bound the ticker must keep.
_WAIT_SECONDS = 0.3
_MAX_GAP_SECONDS = 0.1
_TICK_SECONDS = 0.01
# ``asyncio.sleep`` may return up to the loop's clock resolution early, and the
# remaining cooldown is read a few milliseconds after it was set.
_WAIT_TOLERANCE = 0.05

_KEY = "loop-freedom"


def _coordinator_in_cooldown(seconds: float = _WAIT_SECONDS) -> RateLimitCoordinator:
    """A real coordinator whose key holds an active cooldown of ``seconds``."""
    storage = InMemoryRateLimitStorage()
    storage.set_cooldown(_KEY, time.time() + seconds)
    return RateLimitCoordinator(
        storage=storage,
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0, debounce_window_seconds=0.0
        ),
    )


async def _measure(surface):
    """Run ``surface`` beside a ticker; return ``(elapsed, max_gap)``.

    The ticker samples the loop's clock every ``_TICK_SECONDS`` until the
    surface completes. Its largest inter-sample gap is how long the loop was
    unable to run any other coroutine.
    """
    loop = asyncio.get_running_loop()
    samples: list[float] = [loop.time()]
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            await asyncio.sleep(_TICK_SECONDS)
            samples.append(loop.time())

    ticker_task = asyncio.create_task(ticker())
    started = loop.time()
    try:
        await surface()
    finally:
        elapsed = loop.time() - started
        done.set()
        await ticker_task
    gaps = [
        later - earlier for earlier, later in zip(samples, samples[1:], strict=False)
    ]
    return elapsed, max(gaps)


class TestEventLoopStaysFreeBehavior:
    """A concurrent coroutine keeps running while each surface waits or reports."""

    @pytest.fixture(autouse=True)
    def _no_cluster_broadcast(self):
        with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
            yield

    def test_ticker_keeps_running_while_the_async_retry_stage_waits(self):
        coordinator = _coordinator_in_cooldown()
        policy = AsyncRetryPolicy(
            max_retries=0, domain=_KEY, rate_limit_coordinator=coordinator
        )

        async def ok():
            return "ok"

        async def surface():
            result = await policy.execute(ok)
            assert result.value == "ok"

        elapsed, max_gap = asyncio.run(_measure(surface))

        assert elapsed >= _WAIT_SECONDS - _WAIT_TOLERANCE
        assert max_gap < _MAX_GAP_SECONDS

    def test_ticker_keeps_running_while_the_async_tenacity_bridge_waits(self):
        coordinator = _coordinator_in_cooldown()
        bridge: AsyncTenacityBridgePolicy = AsyncTenacityBridgePolicy(
            stop=tenacity.stop_after_attempt(1),
            domain=_KEY,
            rate_limit_coordinator=coordinator,
            rate_limit_key=_KEY,
        )

        async def ok():
            return "ok"

        async def surface():
            result = await bridge.execute(ok)
            assert result.value == "ok"

        elapsed, max_gap = asyncio.run(_measure(surface))

        assert elapsed >= _WAIT_SECONDS - _WAIT_TOLERANCE
        assert max_gap < _MAX_GAP_SECONDS

    def test_ticker_keeps_running_while_the_rate_limit_aware_decorator_waits(self):
        coordinator = _coordinator_in_cooldown()

        @coordinator.rate_limit_aware(_KEY)
        async def protected():
            return "ok"

        async def surface():
            assert await protected() == "ok"

        elapsed, max_gap = asyncio.run(_measure(surface))

        assert elapsed >= _WAIT_SECONDS - _WAIT_TOLERANCE
        assert max_gap < _MAX_GAP_SECONDS

    def test_ticker_keeps_running_while_aon_rate_limited_publishes_to_a_slow_subscriber(
        self, monkeypatch
    ):
        """The report hop: a synchronous subscriber that blocks for the whole wait.

        ``on_rate_limited`` publishes ``RATE_LIMIT_429`` and waits on every
        subscriber that asked for its result — the reason the report twin
        always hops, whatever the store. The subscriber here blocks its thread
        for the full wait; the loop must not feel it.
        """
        coordinator = _coordinator_in_cooldown(seconds=0.0)
        handled = threading.Event()

        def slow_subscriber(_event):
            # A subscriber doing slow synchronous work of its own — modelled as
            # a bounded block on a thread, never on the loop.
            threading.Event().wait(_WAIT_SECONDS)
            handled.set()

        # The suite pins the per-handler timeout to 0.1 s; the row needs the
        # publish to wait the whole subscriber, as it does under the shipped
        # default of 5 s. Fresh bus + settings so the bound is read anew.
        monkeypatch.setenv(
            "BALDUR_EVENT_BUS_HANDLER_TIMEOUT_SECONDS", str(_WAIT_SECONDS * 10)
        )
        reset_event_bus_settings()
        reset_event_bus()
        get_event_bus().subscribe(
            EventType.RATE_LIMIT_429, slow_subscriber, await_result=True
        )

        async def surface():
            await coordinator.aon_rate_limited(_KEY)

        try:
            elapsed, max_gap = asyncio.run(_measure(surface))
        finally:
            # The env var is restored by monkeypatch; the cached node is not.
            reset_event_bus_settings()

        assert handled.is_set()
        assert elapsed >= _WAIT_SECONDS - _WAIT_TOLERANCE
        assert max_gap < _MAX_GAP_SECONDS
