"""IdempotencyGate two-window TTL semantics against real Redis (595 D2/D3).

The unit layer asserts ``ttl`` forwarding on mock ``setnx`` /
``cas_dict_field`` calls; what mocks cannot prove is the two-window
semantics against real Redis TTL mechanics — the EXECUTING claim's cache
TTL (execution window) being REPLACED by the memory-window TTL at
``cas_dict_field`` time (``SET PX`` inside the Lua CAS), i.e. a
short-execution + long-memory key surviving in Redis for the memory
window after completion, and a crashed claim expiring at the execution
window.

Test Categories:
    A. Window replacement — ``mark_completed(ttl=)`` swaps the short
       execution-claim PTTL for the long memory PTTL; the completed
       record survives past the execution window and keeps blocking
    B. Crashed-claim expiry — an unmarked EXECUTING claim ABORTs
       duplicates within the execution window and expires at it
    C. Memory-window expiry — a completed record stops blocking
       duplicates after the memory window elapses

All tests require a running Redis instance (auto-skipped via
``pytestmark = pytest.mark.requires_redis``).
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from baldur.adapters.cache.redis_adapter import RedisCacheAdapter
from baldur.core.idempotency_gate import IdempotencyDecision, IdempotencyGate

pytestmark = pytest.mark.requires_redis


_PREFIX = "test:gate595:"


@pytest.fixture
def cache(redis_url) -> RedisCacheAdapter:
    """RedisCacheAdapter with a static prefix for gate window tests."""
    return RedisCacheAdapter(
        url=redis_url,
        key_prefix=_PREFIX,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
    )


@pytest.fixture
def gate(cache) -> IdempotencyGate:
    """IdempotencyGate over the real Redis adapter (atomicity validated)."""
    return IdempotencyGate(cache=cache)


# =============================================================================
# A. Window replacement — execution PTTL swapped for memory PTTL at mark time
# =============================================================================


class TestGateWindowReplacement:
    """``mark_completed``'s memory ``ttl`` replaces the EXECUTING claim's
    execution-window TTL inside the Lua CAS (one EVAL, no PEXPIRE follow-up)."""

    def test_mark_completed_replaces_execution_pttl_with_memory_pttl(
        self, gate, redis_test_client
    ):
        """
        Purpose:
            Verify the record's Redis PTTL is the execution window after
            acquire and the (much longer) memory window after mark_completed.
        Expected:
            - After acquire(ttl=2s): 0 < PTTL <= 2000 ms
            - After mark_completed(ttl=1h): 3,500,000 < PTTL <= 3,600,000 ms
        """
        key = "order:replace"

        result = gate.check_and_acquire(key, ttl=timedelta(seconds=2))
        assert result.decision == IdempotencyDecision.CONTINUE
        pttl_execution = redis_test_client.pttl(f"{_PREFIX}{key}")
        assert 0 < pttl_execution <= 2_000

        gate.mark_completed(key, result={"ok": True}, ttl=timedelta(hours=1))
        pttl_memory = redis_test_client.pttl(f"{_PREFIX}{key}")
        assert 3_500_000 < pttl_memory <= 3_600_000

    def test_completed_record_survives_past_execution_window_and_blocks(self, gate):
        """
        Purpose:
            A short-execution + long-memory key must keep blocking duplicates
            after the execution window has fully elapsed — the memory window
            governs completed-record retention (D2 decoupling).
        Expected:
            - 1.5 s after a 1 s execution window: still SKIP + cached result
        """
        key = "order:survive"
        assert (
            gate.check_and_acquire(key, ttl=timedelta(seconds=1)).decision
            == IdempotencyDecision.CONTINUE
        )
        gate.mark_completed(key, result={"ok": True}, ttl=timedelta(hours=1))

        time.sleep(1.5)  # real Redis expiry needs real elapsed time

        result = gate.check_and_acquire(key, ttl=timedelta(seconds=1))
        assert result.decision == IdempotencyDecision.SKIP
        assert result.cached_result == {"ok": True}


# =============================================================================
# B. Crashed-claim expiry — execution window bounds crash recovery
# =============================================================================


class TestGateCrashedClaimExpiry:
    """An EXECUTING claim that is never marked (worker crash) blocks within
    the execution window and becomes retryable right after it."""

    def test_unmarked_claim_aborts_within_and_expires_at_execution_window(
        self, gate, redis_test_client
    ):
        """
        Purpose:
            Verify the in-doubt window equals the execution window: a
            duplicate inside it ABORTs; after it, the claim key has expired
            in Redis and a retry CONTINUEs.
        Expected:
            - Immediately: second acquire → ABORT
            - After 1.5 s (1 s execution window): PTTL == -2 (key gone),
              third acquire → CONTINUE
        """
        key = "order:crash"
        assert (
            gate.check_and_acquire(key, ttl=timedelta(seconds=1)).decision
            == IdempotencyDecision.CONTINUE
        )

        in_doubt = gate.check_and_acquire(key, ttl=timedelta(seconds=1))
        assert in_doubt.decision == IdempotencyDecision.ABORT

        time.sleep(1.5)

        # Redis PTTL: -2 = key does not exist (the claim expired).
        assert redis_test_client.pttl(f"{_PREFIX}{key}") == -2
        retry = gate.check_and_acquire(key, ttl=timedelta(seconds=1))
        assert retry.decision == IdempotencyDecision.CONTINUE


# =============================================================================
# C. Memory-window expiry — dedup memory ends at the memory window
# =============================================================================


class TestGateMemoryWindowExpiry:
    """A completed record blocks duplicates only for the memory window."""

    def test_completed_record_stops_blocking_after_memory_window(self, gate):
        """
        Purpose:
            Verify a duplicate within the memory window is SKIPped and one
            after expiry runs again (the record is gone from Redis).
        Expected:
            - Immediately after mark_completed(ttl=1s): SKIP
            - After 1.5 s: CONTINUE
        """
        key = "order:memexp"
        assert (
            gate.check_and_acquire(key, ttl=timedelta(seconds=30)).decision
            == IdempotencyDecision.CONTINUE
        )
        gate.mark_completed(key, result={"ok": True}, ttl=timedelta(seconds=1))

        within = gate.check_and_acquire(key, ttl=timedelta(seconds=30))
        assert within.decision == IdempotencyDecision.SKIP

        time.sleep(1.5)

        after = gate.check_and_acquire(key, ttl=timedelta(seconds=30))
        assert after.decision == IdempotencyDecision.CONTINUE
