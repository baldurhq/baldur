"""Redis renewal-primitive parity tests for the canary config lock (623).

Validates the renewal primitive against a real broker — the parts the
mock/memory unit tests cannot exercise:
  - the owner-checked Lua ``pexpire`` in ``extend_config_lock`` resets the TTL
    from now (reset-from-now, non-accumulating — G6/D7), and
  - the Lua owner guard / ``SET NX`` reject an ``extend`` / ``acquire`` from a
    foreign rollout id (D3 re-acquire never steals).

Adapter-scoped (OSS ``RedisCanaryRolloutStore``): imports no ``baldur_pro``
symbol, so it stays cleanly in ``tests/``. Marked ``requires_redis`` for
auto-skip when Redis is unavailable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import redis

from baldur.adapters.redis.canary_rollout import RedisCanaryRolloutStore
from baldur.settings.namespace import get_key_prefix

pytestmark = pytest.mark.requires_redis

_CONFIG_TYPE = "circuit_breaker"


def _lock_key() -> str:
    return f"{get_key_prefix()}canary:lock:{_CONFIG_TYPE}"


@pytest.fixture
def redis_client(redis_url):
    """Raw client on the same (flushed-between-tests) test DB as the conftest."""
    client = redis.from_url(redis_url)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Redis not available")
    yield client
    client.close()


@pytest.fixture
def redis_store(redis_client) -> RedisCanaryRolloutStore:
    return RedisCanaryRolloutStore(redis_client=redis_client)


class TestRedisExtendResetFromNow:
    """extend resets the TTL from now (Redis PEXPIRE), non-accumulating."""

    def test_extend_keeps_live_lock_alive_resetting_ttl_from_now(
        self, redis_store, redis_client
    ):
        # Given: a held lock with a long (100s) TTL.
        assert redis_store.acquire_config_lock(
            _CONFIG_TYPE, "r1", timeout=timedelta(seconds=100)
        )

        # When: extended by a SHORTER 30s window.
        assert redis_store.extend_config_lock(_CONFIG_TYPE, "r1", timedelta(seconds=30))

        # Then: pttl reflects ~30s (reset-from-now), NOT 100s and NOT 130s —
        # so the lock is alive but the deadline did not accumulate forward.
        pttl = redis_client.pttl(_lock_key())
        assert 0 < pttl <= 30_000
        assert redis_store.get_config_lock_owner(_CONFIG_TYPE) == "r1"


class TestRedisOwnerGuard:
    """The Lua owner guard and SET NX reject foreign rollout ids."""

    def test_extend_rejected_for_foreign_owner(self, redis_store, redis_client):
        # Given: r1 holds the lock with a 100s TTL.
        assert redis_store.acquire_config_lock(
            _CONFIG_TYPE, "r1", timeout=timedelta(seconds=100)
        )
        before = redis_client.pttl(_lock_key())

        # When: a foreign id attempts to extend.
        extended = redis_store.extend_config_lock(
            _CONFIG_TYPE, "r2", timedelta(seconds=600)
        )

        # Then: rejected by the Lua owner guard; owner + TTL unchanged.
        assert extended is False
        assert redis_store.get_config_lock_owner(_CONFIG_TYPE) == "r1"
        after = redis_client.pttl(_lock_key())
        assert after <= before  # not bumped to 600s by the foreign extend

    def test_acquire_rejected_when_held_by_other(self, redis_store):
        # Given: r1 holds the lock.
        assert redis_store.acquire_config_lock(_CONFIG_TYPE, "r1")

        # When/Then: a foreign acquire loses on SET NX (never steals).
        assert redis_store.acquire_config_lock(_CONFIG_TYPE, "r2") is False
        assert redis_store.get_config_lock_owner(_CONFIG_TYPE) == "r1"
