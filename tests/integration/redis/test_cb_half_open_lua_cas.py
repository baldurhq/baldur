"""
CB HALF_OPEN Distributed Counter — Lua CAS Integration Tests (476 + 498)

Verifies the Redis Lua atomic ``try_acquire_half_open_slot`` and
``record_success_with_close_check`` primitives against a real Redis
instance. Mock-based unit tests cannot exercise Lua's atomicity
guarantee against concurrent ``eval()`` calls, nor the real-Redis
round-trip behavior that the §392 cluster-wide contract and the §498
exactly-one CLOSED-emit contract depend on.

Test Categories:
    A. Lua state-machine round-trip (single-thread, sequential):
        - closed → no_op (state unchanged)
        - open → transition (count=1, success_count=0, watermark set)
        - half_open under limit → increment (count++, watermark stable)
        - half_open at limit + fresh watermark → rejected
        - half_open at limit + stale watermark → stuck_recovery (D8)
    B. Cross-worker concurrent atomicity (Cat 6.4 / §392):
        - 50 threads + Barrier from state=OPEN, limit=10 →
          exactly 10 acquires; exactly 1 transition winner; counter
          settles at exactly limit (no overshoot)
        - 30 threads at limit + fresh watermark → all rejected, counter
          unchanged
    C. Layered L1 writeback after L2 Lua transition (D6 / G11):
        - L2 OPEN→HALF_OPEN via Lua → L1 immediately reflects half_open
          with success_count=0 (no waiting for the next ~5s drift tick)
        - HALF_OPEN→HALF_OPEN increment writeback does NOT reset L1
          success_count (only OPEN→HALF_OPEN transition does)
    D. Drift reconciler XOR rule with real L2 timestamp (D7 / G12):
        - Stale L1=OPEN (older ts) vs L2=HALF_OPEN (newer ts written by
          Lua) → reconciler resolves by timestamp (TIMESTAMP_HALF_OPEN_L2)
          rather than "Most Restrictive Wins" reverting the transition
    E. Cross-worker close-check atomicity (498 D1 / F11):
        - 50 threads + Barrier from state=HALF_OPEN, threshold=5 →
          exactly 1 did_close=True winner; threshold-1 half_open
          increments with unique success_count values; remainder are
          state=closed race-losers
        - threshold=1 boundary: first attempt wins, all others race-lose
        - state=closed pre-contention (post-crash convergence) → all
          attempts race-lose with no HSET
        - state=open (stale-L2 sentinel) → all attempts return state=open
          sentinel without any HSET
    F. Layered L2-authoritative close-check (498 D6):
        - 50 threads via Layered router from L2=HALF_OPEN, threshold=1 →
          exactly 1 did_close=True winner cluster-wide; L1 converges to
          CLOSED via writeback for both winner and race-losers

All tests require a running Redis instance.
Marked with @pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import pytest

from baldur.adapters.memory.drift_reconciliation import (
    DriftReconciler,
    DriftReconciliationResult,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)

pytestmark = pytest.mark.requires_redis


SVC = "payment-api"


def _cb_key(repo, service_name: str = SVC) -> str:
    """Compute the actual Redis hash key the repo writes to.

    The repo's backend prefix depends on `use_dynamic_prefix` /
    `get_effective_key_prefix()`, so derive it from the backend instead
    of hardcoding `baldur:cb:` (which would skip namespace tests).
    """
    return repo._backend._get_full_key(f"cb:{service_name}")


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset runtime-scoped Redis negative cache so backend can init Redis."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


# =============================================================================
# A. Lua Script State-Machine Round-Trip
# =============================================================================


class TestLuaAcquireRoundTrip:
    """Verifies the 5 Lua state-machine branches against real Redis."""

    def test_closed_state_returns_no_op_without_state_change(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            CLOSED state must not transition to HALF_OPEN via try_acquire.
            That transition is reserved for OPEN→HALF_OPEN after the
            recovery_timeout elapses.
        Expected:
            - Returns (False, "closed", "closed"); marker == "no_op"
            - state remains "closed"; no counter mutation
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="closed")

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            SVC, limit=10, stuck_timeout_seconds=60
        )

        assert allowed is False
        assert prev == "closed"
        assert new_state == "closed"
        assert repo._last_acquire_marker == "no_op"
        assert redis_test_client.hget(_cb_key(repo), "state") == "closed"

    def test_open_state_transitions_to_half_open_with_count_one(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            OPEN state acquire must atomically transition to HALF_OPEN,
            initialize the counter to 1, reset success_count to 0, and
            stamp the window watermark.
        Expected:
            - Returns (True, "open", "half_open"); marker == "transition"
            - half_open_request_count == "1", success_count == "0"
            - half_open_window_started_at is a fresh unix timestamp
        """
        repo = redis_circuit_breaker_repository
        # success_count=42 must be reset by the Lua transition.
        repo.update_state(SVC, state="open", success_count=42)

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            SVC, limit=10, stuck_timeout_seconds=60
        )

        assert allowed is True
        assert prev == "open"
        assert new_state == "half_open"
        assert repo._last_acquire_marker == "transition"

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "half_open"
        assert data["half_open_request_count"] == "1"
        assert data["success_count"] == "0"
        # Watermark is unix-seconds float string; must be within last 5 seconds.
        watermark = float(data["half_open_window_started_at"])
        assert abs(watermark - time.time()) < 5.0

    def test_half_open_under_limit_increments_counter(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            HALF_OPEN with count<limit must increment the counter without
            transitioning state and without resetting the watermark.
        Expected:
            - Each acquire returns (True, "half_open", "half_open")
              with marker == "increment"
            - half_open_request_count increments by 1 each call
            - half_open_window_started_at unchanged across increments
        """
        repo = redis_circuit_breaker_repository
        # Initial OPEN→HALF_OPEN transition installs the watermark.
        repo.update_state(SVC, state="open")
        repo.try_acquire_half_open_slot(SVC, limit=10, stuck_timeout_seconds=60)
        cb_key = _cb_key(repo)
        watermark_after_transition = redis_test_client.hget(
            cb_key, "half_open_window_started_at"
        )

        for expected_count in (2, 3, 4):
            allowed, prev, new_state = repo.try_acquire_half_open_slot(
                SVC, limit=10, stuck_timeout_seconds=60
            )
            assert allowed is True
            assert prev == "half_open"
            assert new_state == "half_open"
            assert repo._last_acquire_marker == "increment"
            assert (
                int(redis_test_client.hget(cb_key, "half_open_request_count"))
                == expected_count
            )

        assert (
            redis_test_client.hget(cb_key, "half_open_window_started_at")
            == watermark_after_transition
        )

    def test_half_open_at_limit_with_fresh_watermark_returns_rejected(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            HALF_OPEN with count==limit and a fresh watermark must reject
            new acquires until the window closes via record_success or
            record_failure paths.
        Expected:
            - Returns (False, "half_open", "half_open"); marker == "rejected"
            - half_open_request_count unchanged
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        # Fill the window via Lua (each call paths through
        # transition→increment to populate count and watermark naturally).
        for _ in range(3):
            repo.try_acquire_half_open_slot(SVC, limit=3, stuck_timeout_seconds=60)

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            SVC, limit=3, stuck_timeout_seconds=60
        )

        assert allowed is False
        assert prev == "half_open"
        assert new_state == "half_open"
        assert repo._last_acquire_marker == "rejected"
        assert (
            int(redis_test_client.hget(_cb_key(repo), "half_open_request_count")) == 3
        )

    def test_half_open_at_limit_with_stale_watermark_triggers_stuck_recovery(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            D8 stuck-recovery branch — HALF_OPEN with count==limit and
            (now - watermark) > stuck_timeout. The next acquire must
            auto-reset the window (count=1, success_count=0, fresh
            watermark) and surface a "stuck_recovery" marker.
        Expected:
            - Returns (True, "half_open", "half_open"); marker == "stuck_recovery"
            - half_open_request_count reset to 1 (the new acquire)
            - success_count reset to 0
            - half_open_window_started_at refreshed to now
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        for _ in range(3):
            repo.try_acquire_half_open_slot(SVC, limit=3, stuck_timeout_seconds=60)
        # Inject ancient watermark (1970) and a non-zero success_count so
        # the auto-reset branch's reset effect is observable.
        cb_key = _cb_key(repo)
        redis_test_client.hset(cb_key, "half_open_window_started_at", "1.0")
        redis_test_client.hset(cb_key, "success_count", "5")

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            SVC, limit=3, stuck_timeout_seconds=60
        )

        assert allowed is True
        assert prev == "half_open"
        assert new_state == "half_open"
        assert repo._last_acquire_marker == "stuck_recovery"

        data = redis_test_client.hgetall(cb_key)
        assert data["half_open_request_count"] == "1"
        assert data["success_count"] == "0"
        watermark = float(data["half_open_window_started_at"])
        assert abs(watermark - time.time()) < 5.0


# =============================================================================
# B. Cross-Worker Concurrent Atomicity (Cat 6.4 / §392)
# =============================================================================


class TestCrossWorkerAtomicity:
    """Verifies §392 cluster-wide 'exactly N total' contract via Lua atomicity."""

    def test_fifty_threads_compete_only_limit_acquires_succeed(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            50 concurrent threads attempt try_acquire_half_open_slot from
            state=OPEN with limit=10. Lua atomicity must serialize them
            so that exactly 10 acquires succeed cluster-wide. Exactly one
            thread observes prev_state=="open" (the transition winner);
            the remaining 9 successes are increments, and the 40 failures
            are rejections.
        Expected:
            - Exactly limit threads return allowed=True
            - Exactly 1 transition winner (prev=="open")
            - limit-1 increments (prev=="half_open", allowed=True)
            - thread_count-limit rejections (allowed=False)
            - Final counter == limit (no overshoot)
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")

        limit = 10
        thread_count = 50
        results: list[tuple[bool, str, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            allowed, prev, new_state = repo.try_acquire_half_open_slot(
                SVC, limit=limit, stuck_timeout_seconds=60
            )
            with results_lock:
                results.append((allowed, prev, new_state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        successes = [r for r in results if r[0]]
        failures = [r for r in results if not r[0]]
        transitions = [r for r in successes if r[1] == "open"]
        increments = [r for r in successes if r[1] == "half_open"]

        assert len(successes) == limit, (
            f"§392 contract violated: expected exactly {limit} acquires, "
            f"got {len(successes)}"
        )
        assert len(failures) == thread_count - limit
        assert len(transitions) == 1, (
            f"expected exactly 1 transition winner, got {len(transitions)} — "
            "duplicate CIRCUIT_BREAKER_HALF_OPENED events would fire"
        )
        assert len(increments) == limit - 1

        final_count = int(
            redis_test_client.hget(_cb_key(repo), "half_open_request_count")
        )
        assert final_count == limit

    def test_concurrent_attempts_at_limit_all_rejected_no_overshoot(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            When the HALF_OPEN window is already full and the watermark is
            fresh, 30 concurrent attempts must all be rejected — Lua's
            count>=limit check must fire on every attempt without overshoot.
        Expected:
            - All 30 threads return allowed=False
            - half_open_request_count unchanged from pre-contention value
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        for _ in range(5):
            repo.try_acquire_half_open_slot(SVC, limit=5, stuck_timeout_seconds=60)
        cb_key = _cb_key(repo)
        count_before = int(redis_test_client.hget(cb_key, "half_open_request_count"))
        assert count_before == 5

        thread_count = 30
        results: list[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            allowed, _prev, _new = repo.try_acquire_half_open_slot(
                SVC, limit=5, stuck_timeout_seconds=60
            )
            with results_lock:
                results.append(allowed)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        assert sum(1 for r in results if r) == 0
        assert (
            int(redis_test_client.hget(cb_key, "half_open_request_count"))
            == count_before
        )


# =============================================================================
# C. Layered Repository L1 Writeback After L2 Lua Transition (D6 / G11)
# =============================================================================


@pytest.fixture
def layered_cb_repo(redis_circuit_breaker_repository):
    """Layered (L1 in-memory + L2 real Redis) CB repo for D6 tests.

    Notes:
        - Initial _load_from_l2_with_timeout calls l2.get_all_states; the
          Redis adapter implements it, so L1 is hydrated from L2 on init.
        - The default 50ms L2 timeout can be too tight on slow CI runners,
          so the fixture overrides _get_timeout_seconds to 5 s.
    """
    repo = LayeredCircuitBreakerStateRepository(
        l2_repo=redis_circuit_breaker_repository,
        adapter_type="redis",
    )
    repo._get_timeout_seconds = lambda: 5.0
    yield repo
    reset_layered_repository_executor()


class TestLayeredL1Writeback:
    """Verifies the D6 / G11 L1 writeback after L2 Lua transition."""

    def test_l2_open_to_half_open_lua_writes_back_to_stale_l1(
        self,
        layered_cb_repo,
        redis_circuit_breaker_repository,
    ):
        """
        Purpose:
            When the L2 Lua transitions OPEN→HALF_OPEN, the layered repo
            must synchronously writeback the new state to L1 so that
            subsequent record_failure / record_success calls (which read
            L1 first) don't take the wrong branch on stale L1=open
            (476 D6 / G11).
        Expected:
            - L2 returns (True, "open", "half_open")
            - L1 reflects state="half_open" and success_count=0
              immediately (no waiting for the ~5 s drift tick)
        """
        repo = layered_cb_repo
        # Seed L2 with state=open via the Redis adapter directly.
        redis_circuit_breaker_repository.update_state(
            "checkout-svc", state="open", success_count=99
        )
        # Manually seed L1 with stale state=open / success_count=99 so we
        # can verify the writeback overwrites it.
        repo._l1.get_or_create("checkout-svc")
        repo._l1.update_state("checkout-svc", state="open", success_count=99)
        l1_before = repo._l1.get_by_service_name("checkout-svc")
        assert l1_before.state == "open"
        assert l1_before.success_count == 99

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            "checkout-svc", limit=5, stuck_timeout_seconds=60
        )

        assert allowed is True
        assert prev == "open"
        assert new_state == "half_open"
        # D6 writeback contract: L1 immediately reflects the L2 decision.
        l1_after = repo._l1.get_by_service_name("checkout-svc")
        assert l1_after.state == "half_open"
        assert l1_after.success_count == 0

    def test_l2_increment_writeback_preserves_l1_success_count(
        self,
        layered_cb_repo,
        redis_circuit_breaker_repository,
    ):
        """
        Purpose:
            For HALF_OPEN→HALF_OPEN increment (no state change), the L1
            writeback must NOT reset success_count. Per D6, success_count
            is only forced to 0 when prev=="open" AND new_state=="half_open"
            (the actual transition).
        Expected:
            - L1's existing success_count is preserved across an increment
              writeback
        """
        repo = layered_cb_repo
        redis_circuit_breaker_repository.update_state("api-svc", state="open")
        # First call performs the actual transition (writeback resets L1
        # success_count to 0).
        repo.try_acquire_half_open_slot("api-svc", limit=5, stuck_timeout_seconds=60)
        # Bump L1 success_count to a sentinel so we can detect any
        # spurious overwrite by the increment writeback.
        repo._l1.update_state("api-svc", state="half_open", success_count=7)

        allowed, prev, new_state = repo.try_acquire_half_open_slot(
            "api-svc", limit=5, stuck_timeout_seconds=60
        )

        assert allowed is True
        assert prev == "half_open"
        assert new_state == "half_open"
        l1_after = repo._l1.get_by_service_name("api-svc")
        assert l1_after.state == "half_open"
        assert l1_after.success_count == 7


# =============================================================================
# D. Drift Reconciler XOR Rule with Real L2 Timestamp (D7 / G12)
# =============================================================================


class TestDriftReconcilerWithRealL2:
    """D7 HALF_OPEN-XOR resolution against real Redis-stored timestamps."""

    def test_stale_l1_open_loses_to_l2_half_open_via_xor_timestamp(
        self,
        redis_circuit_breaker_repository,
    ):
        """
        Purpose:
            Cross-worker scenario — Worker A's Lua transitions L2
            OPEN→HALF_OPEN (newer timestamp). Worker B's L1 still
            holds stale OPEN (older timestamp). Pre-476 "Most Restrictive
            Wins" would propagate L1=OPEN back to L2 and reverse the
            transition. D7's XOR exception must instead resolve by
            timestamp, so L2=half_open survives (476 G12).
        Expected:
            - reconcile() returns ("half_open", TIMESTAMP_HALF_OPEN_L2)
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        # Real Lua call writes a real timestamp to L2.
        repo.try_acquire_half_open_slot(SVC, limit=5, stuck_timeout_seconds=60)

        l2_state_after = repo.get_state(SVC)
        assert l2_state_after.state == "half_open"
        assert l2_state_after.updated_at is not None

        # Stale L1: same service, older timestamp, state=open.
        l1_updated_at = l2_state_after.updated_at - timedelta(seconds=10)

        reconciler = DriftReconciler(min_jitter_seconds=0.0, max_jitter_seconds=0.0)
        winner_state, result = reconciler.reconcile(
            service_name=SVC,
            l1_state="open",
            l2_state="half_open",
            l1_updated_at=l1_updated_at,
            l2_updated_at=l2_state_after.updated_at,
        )

        assert winner_state == "half_open"
        assert result == DriftReconciliationResult.TIMESTAMP_HALF_OPEN_L2


# =============================================================================
# E. Cross-Worker Close-Check Atomicity (498 D1 / F11)
# =============================================================================


class TestCloseCheckCrossWorkerAtomicity:
    """Verifies §498 exactly-one CIRCUIT_BREAKER_CLOSED contract via Lua atomicity.

    Mirrors the Cat B harness for ``try_acquire_half_open_slot`` but for
    the close-check primitive that fires on HALF_OPEN→CLOSED. Mock-based
    unit tests verify return-array parsing only; real Redis EVAL is the
    only authoritative substrate for the cross-process exactly-one
    contract (498 G1, F11 race coverage).
    """

    def test_concurrent_close_check_exactly_one_did_close_winner(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            50 concurrent threads from state=HALF_OPEN, success_count=0,
            threshold=5. Lua atomicity must serialize HMGET-increment-HSET
            so that exactly the threshold-crossing thread sees did_close=True;
            (threshold-1) earlier threads receive unique half_open increments;
            the remaining 45 arrive after the close and race-lose on state=closed.
        Expected:
            - exactly 1 did_close=True winner (state='closed')
            - threshold-1 == 4 did_close=False half_open increments with
              UNIQUE success_count ∈ {1, 2, 3, 4}
            - thread_count-threshold == 45 race-losers (state='closed')
            - Final L2 hash: state='closed', success_count='0',
              half_open_request_count='0'
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="half_open", success_count=0)

        threshold = 5
        thread_count = 50
        results: list[tuple[bool, str, int]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_success_with_close_check(
                SVC, success_threshold=threshold
            )
            with results_lock:
                results.append(
                    (
                        outcome.did_close,
                        outcome.state.state,
                        outcome.state.success_count,
                    )
                )

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        half_open_increments = [r for r in results if not r[0] and r[1] == "half_open"]
        closed_race_losers = [r for r in results if not r[0] and r[1] == "closed"]

        assert len(winners) == 1, (
            f"498 exactly-one contract violated: {len(winners)} did_close=True "
            f"winners across {thread_count} workers (expected 1)"
        )
        assert winners[0][1] == "closed"

        assert len(half_open_increments) == threshold - 1
        # Increment success_count must form the contiguous set {1..threshold-1}.
        increment_counts = sorted(r[2] for r in half_open_increments)
        assert increment_counts == list(range(1, threshold)), (
            "Lua HMGET-HSET interleaved: success_count increments collided — "
            f"expected {list(range(1, threshold))}, got {increment_counts}"
        )

        assert len(closed_race_losers) == thread_count - threshold

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "closed"
        assert data["success_count"] == "0"
        assert data["half_open_request_count"] == "0"

    def test_concurrent_close_check_threshold_one_single_winner(
        self, redis_circuit_breaker_repository
    ):
        """
        Purpose:
            threshold=1 boundary — the first close-check attempt is itself
            the threshold-crossing close (Lua increment branch never fires).
            Exactly one winner; all others observe state='closed' on arrival.
        Expected:
            - 1 did_close=True winner (state='closed')
            - thread_count-1 race-losers (state='closed', success_count=0)
            - NO half_open transient leaks
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="half_open", success_count=0)

        thread_count = 50
        results: list[tuple[bool, str, int]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_success_with_close_check(SVC, success_threshold=1)
            with results_lock:
                results.append(
                    (
                        outcome.did_close,
                        outcome.state.state,
                        outcome.state.success_count,
                    )
                )

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        race_losers = [r for r in results if not r[0]]

        assert len(winners) == 1
        assert winners[0][1] == "closed"
        assert len(race_losers) == thread_count - 1
        assert all(r[1] == "closed" for r in race_losers)
        assert all(r[2] == 0 for r in race_losers)

    def test_concurrent_close_check_already_closed_all_race_losers(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            Post-crash convergence — L2 is already CLOSED (e.g., crashed +
            rebooted during HALF_OPEN window). All concurrent close-check
            attempts must observe Lua's state=='closed' branch with no HSET.
        Expected:
            - All threads return did_close=False, state='closed'
            - Lua took the no-write branch — updated_at unchanged
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="closed", success_count=0)
        cb_key = _cb_key(repo)
        snapshot_before = redis_test_client.hgetall(cb_key)

        thread_count = 30
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_success_with_close_check(SVC, success_threshold=2)
            with results_lock:
                results.append((outcome.did_close, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        assert all(not r[0] for r in results)
        assert all(r[1] == "closed" for r in results)
        # Lua state=='closed' branch returns without HSET — updated_at unchanged.
        snapshot_after = redis_test_client.hgetall(cb_key)
        assert snapshot_after.get("updated_at") == snapshot_before.get("updated_at")

    def test_concurrent_close_check_stale_open_returns_sentinel_no_writes(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            Stale-L2 branch (D6 step 2 routing input) — L2 is OPEN when
            workers attempt close-check (their L1 observed HALF_OPEN via the
            L1-fallback try_acquire path; L2 never received the transition).
            All threads must see the Lua else-branch state sentinel without
            any HSET, so the Layered wrapper can detect and fall back to L1.
        Expected:
            - All threads return did_close=False, state='open',
              success_count=0 (synthetic default per D2)
            - L2 hash entirely unchanged
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        cb_key = _cb_key(repo)
        snapshot_before = redis_test_client.hgetall(cb_key)

        thread_count = 20
        results: list[tuple[bool, str, int]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_success_with_close_check(SVC, success_threshold=2)
            with results_lock:
                results.append(
                    (
                        outcome.did_close,
                        outcome.state.state,
                        outcome.state.success_count,
                    )
                )

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        assert all(not r[0] for r in results)
        assert all(r[1] == "open" for r in results)
        assert all(r[2] == 0 for r in results)
        # L2 hash untouched — Lua else-branch returned without HSET.
        assert redis_test_client.hgetall(cb_key) == snapshot_before


# =============================================================================
# F. Layered L2-Authoritative Close-Check (498 D6)
# =============================================================================


class TestLayeredCloseCheckAtomicity:
    """Verifies 498 D6 routing preserves L2 atomicity through the Layered shell.

    Cat E proves the L2 Lua primitive itself is atomic. Cat F proves the
    Layered router that orchestrates L1+L2 (bounded executor, timeout,
    L1 writeback, stale-L2 guard, degraded-mode fallback) does NOT
    introduce extra did_close=True winners and DOES converge L1 to the
    L2 decision for every worker.
    """

    def test_layered_concurrent_close_check_one_winner_l1_converges(
        self,
        layered_cb_repo,
        redis_circuit_breaker_repository,
    ):
        """
        Purpose:
            50 concurrent threads through the Layered router, L2=HALF_OPEN,
            threshold=1. L2-authoritative routing must:
            (1) deliver exactly one did_close=True cluster-wide (the F11
                cross-process emit-count target), and
            (2) writeback state='closed' to L1 for BOTH the winner and every
                race-loser (so subsequent L1 reads observe closed without
                waiting for the ~5 s drift tick).
        Expected:
            - exactly 1 did_close=True
            - thread_count-1 did_close=False, all with state='closed'
            - Final L1: state='closed', success_count=0,
              half_open_request_count=0
        """
        repo = layered_cb_repo
        service = "checkout-svc"
        # Seed L2 to HALF_OPEN so the Lua close branch closes at threshold=1.
        redis_circuit_breaker_repository.update_state(
            service, state="half_open", success_count=0
        )
        # Pre-seed L1 to half_open so the writeback transition half_open→closed
        # is observable (and the test fails if writeback is skipped).
        repo._l1.get_or_create(service)
        repo._l1.update_state(service, state="half_open", success_count=0)

        thread_count = 50
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_success_with_close_check(service, success_threshold=1)
            with results_lock:
                results.append((outcome.did_close, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        race_losers = [r for r in results if not r[0]]

        assert len(winners) == 1, (
            f"498 D6 exactly-one contract violated: {len(winners)} winners "
            f"across {thread_count} workers through the Layered router"
        )
        assert winners[0][1] == "closed"
        assert len(race_losers) == thread_count - 1
        assert all(r[1] == "closed" for r in race_losers)

        # L1 writeback ran for every thread that received state='closed' from
        # L2 (winner + race-losers) — L1 must converge to CLOSED.
        l1_final = repo._l1.get_by_service_name(service)
        assert l1_final.state == "closed"
        assert l1_final.success_count == 0
        assert l1_final.half_open_request_count == 0
