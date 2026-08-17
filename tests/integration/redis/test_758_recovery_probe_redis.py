"""758 — the recovery write-probe, executed by a real server.

The unit layer drives the whole outage lifecycle through a constructor-injected
client double, so it needs no infrastructure. The one thing a double cannot
establish is whether the double's model of Redis is right — and both defects
caught late in this design were exactly that kind: ``INCR`` against a
non-integer stored value, and which faults arrive as a bare ``ResponseError``.

So the probe's command vocabulary is replayed against a server here. If a real
Redis rejected any command in it, or answered the probe's own ``SET`` value in a
way ``INCR`` could not consume, no process would ever leave the degraded mode —
a failure the doubles would agree was fine.

Auto-skips without Redis (``requires_redis``).
"""

from __future__ import annotations

import time

import pytest
import redis

from baldur.adapters.rate_limit.redis_adapter import (
    _RECOVERY_PROBE_GATE_KEY,
    _RECOVERY_PROBE_KEY_NAME,
    RedisRateLimitStorage,
)
from baldur.metrics import drift_metrics

pytestmark = pytest.mark.requires_redis


_CONFIGURED_TTL = 3600
_COOLDOWN_SECONDS = 7200
_KEY = "recovery_probe_payment_api"


@pytest.fixture
def storage(redis_url):
    """Rate-limit storage over the test server, at the shipped TTL."""
    client = redis.from_url(redis_url, decode_responses=True)
    yield RedisRateLimitStorage(client, ttl=_CONFIGURED_TTL)
    client.close()


@pytest.fixture(autouse=True)
def _pinned_fallback_gauge():
    """The gauge is a process-global module attribute, so it is pinned at both
    ends: a case that enters the degraded mode must not leak a 1 into a later
    reader in this worker."""
    drift_metrics.set_ratelimit_fallback_mode(False)
    yield
    drift_metrics.set_ratelimit_fallback_mode(False)


def _probe_key() -> str:
    return f"{RedisRateLimitStorage.KEY_PREFIX}:{_RECOVERY_PROBE_KEY_NAME}"


def _degrade(storage) -> None:
    """Put the adapter in the state a mid-run outage leaves it in, then open the
    probe window the transition just consumed."""
    storage._enter_fallback(ConnectionError("simulated mid-run outage"))
    assert storage._fallback_mode is True, "setup failed: the outage was not latched"
    storage._probe_gate.reset(_RECOVERY_PROBE_GATE_KEY)


class TestRecoveryProbeAgainstRealRedis:
    """A live server accepts the whole vocabulary the exit depends on."""

    def test_the_write_probe_is_accepted_and_leaves_no_key_behind(
        self, storage, redis_test_client
    ):
        """``SET`` an integer-shaped value, ``INCR`` it, ``EXPIRE``, ``DEL``.

        Asserted on the returning verdict and on the empty keyspace rather than
        on the absence of an exception: a probe that raised would report "still
        degraded" silently, and one whose trailing ``DEL`` never landed would
        leave a key behind on every recovery.
        """
        _degrade(storage)

        recovered = storage._try_exit_fallback()

        assert recovered is True
        assert storage._fallback_mode is False
        assert redis_test_client.exists(_probe_key()) == 0

    def test_the_adapter_writes_to_redis_again_after_a_verified_exit(
        self, storage, redis_test_client
    ):
        """The point of the exit: coordination is fleet-wide once more.

        A cooldown installed after recovery has to land in the shared store,
        where every other worker reads it — not in the per-worker one the
        degraded window served from.
        """
        # Given: a cooldown installed while degraded, held per worker only
        _degrade(storage)
        storage.set_cooldown(_KEY, time.time() + _COOLDOWN_SECONDS)
        cooldown_key = f"{RedisRateLimitStorage.KEY_PREFIX}:{_KEY}:cooldown_until"
        assert redis_test_client.exists(cooldown_key) == 0

        # When: the server is verified writable and the next 429 re-arms the key
        assert storage._try_exit_fallback() is True
        until = time.time() + _COOLDOWN_SECONDS
        storage.set_cooldown(_KEY, until)

        # Then
        assert float(redis_test_client.get(cooldown_key)) == pytest.approx(until)
        assert storage.get_state(_KEY).is_in_cooldown is True
