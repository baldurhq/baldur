"""The keep-open guard against a real Redis: the script's decline and the
degraded-backend WAL decline.

793 D3. Unit tests mock the client's ``eval``, so the conditional script's
state check is only decidable here: a guarded CLOSED write against a stored
``open`` / ``half_open`` row must leave the hash untouched — ``opened_at``
included — and report success, while the same write against a CLOSED row and
an operator's unguarded close both land. The other arm only a real resilient
backend can show: a keep-open CLOSED write while the backend answers from its
local fallback is declined outright and writes no WAL record, so a WAL replay
can never put CLOSED over a peer's trip; a guarded OPEN write on the same
backend still reaches the WAL, and the layered repository's record-path
mirror over that backend writes nothing either.

Test categories:
    A. The Lua state check against real Redis.
    B. The degraded backend: no WAL record for the guarded CLOSED write.
    C. Layered routing: a second worker's CLOSED mirror leaves a peer's OPEN
       row in place for boot hydration to read.

All tests require a running Redis instance (``requires_redis`` auto-skip).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
    reset_layered_repository_executor,
)
from baldur.adapters.redis.circuit_breaker import RedisCircuitBreakerStateRepository
from baldur.adapters.resilient.backend import ResilientStorageBackend
from baldur.interfaces.repositories import (
    CircuitBreakerStateEnum,
)
from baldur.settings.resilient_storage import ResilientStorageSettings
from baldur.utils.time import utc_now

pytestmark = pytest.mark.requires_redis

SVC = "payment-api"
CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value


def _cb_key(repo, service_name: str = SVC) -> str:
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


@pytest.fixture
def wal_backed_repository(redis_url, redis_client, tmp_path):
    """A Redis repository whose backend keeps a WAL in a per-test directory."""
    settings = ResilientStorageSettings(
        redis_url=redis_url,
        key_prefix="test:baldur:",
        use_dynamic_prefix=False,
        allow_memory_only=True,
        wal_dir=str(tmp_path / "wal"),
    )
    backend = ResilientStorageBackend(settings=settings)
    assert backend.ensure_redis() is True
    yield RedisCircuitBreakerStateRepository(backend=backend)
    for key in redis_client.keys("test:baldur:*"):
        redis_client.delete(key)


def _wal_entries(repo) -> int:
    wal = repo._backend._wal
    return wal.get_stats().total_entries if wal is not None else 0


# =============================================================================
# A. The Lua state check
# =============================================================================


class TestKeepOpenLuaStateCheck:
    """The script declines a CLOSED write against a stored non-CLOSED row."""

    @pytest.mark.parametrize(
        "stored_state", [OPEN, HALF_OPEN], ids=["open", "half_open"]
    )
    def test_guarded_closed_write_leaves_a_stored_trip_untouched(
        self, redis_circuit_breaker_repository, redis_test_client, stored_state
    ):
        """
        Purpose:
            The store-side half of the trip-precedence rule, decided in Lua.
        Expected:
            - ``update_state`` returns True (declined by contract)
            - the hash is byte-for-byte what it was, ``opened_at`` included
        """
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state=stored_state, failure_count=5, opened_at=utc_now())
        snapshot_before = redis_test_client.hgetall(_cb_key(repo))
        assert snapshot_before["state"] == stored_state

        result = repo.update_state(SVC, state=CLOSED, failure_count=0, keep_open=True)

        assert result is True
        assert redis_test_client.hgetall(_cb_key(repo)) == snapshot_before

    def test_guarded_closed_write_refreshes_a_stored_closed_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state=CLOSED, failure_count=3)

        repo.update_state(SVC, state=CLOSED, failure_count=0, keep_open=True)

        assert redis_test_client.hget(_cb_key(repo), "failure_count") == "0"

    def test_unguarded_closed_write_still_moves_a_stored_open_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """Control: an operator's close passes no guard and lands."""
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state=OPEN, failure_count=5, opened_at=utc_now())

        repo.update_state(SVC, state=CLOSED, failure_count=0, clear_opened_at=True)

        assert redis_test_client.hget(_cb_key(repo), "state") == CLOSED

    def test_guarded_open_write_lands_on_a_stored_closed_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.update_state(SVC, state=CLOSED)

        repo.update_state(SVC, state=OPEN, opened_at=utc_now(), keep_open=True)

        assert redis_test_client.hget(_cb_key(repo), "state") == OPEN

    def test_keep_open_alone_does_not_apply_the_pin_guard(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        """``ARGV[2]`` and ``ARGV[3]`` are independent: a pinned CLOSED row is written."""
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            CLOSED,
            reason="operator allow",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        repo.update_state(SVC, state=CLOSED, failure_count=4)

        repo.update_state(SVC, state=CLOSED, failure_count=0, keep_open=True)

        assert redis_test_client.hget(_cb_key(repo), "failure_count") == "0"

    def test_both_guards_decline_a_pinned_closed_row(
        self, redis_circuit_breaker_repository, redis_test_client
    ):
        repo = redis_circuit_breaker_repository
        repo.set_manual_control(
            SVC,
            CLOSED,
            reason="operator allow",
            expires_at=utc_now() + timedelta(minutes=10),
        )
        repo.update_state(SVC, state=CLOSED, failure_count=4)

        repo.update_state(
            SVC, state=CLOSED, failure_count=0, skip_if_pinned=True, keep_open=True
        )

        assert redis_test_client.hget(_cb_key(repo), "failure_count") == "4"


# =============================================================================
# B. The degraded backend
# =============================================================================


class TestKeepOpenDegradedBackendWal:
    """A guarded CLOSED write on a degraded backend never becomes a WAL record."""

    def test_guarded_closed_write_on_a_degraded_backend_writes_no_wal_record(
        self, wal_backed_repository
    ):
        """
        Purpose:
            The decline before and after the guard read, with the real WAL.
        Expected:
            - the write reports success (declined by contract)
            - the WAL entry count is unchanged
            - the backend's local memory holds no row for the name
        """
        repo = wal_backed_repository
        backend = repo._backend
        backend._switch_to_degraded()
        assert backend.is_degraded is True
        entries_before = _wal_entries(repo)

        with patch.object(
            backend.raw_redis_client, "eval", side_effect=ConnectionError("redis down")
        ):
            result = repo.update_state(
                SVC, state=CLOSED, failure_count=0, keep_open=True
            )

        assert result is True
        assert _wal_entries(repo) == entries_before
        assert backend._memory.get(f"cb:{SVC}") is None

    def test_guarded_open_write_on_a_degraded_backend_takes_the_wal(
        self, wal_backed_repository
    ):
        """Control: an OPEN write can only make the store more restrictive."""
        repo = wal_backed_repository
        backend = repo._backend
        backend._switch_to_degraded()
        entries_before = _wal_entries(repo)

        with patch.object(
            backend.raw_redis_client, "eval", side_effect=ConnectionError("redis down")
        ):
            result = repo.update_state(
                SVC, state=OPEN, opened_at=utc_now(), keep_open=True
            )

        assert result is True
        assert _wal_entries(repo) == entries_before + 1
        assert backend._memory[f"cb:{SVC}"]["state"] == OPEN

    def test_unguarded_closed_write_on_a_degraded_backend_takes_the_wal(
        self, wal_backed_repository
    ):
        """Control: only the guarded CLOSED write is declined."""
        repo = wal_backed_repository
        backend = repo._backend
        backend._switch_to_degraded()
        entries_before = _wal_entries(repo)

        result = repo.update_state(SVC, state=CLOSED, failure_count=0)

        assert result is True
        assert _wal_entries(repo) == entries_before + 1


# =============================================================================
# C. Layered routing
# =============================================================================


@pytest.fixture
def layered_cb_repo(redis_circuit_breaker_repository):
    """Layered repo: L1 in-memory + L2 real Redis, a second worker's view."""
    repo = LayeredCircuitBreakerStateRepository(
        l2_repo=redis_circuit_breaker_repository,
        adapter_type="redis",
    )
    repo._get_timeout_seconds = lambda: 5.0
    yield repo
    reset_layered_repository_executor()


class TestKeepOpenLayeredMirror:
    """A CLOSED mirror from a worker whose L1 never saw the trip."""

    def test_second_workers_closed_mirror_leaves_a_peers_open_row_in_place(
        self, layered_cb_repo, redis_circuit_breaker_repository, redis_test_client
    ):
        """
        Purpose:
            Boot hydration reads the store: a peer's trip must survive this
            worker's snapshot mirror of its own CLOSED row.
        Expected:
            - the store row stays OPEN with its ``opened_at``
            - this worker's L1 row is still CLOSED (the mirror was declined,
              not reversed)
        """
        store = redis_circuit_breaker_repository
        worker = layered_cb_repo
        worker._l1.get_or_create(SVC)
        # The peer trips the name in the store after this worker read it CLOSED.
        store.trip_to_open(SVC, 5)
        opened_at = redis_test_client.hget(_cb_key(store), "opened_at")
        assert opened_at

        # This worker's consecutive-count reset mirrors its CLOSED snapshot.
        worker.update_state(SVC, state=CLOSED, failure_count=0, keep_open=True)
        reset_layered_repository_executor()

        assert redis_test_client.hget(_cb_key(store), "state") == OPEN
        assert redis_test_client.hget(_cb_key(store), "opened_at") == opened_at
        assert worker._l1.get_by_service_name(SVC).state == CLOSED

    def test_hydration_then_reads_the_peers_trip(
        self, layered_cb_repo, redis_circuit_breaker_repository
    ):
        """The row the next boot hydrates is the trip, not the mirror."""
        store = redis_circuit_breaker_repository
        layered_cb_repo._l1.get_or_create(SVC)
        store.trip_to_open(SVC, 5)
        layered_cb_repo.update_state(SVC, state=CLOSED, failure_count=0, keep_open=True)
        reset_layered_repository_executor()

        fresh_worker = LayeredCircuitBreakerStateRepository(
            l2_repo=store, adapter_type="redis"
        )
        try:
            hydrated = fresh_worker._l1.get_by_service_name(SVC)
        finally:
            reset_layered_repository_executor()

        assert hydrated is not None
        assert hydrated.state == OPEN
        assert hydrated.failure_count == 5
