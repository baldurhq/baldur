"""656 D7 — record_failure_with_open_check Lua + Layered routing (Redis).

Verifies the atomic HALF_OPEN -> OPEN re-open primitive against a real
Redis instance — the failure-side mirror of the #498 close-check Lua CAS.
Mock-based unit tests verify return-array parsing only; real Redis ``EVAL``
is the only authoritative substrate for the cross-process exactly-one
``CIRCUIT_BREAKER_OPENED`` contract.

Test Categories:
    A. Lua state-machine round-trip (single-thread, sequential):
        - half_open -> open (did_open=1, opened_at set, counters reset)
        - open -> no-write race-loser (did_open=0, opened_at carried)
        - closed -> trust-L2 sentinel (did_open=0, no HSET)
        - missing -> stale sentinel (did_open=0, no HSET)
    E. Cross-worker open-check atomicity (#498 / 656 D7):
        - 50 threads + Barrier from state=HALF_OPEN -> exactly 1 did_open=True
          winner; the remaining 49 race-lose on state=open
    F. Layered L2-authoritative open-check (656 D7):
        - 50 threads via the Layered router from L2=HALF_OPEN -> exactly 1
          did_open=True winner cluster-wide; L1 converges to OPEN for both
          the winner and every race-loser

All tests require a running Redis instance. Marked with
@pytest.mark.requires_redis for auto-skip.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)

pytestmark = pytest.mark.requires_redis


SVC = "payment-api"


def _cb_key(repo, service_name: str = SVC) -> str:
    """Compute the actual Redis hash key the repo writes to."""
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


class TestOpenCheckLuaRoundTrip:
    """Verifies the Lua state-machine branches against real Redis."""

    def test_half_open_re_opens_with_counters_reset(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        # success_count / half_open_request_count must be reset by the Lua.
        repo.update_state(
            SVC, state="half_open", success_count=42, half_open_request_count=3
        )

        attempt = repo.record_failure_with_open_check(SVC)

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        assert attempt.state.opened_at is not None

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "open"
        assert data["failure_count"] == "0"
        assert data["success_count"] == "0"
        assert data["half_open_request_count"] == "0"
        assert data["opened_at"]  # non-empty ISO timestamp

    def test_open_state_returns_race_loser_carrying_opened_at(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open")
        opened_before = redis_test_client.hget(_cb_key(repo), "opened_at")

        attempt = repo.record_failure_with_open_check(SVC)

        # No re-open; the existing opened_at is carried (no HSET overwrite).
        assert attempt.did_open is False
        assert attempt.state.state == "open"
        assert redis_test_client.hget(_cb_key(repo), "opened_at") == opened_before

    def test_closed_state_returns_sentinel_without_writing(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="closed")
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        attempt = repo.record_failure_with_open_check(SVC)

        # Trust-L2 sentinel: did_open=False, no HSET (a straggler failure
        # never overrides the cluster's recovery).
        assert attempt.did_open is False
        assert attempt.state.state == "closed"
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_missing_hash_returns_stale_sentinel(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        # Warm up the backend's lazy Redis connection on an unrelated key so
        # the target key itself stays absent -> HMGET reports no hash.
        repo.update_state("warmup-svc", state="closed")

        attempt = repo.record_failure_with_open_check("never-seen-svc")

        assert attempt.did_open is False
        assert attempt.state.state == "missing"
        assert not redis_test_client.exists(_cb_key(repo, "never-seen-svc"))


# =============================================================================
# E. Cross-Worker Open-Check Atomicity (#498 / 656 D7)
# =============================================================================


class TestOpenCheckCrossWorkerAtomicity:
    """Verifies the exactly-one CIRCUIT_BREAKER_OPENED contract via Lua."""

    def test_concurrent_open_check_exactly_one_did_open_winner(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """50 concurrent threads from state=HALF_OPEN. Lua atomicity must
        serialize HMGET-decide-HSET so exactly one thread sees did_open=True;
        the remaining 49 arrive after the re-open and race-lose on state=open.
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="half_open", success_count=0)

        thread_count = 50
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_failure_with_open_check(SVC)
            with results_lock:
                results.append((outcome.did_open, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        race_losers = [r for r in results if not r[0]]

        assert len(winners) == 1, (
            f"#498 exactly-one contract violated: {len(winners)} did_open=True "
            f"winners across {thread_count} workers (expected 1)"
        )
        assert winners[0][1] == "open"
        assert len(race_losers) == thread_count - 1
        assert all(r[1] == "open" for r in race_losers)

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "open"
        assert data["half_open_request_count"] == "0"


# =============================================================================
# F. Layered L2-Authoritative Open-Check (656 D7)
# =============================================================================


@pytest.fixture
def layered_cb_repo(redis_circuit_breaker_repository):
    """Layered (L1 in-memory + L2 real Redis) CB repo."""
    repo = LayeredCircuitBreakerStateRepository(
        l2_repo=redis_circuit_breaker_repository,
        adapter_type="redis",
    )
    repo._get_timeout_seconds = lambda: 5.0
    yield repo
    reset_layered_repository_executor()


class TestLayeredOpenCheckAtomicity:
    """Verifies 656 D7 routing preserves L2 atomicity through the Layered shell."""

    def test_layered_concurrent_open_check_one_winner_l1_converges(
        self,
        layered_cb_repo,
        redis_circuit_breaker_repository,
    ):
        """50 concurrent threads through the Layered router, L2=HALF_OPEN.
        L2-authoritative routing must (1) deliver exactly one did_open=True
        cluster-wide and (2) writeback state='open' to L1 for BOTH the winner
        and every race-loser (so subsequent L1 reads cut traffic immediately).
        """
        repo = layered_cb_repo
        service = "checkout-svc"
        redis_circuit_breaker_repository.update_state(
            service, state="half_open", success_count=0
        )
        # Pre-seed L1 to half_open so the writeback transition is observable.
        repo._l1.get_or_create(service)
        repo._l1.update_state(service, state="half_open", success_count=0)

        thread_count = 50
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.record_failure_with_open_check(service)
            with results_lock:
                results.append((outcome.did_open, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        race_losers = [r for r in results if not r[0]]

        assert len(winners) == 1, (
            f"656 D7 exactly-one contract violated: {len(winners)} winners "
            f"across {thread_count} workers through the Layered router"
        )
        assert winners[0][1] == "open"
        assert len(race_losers) == thread_count - 1
        assert all(r[1] == "open" for r in race_losers)

        # L1 writeback ran for every thread that received state='open' from L2
        # (winner + race-losers) — L1 must converge to OPEN.
        l1_final = repo._l1.get_by_service_name(service)
        assert l1_final.state == "open"
