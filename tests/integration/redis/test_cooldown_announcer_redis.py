"""The verified all-clear against a real Redis, with two workers.

The unit layer drives the announcer's decision through a store double, which is
enough to pin *which* value it acts on. What it structurally cannot reach is the
property the guarantee actually rests on: the extension the announcer has to
notice is written by a **different process**, through its own client, and merged
by a Lua script neither worker owns. Two workers over one in-process dict share
a Python object; two workers over one Redis share nothing but the server.

Four properties therefore only exist against a server:

A. A peer's ``extend_cooldown``, merged server-side, is what the verifying read
   returns — the only path by which the extension reaches a worker that never
   saw the peer's 429.
B. The stale locally-learned expiry is not an all-clear, and the merged one is.
C. A peer's ``clear`` produces the all-clear the operator asked for, carrying
   the store's own post-clear value.
D. A Redis the adapter cannot read propagates out of ``get_state_strict`` and
   into the announcer's hold, instead of folding into "no cooldown" and
   releasing the fleet.

All tests auto-skip without Redis (``requires_redis``).
"""

from __future__ import annotations

import time

import pytest
import redis

from baldur.adapters.rate_limit.redis_adapter import RedisRateLimitStorage
from baldur.services.rate_limit_coordinator.announcer import CooldownAnnouncer

pytestmark = pytest.mark.requires_redis


KEY = "cooldown_announcer_probe"
WORKER_COOLDOWN_SECONDS = 60.0
PEER_COOLDOWN_SECONDS = 300.0

#: Well outside the sub-second precision the Lua reply preserves, so a case
#: never turns on float noise.
EXPIRY_TOLERANCE_SECONDS = 0.01


class _Clock:
    """A wall clock the case moves, seeded from the one Redis stores against."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


class _Recorder:
    """The announcer's emission seam, recording instead of reaching the bus."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, event_type_name, data, priority_name="HIGH"):
        self.calls.append({"event_type_name": event_type_name, **data})


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


@pytest.fixture
def worker_storage(redis_url):
    """The worker under test, on its own connection."""
    client = redis.from_url(redis_url, decode_responses=True)
    yield RedisRateLimitStorage(client)
    client.close()


@pytest.fixture
def peer_storage(redis_url):
    """A second worker, on a *different* connection to the same server.

    Distinct clients are the point: a shared one would let the two workers race
    through the same connection pool and socket, which is not the deployment
    the merge has to be correct for.
    """
    client = redis.from_url(redis_url, decode_responses=True)
    yield RedisRateLimitStorage(client)
    client.close()


@pytest.fixture
def announcer(worker_storage, recorder, clock):
    """The worker's announcer, driven pass by pass rather than by its thread."""
    instance = CooldownAnnouncer(storage=worker_storage, emit=recorder, clock=clock)
    yield instance
    instance.stop()


