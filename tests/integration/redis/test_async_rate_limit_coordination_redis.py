"""Async outbound 429 coordination against a real Redis.

The mock-based composition (``tests/integration/test_async_outbound_429_coordination.py``)
shares one in-process store *object*. Three claims only exist against a
network-backed store.

Test Categories:
A. Every store call the awaitable surface makes leaves the event loop — the
   coordinator hops them to a worker thread because the client is synchronous.
B. A cooldown written by one worker on one event loop governs another worker
   on another event loop, in another thread, holding a separate storage
   instance over the same server — the cross-process contract.
C. A peer's extension landing on the server while a waiter sleeps is read back
   by the re-check and turns the wait into a deferral when it outgrows the bound.

All tests auto-skip without Redis (``requires_redis``).
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from unittest.mock import patch

import pytest
import redis

from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.services.rate_limit_coordinator.models import RateLimitCoordinatorConfig
from tests.factories.rate_limit_doubles import ToThreadSpy

pytestmark = pytest.mark.requires_redis

_COORDINATOR_TO_THREAD = (
    "baldur.services.rate_limit_coordinator.coordinator.asyncio.to_thread"
)
_SHORT_COOLDOWN = 0.3


@pytest.fixture(autouse=True)
def _no_cluster_broadcast():
    with patch.object(RateLimitCoordinator, "_broadcast_to_cluster", autospec=True):
        yield


@pytest.fixture
def key() -> str:
    """A key nothing else on the shared server has touched."""
    return f"async-coordination-{uuid.uuid4().hex}"


def _storage(redis_url: str) -> RedisRateLimitStorage:
    """A fresh adapter over its own client — one worker's view of the server."""
    client = redis.from_url(redis_url, decode_responses=True)
    return RedisRateLimitStorage(client)


def _coordinator(storage, *, default_retry_after: float) -> RateLimitCoordinator:
    return RateLimitCoordinator(
        storage=storage,
        config=RateLimitCoordinatorConfig(
            jitter_percent=0.0,
            debounce_window_seconds=0.0,
            default_retry_after=default_retry_after,
        ),
    )


def _run_on_own_loop(coro_factory):
    """Run a coroutine to completion on a fresh event loop in a new thread."""
    box: dict = {}

    def target():
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as error:  # noqa: BLE001 — surfaced to the test
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(10.0)
    if "error" in box:
        raise box["error"]
    return box["value"]


class TestRedisAwaitableSurfaceLeavesTheLoop:
    """Every store call under the awaitable surface is a worker-thread hop."""

    def test_the_wait_reads_the_server_on_a_worker_thread(self, redis_url, key):
        """
        Purpose:
            ``await_if_needed`` over the Redis adapter must not run the client's
            network round trips on the event loop.
        Expected:
            - ``ensure_running`` and ``get_state`` were hopped
            - Nothing about the store was called inline
        """
        coordinator = _coordinator(_storage(redis_url), default_retry_after=1.0)
        spy = ToThreadSpy()

        with patch(_COORDINATOR_TO_THREAD, new=spy):
            result = asyncio.run(coordinator.await_if_needed(key, max_wait=1.0))

        assert result.waited is False
        assert spy.hopped("get_state") is True
        assert spy.hopped("ensure_running") is True

    def test_the_report_and_the_reset_leave_the_loop_too(self, redis_url, key):
        """
        Purpose:
            ``aon_rate_limited`` (store writes plus a bus publish) and
            ``aon_success`` (a read plus a conditional write) are hopped.
        Expected:
            - ``on_rate_limited`` and ``on_success`` appear in the hop record
            - The server holds the cooldown after the report and a zero
              counter after the reset
        """
        storage = _storage(redis_url)
        coordinator = _coordinator(storage, default_retry_after=30.0)
        spy = ToThreadSpy()

        async def scenario():
            await coordinator.aon_rate_limited(key)
            in_cooldown = storage.get_state(key).is_in_cooldown
            await coordinator.aon_success(key)
            return in_cooldown

        with patch(_COORDINATOR_TO_THREAD, new=spy):
            in_cooldown = asyncio.run(scenario())

        assert in_cooldown is True
        assert spy.hopped("on_rate_limited") is True
        assert spy.hopped("on_success") is True
        assert storage.get_state(key).consecutive_429s == 0


class TestRedisCooldownSharedAcrossEventLoops:
    """One worker's 429 on one loop governs another worker on another loop."""

    def test_a_cooldown_reported_on_one_loop_is_awaited_on_another(
        self, redis_url, key
    ):
        """
        Purpose:
            Worker A (thread 1, its own loop, its own adapter) reports a 429;
            worker B (thread 2, its own loop, a separate adapter over the same
            server) waits it out before its call — the cross-process contract
            the in-memory adapter cannot make.
        Expected:
            - B's wait reports waited=True and a wait covering A's cooldown
            - B's call ran no earlier than the expiry A installed
        """
        worker_a = _coordinator(
            _storage(redis_url), default_retry_after=_SHORT_COOLDOWN
        )
        worker_b = _coordinator(
            _storage(redis_url), default_retry_after=_SHORT_COOLDOWN
        )

        async def report():
            await worker_a.aon_rate_limited(key)
            return worker_a.get_state(key).cooldown_until

        expiry = _run_on_own_loop(report)

        async def wait_then_call():
            result = await worker_b.await_if_needed(key, max_wait=5.0)
            return result, time.time()

        result, ran_at = _run_on_own_loop(wait_then_call)

        assert result.waited is True
        assert result.deferred is False
        assert ran_at >= expiry - 0.02
        assert result.wait_time >= _SHORT_COOLDOWN - 0.1

    def test_a_peer_extension_on_the_server_defers_the_sleeping_waiter(
        self, redis_url, key
    ):
        """
        Purpose:
            Worker B sleeps a short cooldown with a bound it fits; while B
            sleeps, worker A reports a 429 with a Retry-After far past B's
            bound. B's re-check reads the extension off the server and defers
            instead of resuming into the extended cooldown.
        Expected:
            - deferred=True and waited=True (one segment slept)
            - not_before is the extended expiry the server now holds
        """
        storage_a = _storage(redis_url)
        worker_a = _coordinator(storage_a, default_retry_after=_SHORT_COOLDOWN)
        worker_b = _coordinator(
            _storage(redis_url), default_retry_after=_SHORT_COOLDOWN
        )
        storage_a.set_cooldown(key, time.time() + _SHORT_COOLDOWN)
        extended: dict[str, float] = {}

        async def scenario():
            wait_task = asyncio.create_task(worker_b.await_if_needed(key, max_wait=1.0))
            await asyncio.sleep(0.05)
            await worker_a.aon_rate_limited(key, retry_after=30.0)
            extended["until"] = storage_a.get_state(key).cooldown_until
            return await wait_task

        result = asyncio.run(scenario())

        assert result.deferred is True
        assert result.waited is True
        assert 0.1 <= result.wait_time <= 0.4
        assert result.not_before == pytest.approx(extended["until"], abs=1e-3)
