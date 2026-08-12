"""Monotonic cooldown against a real Redis (#754 D2).

The unit layer (``tests/unit/rate_limit/test_storage_adapters.py``) pins every
max-merge that runs in Python, plus the scripted path's call shape and reply
handling. What it structurally cannot reach is the shipped default: the merge
and the key TTL are computed *inside* a Lua script, and no client double
executes Lua. Four properties therefore only exist against a server:

A. The merge is atomic — concurrent writers cannot lose the longer cooldown.
B. The key's TTL is derived server-side from the effective expiry, so a short
   candidate cannot shrink a long cooldown's TTL and let Redis evict it early.
C. The reply is a string, so the sub-second part survives (a Lua number reply
   is converted to a Redis integer and truncated).
D. ``EVALSHA`` keeps working across a ``SCRIPT FLUSH`` — the registry's
   NOSCRIPT recovery is on this path.

All tests auto-skip without Redis (``requires_redis``).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from baldur.adapters.rate_limit.redis_adapter import (
    _COOLDOWN_TTL_MARGIN_SECONDS,
    RedisRateLimitStorage,
)

pytestmark = pytest.mark.requires_redis


CONFIGURED_TTL = 3600
HONORED_HEADER_COOLDOWN_SECONDS = 7200
LADDER_COOLDOWN_SECONDS = 10

_KEY = "extend_cooldown_probe"


@pytest.fixture
def storage(redis_url):
    """Redis rate-limit storage over the test server, at the shipped TTL."""
    client = redis.from_url(redis_url, decode_responses=True)
    yield RedisRateLimitStorage(client, ttl=CONFIGURED_TTL)
    client.close()


def _cooldown_redis_key(key: str = _KEY) -> str:
    return f"{RedisRateLimitStorage.KEY_PREFIX}:{key}:cooldown_until"


class TestRedisExtendCooldownAtomicity:
    """The merge happens on the server, so unordered writers converge."""

    def test_a_shorter_write_does_not_shorten_a_live_cooldown(self, storage):
        """The contract itself, executed by Redis rather than by Python."""
        now = time.time()
        honored = now + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown(_KEY, honored)

        effective = storage.extend_cooldown(_KEY, now + LADDER_COOLDOWN_SECONDS)

        assert effective == pytest.approx(honored, abs=1e-5)
        assert storage.get_state(_KEY).cooldown_until == pytest.approx(
            honored, abs=1e-5
        )

    def test_concurrent_short_writers_cannot_shorten_an_established_cooldown(
        self, storage, redis_test_client
    ):
        """The property a client-side read-modify-write cannot give across processes.

        An honored ``Retry-After`` is established, then sixteen workers race
        headerless ladder writes at it with no coordination — the shape of a
        real 429 storm across a fleet. Every one of them has to come back with
        the long cooldown, and the key has to still hold it once they are done.

        The candidates are deliberately all *short* and all *after* the long
        one: an interleaved mix lets a last-writer-wins store pass whenever the
        write that happens to land last is a long one, which is most of the
        time.
        """
        now = time.time()
        longest = now + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown(_KEY, longest)
        candidates = [now + LADDER_COOLDOWN_SECONDS + index for index in range(16)]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda c: storage.extend_cooldown(_KEY, c), candidates)
            )

        stored = float(redis_test_client.get(_cooldown_redis_key()))
        assert stored == pytest.approx(longest, abs=1e-5)
        # Not one writer was ever told a cooldown earlier than the established one.
        assert min(results) == pytest.approx(longest, abs=1e-5)

    def test_the_reply_keeps_its_sub_second_precision(self, storage):
        """A truncated reply would arm the all-clear up to a second early."""
        candidate = time.time() + 123.456789

        effective = storage.extend_cooldown(_KEY, candidate)

        assert effective != int(effective)
        assert effective == pytest.approx(candidate, abs=1e-5)

    def test_the_last_updated_marker_is_written_beside_the_cooldown(
        self, storage, redis_test_client
    ):
        """It stays outside the script — a second key would make the call multi-key.

        The shared registry rejects keys spanning hash slots and Redis Cluster
        refuses an ARGV-named second key outright, so the bookkeeping write is a
        plain unordered ``SET``. Nothing branches on it; it just has to exist.
        """
        storage.extend_cooldown(_KEY, time.time() + LADDER_COOLDOWN_SECONDS)

        marker = redis_test_client.get(
            f"{RedisRateLimitStorage.KEY_PREFIX}:{_KEY}:last_updated"
        )
        assert marker is not None
        assert float(marker) == pytest.approx(time.time(), abs=30)


class TestRedisExtendCooldownTtlCoverage:
    """The key must outlive the cooldown it carries — Redis reports the real TTL."""

    def test_a_cooldown_longer_than_the_configured_ttl_gets_a_covering_ttl(
        self, storage, redis_test_client
    ):
        """``redis_ttl`` bottoms out at 60s and an honored header tops out at a day.

        With the configured TTL below the expiry, Redis deletes a live cooldown
        and the whole fleet resumes — an earlier movement of the shared expiry
        through the one writer the monotonic merge cannot see.
        """
        storage.extend_cooldown(_KEY, time.time() + HONORED_HEADER_COOLDOWN_SECONDS)

        ttl = redis_test_client.ttl(_cooldown_redis_key())
        assert ttl >= HONORED_HEADER_COOLDOWN_SECONDS + _COOLDOWN_TTL_MARGIN_SECONDS - 1

    def test_a_short_second_write_does_not_shrink_the_ttl(
        self, storage, redis_test_client
    ):
        """The TTL follows the effective expiry, which only the script knows.

        Computing it from the caller's candidate is the subtle half of the same
        bug the merge fixes: the value survives the second write but the key
        does not.
        """
        now = time.time()
        storage.extend_cooldown(_KEY, now + HONORED_HEADER_COOLDOWN_SECONDS)

        storage.extend_cooldown(_KEY, now + LADDER_COOLDOWN_SECONDS)

        ttl = redis_test_client.ttl(_cooldown_redis_key())
        assert ttl >= HONORED_HEADER_COOLDOWN_SECONDS + _COOLDOWN_TTL_MARGIN_SECONDS - 1

    def test_a_short_cooldown_keeps_the_configured_retention(
        self, storage, redis_test_client
    ):
        """Boundary the other way: coverage never shortens the configured TTL."""
        storage.extend_cooldown(_KEY, time.time() + LADDER_COOLDOWN_SECONDS)

        assert redis_test_client.ttl(_cooldown_redis_key()) == pytest.approx(
            CONFIGURED_TTL, abs=2
        )


class TestRedisExtendCooldownScriptRecovery:
    """The script survives a server that forgot it."""

    def test_the_merge_still_works_after_a_script_flush(
        self, storage, redis_test_client
    ):
        """``SCRIPT FLUSH`` invalidates the cached SHA mid-storm.

        The shared registry reloads on NOSCRIPT; if that recovery did not reach
        this call, ``extend_cooldown`` would raise and the caller's fail-open
        wrap would install no cooldown at all.
        """
        now = time.time()
        honored = now + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown(_KEY, honored)

        redis_test_client.script_flush()

        effective = storage.extend_cooldown(_KEY, now + LADDER_COOLDOWN_SECONDS)
        assert effective == pytest.approx(honored, abs=1e-5)

    def test_scripting_is_used_rather_than_the_read_modify_write_fallback(
        self, storage
    ):
        """Negative: a real Redis must not land on the degraded path.

        The fallback is a genuine shipped path, and a silent slide onto it would
        cost cross-process atomicity without anything failing — so its flag
        staying clear is the assertion that the scripted path actually ran.
        """
        storage.extend_cooldown(_KEY, time.time() + LADDER_COOLDOWN_SECONDS)

        assert storage._script_fallback is False


class TestRedisGetStateStrictAgainstRealRedis:
    """``get_state_strict`` reads the same value as ``get_state`` on a healthy server."""

    def test_both_reads_agree_on_a_live_cooldown(self, storage):
        cooldown_until = time.time() + HONORED_HEADER_COOLDOWN_SECONDS
        storage.extend_cooldown(_KEY, cooldown_until)

        assert storage.get_state_strict(_KEY).cooldown_until == pytest.approx(
            storage.get_state(_KEY).cooldown_until, abs=1e-6
        )

    def test_an_absent_key_reads_as_no_cooldown_rather_than_raising(self, storage):
        """Strict is about backend failure, not about a missing key.

        A key that was never written is a fact the backend reported, so folding
        it into a clean state is correct — conflating it with an unreachable
        backend would make every first 429 look like an outage.
        """
        state = storage.get_state_strict("never_written_key")

        assert state.cooldown_until == 0.0
        assert state.is_in_cooldown is False
