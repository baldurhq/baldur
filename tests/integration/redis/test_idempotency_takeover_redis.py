"""IdempotencyGate atomic takeover against real Redis (673 D1 / G1 / D8).

Two cross-component behaviors the in-memory / mocked unit doubles cannot prove
(``MockRedisClient`` does not execute Lua, and the memory adapter is loop-atomic
so it cannot exhibit the pre-673 two-step race):

1. **Single-winner takeover under genuine contention** — N concurrent retriers on
   a pre-seeded ``failed`` / stale-``executing`` key against a live server elect
   exactly one ``CONTINUE`` (the atomic ``cas_takeover`` Lua ``EVAL``); the rest
   ``ABORT``. The pre-673 ``delete()+setnx()`` two-step would let two interleave
   and both proceed → double-execute.
2. **Record-shape ↔ Lua contract** — the takeover Lua reads ``status`` /
   ``started_at`` out of a record ACTUALLY written by the gate's own
   serialization, so a future record-schema / serialization change that desyncs
   the Lua field access fails loudly here instead of silently mis-deduping.

All tests require a running Redis instance (auto-skipped via
``pytestmark = pytest.mark.requires_redis``).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.idempotency_gate import IdempotencyDecision, IdempotencyGate

pytestmark = pytest.mark.requires_redis

_PREFIX = "test:take673:"


def _make_cache(redis_url) -> RedisCacheAdapter:
    return RedisCacheAdapter(
        url=redis_url,
        key_prefix=_PREFIX,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


@pytest.fixture
def cache(redis_url) -> RedisCacheAdapter:
    return _make_cache(redis_url)


@pytest.fixture
def gate(cache) -> IdempotencyGate:
    return IdempotencyGate(cache=cache)


# =============================================================================
# Single-winner takeover under genuine concurrency (real Lua EVAL contention)
# =============================================================================


class TestRedisTakeoverSingleWinner:
    """N concurrent retriers on ONE non-fresh key ⇒ exactly one CONTINUE."""

    @staticmethod
    def _race(redis_url, key: str, n: int) -> list[IdempotencyDecision]:
        """Fire ``n`` gate acquires simultaneously (barrier-synced), each on its
        own adapter/gate (distinct workers), and return their decisions."""
        barrier = threading.Barrier(n)

        def retry(_: int) -> IdempotencyDecision:
            g = IdempotencyGate(cache=_make_cache(redis_url))
            barrier.wait()  # maximize real contention on the single key
            return g.check_and_acquire(key).decision

        with ThreadPoolExecutor(max_workers=n) as ex:
            return list(ex.map(retry, range(n)))

    def test_concurrent_retries_on_failed_key_elect_single_winner(
        self, gate, redis_url
    ):
        """A pre-seeded ``failed`` record (written via the gate) + N racing
        retriers ⇒ exactly one takeover wins CONTINUE, the rest ABORT."""
        key = "order:failed-race"
        # Seed a failed record through the gate's own write path.
        assert gate.check_and_acquire(key).decision == IdempotencyDecision.CONTINUE
        gate.mark_failed(key)

        n = 16
        decisions = self._race(redis_url, key, n)

        assert decisions.count(IdempotencyDecision.CONTINUE) == 1
        assert decisions.count(IdempotencyDecision.ABORT) == n - 1

    def test_concurrent_retries_on_stale_key_elect_single_winner(
        self, cache, redis_url
    ):
        """A pre-seeded stale ``executing`` record (``started_at`` 2h ago, well
        past the 1800 s + tolerance threshold) + N racing retriers ⇒ one CONTINUE."""
        key = "order:stale-race"
        cache.set(
            key,
            {"status": "executing", "started_at": time.time() - 7200, "retry_count": 0},
            ttl=timedelta(seconds=1800),
        )

        n = 16
        decisions = self._race(redis_url, key, n)

        assert decisions.count(IdempotencyDecision.CONTINUE) == 1
        assert decisions.count(IdempotencyDecision.ABORT) == n - 1


# =============================================================================
# Record-shape ↔ Lua contract (gate-written record read by the takeover Lua)
# =============================================================================


class TestRedisTakeoverRecordShapeLuaContract:
    """The takeover Lua decodes ``status`` / ``started_at`` from a record written
    by the gate's serialization — a serialization desync would fail these loudly
    (a False takeover → ABORT) instead of silently mis-deduping (673 4b)."""

    def test_gate_written_failed_record_status_is_read_by_lua(self, gate):
        """A ``failed`` record written via ``mark_failed`` is decoded by the Lua
        (``status == 'failed'`` → takeover) → CONTINUE with incremented retry."""
        key = "order:shape-failed"
        assert gate.check_and_acquire(key).decision == IdempotencyDecision.CONTINUE
        gate.mark_failed(key)  # writes {"status": "failed", ...} via cas_dict_field

        result = gate.check_and_acquire(key)

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 1

    def test_gate_written_stale_started_at_is_read_by_lua(self, gate, cache):
        """A stale ``executing`` record's ``started_at`` is decoded and compared
        against ``stale_before`` inside the Lua → takeover CONTINUEs, carrying the
        incremented retry_count."""
        key = "order:shape-stale"
        cache.set(
            key,
            {"status": "executing", "started_at": time.time() - 7200, "retry_count": 3},
            ttl=timedelta(seconds=1800),
        )

        result = gate.check_and_acquire(key)

        assert result.decision == IdempotencyDecision.CONTINUE
        assert result.retry_count == 4

    def test_fresh_executing_record_started_at_blocks_takeover_in_lua(
        self, gate, cache
    ):
        """Negative control: a fresh ``executing`` record (``started_at`` now) is
        NOT takeable — the Lua's ``started_at < stale_before`` check returns 0 →
        ABORT. Proves the comparison is real, not an always-takeover."""
        key = "order:shape-fresh"
        cache.set(
            key,
            {"status": "executing", "started_at": time.time(), "retry_count": 0},
            ttl=timedelta(seconds=1800),
        )

        result = gate.check_and_acquire(key)

        assert result.decision == IdempotencyDecision.ABORT