class TestPeerExtensionAcrossRedis:
    """The extension one worker never observed reaches it through the read."""

    def test_the_stale_local_expiry_is_not_an_all_clear(
        self, announcer, worker_storage, peer_storage, recorder, clock
    ):
        """The regression, with the extending write on the other connection.

        Before the verified read this worker announced at ``learned`` — the
        expiry its own 429 produced — while the server still held the peer's
        longer one, and the throttle took that as permission to ramp back into
        a provider that was still rate-limiting the fleet.
        """
        # Given a worker that observed one 429 and a peer that extended it
        learned = worker_storage.extend_cooldown(
            KEY, clock.now + WORKER_COOLDOWN_SECONDS
        )
        announcer.track(KEY, learned)
        peer_storage.extend_cooldown(KEY, clock.now + PEER_COOLDOWN_SECONDS)

        # When the moment this worker's own 429 alone would have ended arrives
        clock.now = learned + 1.0
        announced = announcer.run_once()

        # Then nothing is announced, and the peer's expiry is learned in place
        assert announced == []
        assert recorder.calls == []
        merged = worker_storage.get_state(KEY).cooldown_until
        assert merged > learned
        assert announcer.pending[KEY] == pytest.approx(
            merged, abs=EXPIRY_TOLERANCE_SECONDS
        )

    def test_the_merged_expiry_is_what_the_worker_announces_at(
        self, announcer, worker_storage, peer_storage, recorder, clock
    ):
        """Withheld, not lost — and carrying the server's value, not the local one."""
        learned = worker_storage.extend_cooldown(
            KEY, clock.now + WORKER_COOLDOWN_SECONDS
        )
        announcer.track(KEY, learned)
        merged = peer_storage.extend_cooldown(KEY, clock.now + PEER_COOLDOWN_SECONDS)

        clock.now = learned + 1.0
        announcer.run_once()
        clock.now = merged + 1.0
        announced = announcer.run_once()

        assert announced == [KEY]
        assert recorder.calls[0]["event_type_name"] == "RATE_LIMIT_COOLDOWN_END"
        assert recorder.calls[0]["cooldown_until"] == pytest.approx(
            merged, abs=EXPIRY_TOLERANCE_SECONDS
        )

    def test_a_shorter_peer_write_does_not_bring_the_all_clear_forward(
        self, announcer, worker_storage, peer_storage, recorder, clock
    ):
        """The merge is monotonic on the server, so the read cannot shorten it.

        Negative direction of the same property: a peer's ladder write landing
        under a live honored ``Retry-After`` must leave both the stored expiry
        and this worker's record where they were.
        """
        learned = worker_storage.extend_cooldown(KEY, clock.now + PEER_COOLDOWN_SECONDS)
        announcer.track(KEY, learned)
        peer_storage.extend_cooldown(KEY, clock.now + WORKER_COOLDOWN_SECONDS)

        clock.now = clock.now + WORKER_COOLDOWN_SECONDS + 1.0
        announced = announcer.run_once()

        assert announced == []
        assert recorder.calls == []
        assert announcer.pending[KEY] == pytest.approx(
            learned, abs=EXPIRY_TOLERANCE_SECONDS
        )

    def test_the_release_arrives_exactly_once_per_episode(
        self, announcer, worker_storage, recorder, clock
    ):
        """Repeated passes over a real server still yield one all-clear.

        The compare-and-delete happens in this process, but the value it is
        compared against comes back over the wire on every pass — so a read that
        answered differently each time would be visible here.
        """
        expiry = worker_storage.extend_cooldown(
            KEY, clock.now + WORKER_COOLDOWN_SECONDS
        )
        announcer.track(KEY, expiry)
        clock.now = expiry + 1.0

        for _ in range(3):
            announcer.run_once()

        assert [call["key"] for call in recorder.calls] == [KEY]


class TestOperatorClearAcrossRedis:
    """A clear issued on one connection ends the cooldown on the other."""

    def test_a_peers_clear_produces_this_workers_all_clear(
        self, announcer, worker_storage, peer_storage, recorder, clock
    ):
        """And the payload reports no cooldown, because the store reports none.

        The predecessor announced the expiry it had been armed for, which after
        an operator clear is a cooldown that exists nowhere.
        """
        expiry = worker_storage.extend_cooldown(KEY, clock.now + PEER_COOLDOWN_SECONDS)
        announcer.track(KEY, expiry)

        peer_storage.clear(KEY)
        announcer.reverify(KEY)
        announced = announcer.run_once()

        assert announced == [KEY]
        assert recorder.calls[0]["cooldown_until"] == 0.0

    def test_a_key_this_worker_never_recorded_announces_nothing(
        self, announcer, peer_storage, recorder, clock
    ):
        """Negative: record-scoped even when the server would answer.

        Announcing here would be an all-clear on behalf of workers that may
        still be holding the cooldown themselves.
        """
        peer_storage.extend_cooldown(KEY, clock.now + PEER_COOLDOWN_SECONDS)

        announcer.reverify(KEY)

        assert announcer.pending == {}
        assert announcer.run_once() == []
        assert recorder.calls == []


class TestUnreadableRedisHoldsTheAnnouncement:
    """ "Cannot tell" has to survive the real adapter, not just the double."""

    def test_a_redis_the_adapter_cannot_read_holds_instead_of_releasing(
        self, worker_storage, recorder, clock
    ):
        """The fold this whole call path exists to forbid.

        ``get_state`` answers a clean state on a backend failure, which for a
        caller deciding whether a cooldown has *ended* reads as "ended" and
        releases the fleet. The strict read has to propagate all the way from
        the adapter into the announcer's hold.
        """
        # Given a worker holding a due record and a Redis it cannot reach
        expiry = worker_storage.extend_cooldown(
            KEY, clock.now + WORKER_COOLDOWN_SECONDS
        )
        unreachable = RedisRateLimitStorage(
            redis.Redis(
                host="127.0.0.1",
                port=1,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                decode_responses=True,
            )
        )
        announcer = CooldownAnnouncer(storage=unreachable, emit=recorder, clock=clock)
        announcer.track(KEY, expiry)
        clock.now = expiry + 1.0

        # When the verification pass runs
        try:
            announced = announcer.run_once()

            # Then nothing is announced and the record is kept for the retry
            assert announced == []
            assert recorder.calls == []
            assert KEY in announcer.pending
            assert announcer._state.held_until > clock.now
        finally:
            announcer.stop()
