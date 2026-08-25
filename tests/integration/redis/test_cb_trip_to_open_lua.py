"""``trip_to_open`` Lua + pin-guarded mirror write + Layered routing (Redis).

The atomic CLOSED->OPEN trip against a real Redis instance. Unit tests pin the
Python wrapper's return-array parsing; only real ``EVAL`` is an authoritative
substrate for the branch matrix and for the single-winner contract the
service's event, audit, metric and shared-error-budget side effects are gated
on.

Two Lua details are only decidable here, and both are wire-format traps:
Redis stores booleans as the literal strings ``"True"`` / ``"False"``, and
``'False'`` is truthy in Lua — so a bare truthiness check on the pin flag
would decline every trip on any row that ever carried an override. And the
expiry comparison is a lexicographic string compare, which holds only while
every writer stamps ``utc_now().isoformat()`` verbatim.

Test categories:
    A. Lua state-machine round-trip: closed / missing / open / half_open,
       plus the active-pin decline and the lapsed-pin clear.
    B. ``update_state`` directives against real Redis: ``clear_opened_at`` and
       the ``skip_if_pinned`` conditional write.
    C. Cross-worker trip atomicity: 50 threads from CLOSED -> exactly one
       ``did_open=True`` winner.
    D. Layered L2-authoritative routing, including the regression the whole
       change exists for — a trip surviving its own record-path mirror.

All tests require a running Redis instance (``requires_redis`` auto-skip).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)
from baldur.interfaces.repositories import CIRCUIT_BREAKER_PINNED_TOKEN
from baldur.utils.time import utc_now

pytestmark = pytest.mark.requires_redis


SVC = "payment-api"
FAILURE_COUNT = 5


def _cb_key(repo, service_name: str = SVC) -> str:
    """The physical Redis hash key the repo writes to."""
    return repo._backend._get_full_key(f"cb:{service_name}")


@pytest.fixture(autouse=True)
def _reset_redis_unavailable_flag():
    """Reset the runtime-scoped Redis negative cache so the backend can init."""
    from baldur.adapters.redis import _redis_state

    state = _redis_state()
    state.unavailable = False
    state.fail_time = 0.0
    yield
    state.unavailable = False
    state.fail_time = 0.0


# =============================================================================
# A. Lua state-machine round-trip
# =============================================================================


class TestTripToOpenLuaRoundTrip:
    """Every branch of the trip script, against real Redis."""

    def test_closed_row_is_written_open_with_counters_reset(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(
            SVC, state="closed", success_count=42, half_open_request_count=3
        )

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        assert attempt.state.opened_at is not None

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "open"
        # The caller's count verbatim, so the durable and local rows describe
        # the same trip.
        assert data["failure_count"] == str(FAILURE_COUNT)
        assert data["success_count"] == "0"
        assert data["half_open_request_count"] == "0"
        assert data["half_open_window_started_at"] == ""
        assert data["opened_at"]

    def test_missing_hash_is_treated_as_closed_and_tripped(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        # Warm the lazy connection on an unrelated key so the target stays
        # absent — HMGET then reports no hash.
        repo.update_state("warmup-svc", state="closed")

        attempt = repo.trip_to_open("never-seen-svc", FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        data = redis_test_client.hgetall(_cb_key(repo, "never-seen-svc"))
        assert data["state"] == "open"

    def test_open_row_is_a_race_loser_that_writes_nothing(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open", opened_at=utc_now(), failure_count=9)
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == "open"
        assert attempt.state.opened_at is not None
        # No HSET at all: the winner's row is untouched, timestamp included.
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_half_open_row_is_not_clobbered_back_to_open(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="half_open", half_open_request_count=2)
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # Recency over restrictiveness: the cluster progressed to recovery
        # testing, and reverting it would undo a legitimate transition.
        assert attempt.did_open is False
        assert attempt.state.state == "half_open"
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_active_pin_declines_the_trip_and_leaves_the_row_untouched(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        # The wire-format trap: 'True'/'False' are stored as literal strings
        # and 'False' is truthy in Lua, so the guard must compare strings.
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            "closed",
            controlled_by_id=7,
            reason="deploy window",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at is not None
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_open_ended_pin_declines_the_trip(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(SVC, "closed", reason="indefinite allow")
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_cleared_flag_does_not_decline_the_trip(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        # The 'False' truthiness trap made concrete: a row that once carried
        # an override now stores the literal string "False", which a bare Lua
        # truthiness test would read as pinned and decline forever.
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(SVC, "closed", reason="lifted")
        repo.clear_manual_control(SVC)
        assert redis_test_client.hget(_cb_key(repo), "manually_controlled") == "False"

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert redis_test_client.hget(_cb_key(repo), "state") == "open"

    def test_lapsed_pin_trips_and_clears_the_stale_flag(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            "closed",
            controlled_by_id=7,
            reason="expired window",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        # The trip lands, and the row re-enters the recovery lane's view —
        # that filter still reads the raw flag.
        assert attempt.did_open is True
        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "open"
        assert data["manually_controlled"] == "False"
        assert data["manual_override_expires_at"] == ""
        assert data["controlled_by_id"] == ""


# =============================================================================
# B. update_state directives against real Redis
# =============================================================================


class TestUpdateStateDirectivesAgainstRedis:
    """``clear_opened_at`` and the ``skip_if_pinned`` conditional write."""

    def test_clear_opened_at_scrubs_the_open_era_timestamp(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="open", opened_at=utc_now())
        assert redis_test_client.hget(_cb_key(repo), "opened_at")

        repo.update_state(SVC, state="closed", clear_opened_at=True)

        # Without the directive the row reads closed while still naming the
        # instant it opened, and no reader can tell which half is current.
        assert redis_test_client.hget(_cb_key(repo), "opened_at") == ""
        assert repo.get_by_service_name(SVC).opened_at is None

    def test_skip_if_pinned_declines_a_write_on_an_actively_pinned_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        # The store-side half of pin neutrality: a worker that never hydrated
        # this override has nothing local to skip on.
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            "open",
            reason="operator block",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))

        result = repo.update_state(
            SVC, state="closed", failure_count=0, skip_if_pinned=True
        )

        # Declined by contract is a success for the caller.
        assert result is True
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_skip_if_pinned_writes_through_a_lapsed_override(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            "open",
            reason="expired block",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        repo.update_state(SVC, state="closed", failure_count=0, skip_if_pinned=True)

        assert redis_test_client.hget(_cb_key(repo), "state") == "closed"

    def test_skip_if_pinned_writes_through_an_unpinned_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="closed")

        repo.update_state(SVC, state="open", opened_at=utc_now(), skip_if_pinned=True)

        assert redis_test_client.hget(_cb_key(repo), "state") == "open"


# =============================================================================
# C. Cross-worker trip atomicity
# =============================================================================


class TestTripCrossWorkerAtomicity:
    """One winner per CLOSED->OPEN transition, cluster-wide."""

    def test_concurrent_trips_produce_exactly_one_winner(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """50 threads from state=CLOSED. Lua atomicity must serialize
        HMGET-decide-HSET so exactly one sees ``did_open=True``; the rest
        arrive after the write and race-lose on state=open.
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state="closed")

        thread_count = 50
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.trip_to_open(SVC, FAILURE_COUNT)
            with results_lock:
                results.append((outcome.did_open, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        race_losers = [r for r in results if not r[0]]

        assert len(winners) == 1, (
            f"single-fire contract violated: {len(winners)} did_open=True "
            f"winners across {thread_count} workers (expected 1)"
        )
        assert winners[0][1] == "open"
        assert len(race_losers) == thread_count - 1
        assert all(r[1] == "open" for r in race_losers)

        data = redis_test_client.hgetall(_cb_key(repo))
        assert data["state"] == "open"
        assert data["failure_count"] == str(FAILURE_COUNT)


# =============================================================================
# D. Layered L2-authoritative routing
# =============================================================================


@pytest.fixture
def layered_cb_repo(redis_circuit_breaker_repository):
    """Layered repo: L1 in-memory + L2 real Redis."""
    repo = LayeredCircuitBreakerStateRepository(
        l2_repo=redis_circuit_breaker_repository,
        adapter_type="redis",
    )
    repo._get_timeout_seconds = lambda: 5.0
    yield repo
    reset_layered_repository_executor()


class TestLayeredTripRoutingAgainstRedis:
    """The router's contract with a real store underneath it."""

    def test_layered_concurrent_trips_one_winner_and_l1_converges(
        self, layered_cb_repo, redis_circuit_breaker_repository
    ):
        repo = layered_cb_repo
        service = "checkout-svc"
        redis_circuit_breaker_repository.update_state(service, state="closed")
        repo._l1.get_or_create(service)

        thread_count = 50
        results: list[tuple[bool, str]] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(thread_count)

        def attempt():
            barrier.wait()
            outcome = repo.trip_to_open(service, FAILURE_COUNT)
            with results_lock:
                results.append((outcome.did_open, outcome.state.state))

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(attempt) for _ in range(thread_count)]
            for future in as_completed(futures):
                future.result()

        winners = [r for r in results if r[0]]
        assert len(winners) == 1, (
            f"single-fire contract violated through the Layered router: "
            f"{len(winners)} winners across {thread_count} workers"
        )
        # Winner and race-losers alike write back OPEN, so no worker readmits.
        assert repo._l1.get_by_service_name(service).state == "open"

    def test_trip_is_visible_to_an_immediate_store_read(
        self, layered_cb_repo, redis_circuit_breaker_repository
    ):
        # No settle poll: on the atomic path the store write is done when the
        # call returns.
        repo = layered_cb_repo
        service = "immediate-read-svc"
        redis_circuit_breaker_repository.update_state(service, state="closed")
        repo._l1.get_or_create(service)

        attempt = repo.trip_to_open(service, FAILURE_COUNT)

        assert attempt.did_open is True
        stored = redis_circuit_breaker_repository.get_by_service_name(service)
        assert stored.state == "open"
        assert stored.opened_at is not None

    def test_trip_survives_its_own_record_path_mirrors(
        self, layered_cb_repo, redis_circuit_breaker_repository
    ):
        """The measured defect: five failure records plus the trip used to be
        six concurrent snapshots, and whichever finished last decided the
        durable row — so a real trip could settle as CLOSED with the trip's
        own ``opened_at`` still stamped on it.
        """
        repo = layered_cb_repo
        service = "burst-svc"
        redis_circuit_breaker_repository.update_state(service, state="closed")
        repo._l1.get_or_create(service)

        # The burst that produces the trip: each record submits a mirror.
        for _ in range(FAILURE_COUNT):
            repo.record_failure(service)

        attempt = repo.trip_to_open(service, FAILURE_COUNT)
        assert attempt.did_open is True

        # Drain the mirror lane: every task the burst and the trip's nudge
        # queued must have run before the store is judged.
        reset_layered_repository_executor()

        stored = redis_circuit_breaker_repository.get_by_service_name(service)
        assert stored.state == "open"
        assert stored.opened_at is not None

    def test_actively_pinned_store_row_declines_the_trip_and_reaches_l1(
        self, layered_cb_repo, redis_circuit_breaker_repository
    ):
        # The operator pinned the row on another worker. This one must adopt
        # the decision rather than write OPEN over it.
        repo = layered_cb_repo
        service = "pinned-svc"
        redis_circuit_breaker_repository.set_manual_control(
            service,
            "closed",
            controlled_by_id=7,
            reason="peer allow",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        repo._l1.get_or_create(service)

        attempt = repo.trip_to_open(service, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        l1_state = repo._l1.get_by_service_name(service)
        assert l1_state.manually_controlled is True
        assert l1_state.state != "open"
        # And nothing was written to the store row.
        assert redis_circuit_breaker_repository.get_by_service_name(service).state == (
            "closed"
        )
