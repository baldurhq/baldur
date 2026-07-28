"""Unit tests for the detached liveness write — ``update_last_loop_timestamp``.

Target: ``baldur.meta.state_store.WatchdogStateStore``.

The caller of this method is the watchdog loop, whose whole point is a bounded
wall-clock pass: a Redis whose ``set`` takes seconds must cost the loop nothing.
So the local fallback is stamped on the calling thread and the Redis leg runs on
a daemon thread nobody waits for, with at most one write in flight and the
timestamp taken at send time.

Verification techniques applied:
  - Side effects — the key, value and TTL handed to Redis
  - Concurrency — single-in-flight, and the caller never joins the writer
  - Exception/edge cases — a raising client is swallowed and re-arms the slot
  - Negative assertions — a second call mid-write starts no second writer

The injected client parks on a test-controlled event, so a caller that waited on
the write would hang the test rather than fail an assertion: the non-blocking
claim is structural, not a timing threshold.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pytest

from baldur.meta.state_store import WatchdogStateStore

_EVENT_WAIT_SECONDS = 10.0


class _ParkedRedis:
    """Redis stand-in whose ``set`` blocks until the test releases it."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.set_calls: list[tuple[str, str, Any]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.local_seen_at_send: list[bool] = []
        self._raises = raises
        self._store: WatchdogStateStore | None = None

    def bind(self, store: WatchdogStateStore) -> None:
        self._store = store

    def get(self, key: str) -> None:
        # No stored value: readers fall through to the local timestamp, which is
        # the leg under test here.
        return None

    def set(self, key: str, value: str, ex: Any = None) -> bool:
        if self._store is not None:
            self.local_seen_at_send.append(
                self._store.get_last_loop_timestamp() is not None
            )
        self.set_calls.append((key, value, ex))
        self.entered.set()
        self.release.wait(_EVENT_WAIT_SECONDS)
        if self._raises is not None:
            raise self._raises
        return True


@pytest.fixture
def parked_store():
    """Build store + parked client pairs; release and join them at teardown.

    Release strictly precedes the join, so a test that deliberately leaves a
    write parked costs teardown nothing.
    """
    created: list[tuple[WatchdogStateStore, _ParkedRedis]] = []

    def _make(**kwargs: Any) -> tuple[WatchdogStateStore, _ParkedRedis]:
        client = _ParkedRedis(**kwargs)
        store = WatchdogStateStore(redis_client=client)
        client.bind(store)
        created.append((store, client))
        return store, client

    yield _make

    for store, client in created:
        client.release.set()
        writer = store._liveness_writer
        if writer is not None:
            writer.join(timeout=_EVENT_WAIT_SECONDS)


class TestDetachedLivenessWriteBehavior:
    """The loop never waits on the liveness write."""

    def test_update_returns_while_the_redis_write_is_still_parked(self, parked_store):
        # Given: a client whose set() will not return
        store, client = parked_store()

        # When
        store.update_last_loop_timestamp()

        # Then: the write is genuinely in flight and the caller is already back
        assert client.entered.wait(_EVENT_WAIT_SECONDS)
        assert not client.release.is_set()

    def test_local_timestamp_is_fresh_before_the_write_completes(self, parked_store):
        store, client = parked_store()

        store.update_last_loop_timestamp()

        # The liveness endpoint reads this age; it must not wait for Redis
        assert client.entered.wait(_EVENT_WAIT_SECONDS)
        assert store.get_last_loop_age_seconds() < _EVENT_WAIT_SECONDS

    def test_local_leg_is_stamped_before_the_redis_leg_starts(self, parked_store):
        # Given: the client observes the local timestamp from inside set()
        store, client = parked_store()

        # When
        store.update_last_loop_timestamp()
        assert client.entered.wait(_EVENT_WAIT_SECONDS)

        # Then: local first — a crash mid-write can never leave both unset
        assert client.local_seen_at_send == [True]

    def test_write_carries_the_liveness_key_and_its_ttl(self, parked_store):
        # Given: a client that returns immediately
        store, client = parked_store()
        client.release.set()

        # When
        store.update_last_loop_timestamp()
        store._liveness_writer.join(timeout=_EVENT_WAIT_SECONDS)

        # Then
        key, value, ttl = client.set_calls[0]
        assert key == WatchdogStateStore.LAST_LOOP_KEY
        assert ttl == WatchdogStateStore.LAST_LOOP_TTL_SECONDS
        # Stamped at send time, so a slow write cannot land a stale value
        assert isinstance(datetime.fromisoformat(value), datetime)

    def test_second_call_mid_write_does_not_start_a_second_writer(self, parked_store):
        # Given: the first write is parked
        store, client = parked_store()
        store.update_last_loop_timestamp()
        assert client.entered.wait(_EVENT_WAIT_SECONDS)
        first_writer = store._liveness_writer

        # When: the loop's next iteration writes again
        store.update_last_loop_timestamp()

        # Then: the Redis leg is skipped — one write in flight, no thread pile-up
        assert store._liveness_writer is first_writer
        assert len(client.set_calls) == 1
        # ...and the local timestamp was still refreshed
        assert store.get_last_loop_age_seconds() < _EVENT_WAIT_SECONDS

    def test_the_slot_re_arms_once_the_in_flight_write_returns(self, parked_store):
        # Given: a skipped call behind a parked write
        store, client = parked_store()
        store.update_last_loop_timestamp()
        assert client.entered.wait(_EVENT_WAIT_SECONDS)
        store.update_last_loop_timestamp()

        # When: the parked write finally returns
        client.release.set()
        store._liveness_writer.join(timeout=_EVENT_WAIT_SECONDS)
        client.entered.clear()
        store.update_last_loop_timestamp()

        # Then: the next call writes again — the skip is never permanent
        assert client.entered.wait(_EVENT_WAIT_SECONDS)
        assert len(client.set_calls) == 2

    def test_a_failing_write_is_swallowed_and_re_arms_the_slot(self, parked_store):
        # Given: a client whose set() raises
        store, client = parked_store(raises=RuntimeError("redis down"))
        client.release.set()

        # When: two consecutive iterations write
        store.update_last_loop_timestamp()
        store._liveness_writer.join(timeout=_EVENT_WAIT_SECONDS)
        store.update_last_loop_timestamp()
        store._liveness_writer.join(timeout=_EVENT_WAIT_SECONDS)

        # Then: no exception reached the loop, and the failure did not wedge the
        # in-flight flag (which would silently stop every later write)
        assert len(client.set_calls) == 2
        assert store.get_last_loop_age_seconds() < _EVENT_WAIT_SECONDS
