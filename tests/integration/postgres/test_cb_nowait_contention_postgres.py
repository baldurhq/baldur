"""NOWAIT lock contention on the three atomic CB primitives (PostgreSQL).

Each of the SQL adapter's single-winner primitives takes its row lock with
``SELECT ... FOR UPDATE NOWAIT`` and re-raises the driver's contention error,
so the Layered wrapper records degraded mode and falls back to L1 rather than
reporting a transition it never performed:

- ``trip_to_open`` (CLOSED -> OPEN automatic trip)
- ``record_failure_with_open_check`` (HALF_OPEN -> OPEN re-open)
- ``record_success_with_close_check`` (HALF_OPEN -> CLOSED close)

Unit tests reach that contract by making a cursor raise, which proves the
wrapper's rollback-and-re-raise discipline but assumes the premise: that
PostgreSQL raises at all. The premise is invisible to sqlite — the dialect
split omits the clause entirely there, so the whole ``FOR UPDATE NOWAIT``
statement is never executed by any other test in the suite. This file is the
only place the emitted SQL is checked against a server that implements it.

Test categories:
    A. The clause is accepted and the primitives work uncontended, so a
       failure in B is contention rather than a syntax error.
    B. A second connection holding the row lock makes each primitive raise,
       and the row is left untouched by the loser's rolled-back transaction.
    C. Releasing the lock lets the same call succeed, so the failure is the
       lock and nothing else.

Requires Docker PostgreSQL on port 15432 (docker-compose.test.yml). Marked
with @pytest.mark.requires_db for auto-skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from baldur.adapters.sql.base import SchemaVersionManager
from baldur.adapters.sql.circuit_breaker import (
    _TABLE,
    SQLCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.settings.sql import SQLDialect, reset_sql_settings
from tests.integration.conftest import DatabaseTestConfig

pytestmark = pytest.mark.requires_db

SVC = "payment.charge"
FAILURE_COUNT = 5
SUCCESS_THRESHOLD = 2


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def pg_connect():
    """Callable opening a fresh psycopg2 connection to the test Postgres."""
    psycopg2 = pytest.importorskip("psycopg2")
    cfg = DatabaseTestConfig()

    def _connect():
        return psycopg2.connect(
            host=cfg.DEFAULT_HOST,
            port=cfg.DEFAULT_PORT,
            database=cfg.DEFAULT_DB,
            user=cfg.DEFAULT_USER,
            password=cfg.DEFAULT_PASSWORD,
            connect_timeout=5,
        )

    return _connect


@pytest.fixture(autouse=True)
def _reset_sql_singletons() -> Iterator[None]:
    """Keep the settings singleton and the schema-applied cache pristine."""
    reset_sql_settings()
    yield
    reset_sql_settings()
    SchemaVersionManager._reset_applied_cache()


@pytest.fixture
def repo(pg_connect) -> Iterator[SQLCircuitBreakerStateRepository]:
    """Repository under test, on its own dedicated connection.

    The dialect is passed explicitly rather than inferred from a DSN: this
    file exists precisely to exercise the non-sqlite branch of the dialect
    split, so leaving it to settings inference would let an environment
    difference silently turn the test into a no-op.

    ``autocommit_delegated=False`` keeps Baldur owning commit/rollback, which
    is what the contention contract is about.
    """
    conn = pg_connect()
    repository = SQLCircuitBreakerStateRepository(
        lambda: conn,
        dialect=SQLDialect.POSTGRESQL,
        autocommit_delegated=False,
    )
    # Force the lazy schema bootstrap and seed the row every test starts from.
    repository.get_or_create(SVC)
    _reset_row(repository)
    try:
        yield repository
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {_TABLE} WHERE service_name = %s", (SVC,))
            conn.commit()
        except Exception:  # noqa: BLE001 — teardown must not mask a failure
            conn.rollback()
        conn.close()


@pytest.fixture
def lock_holder(pg_connect) -> Iterator[Any]:
    """A second connection that can pin the CB row with a plain FOR UPDATE.

    Plain ``FOR UPDATE`` (no NOWAIT) is what a *legitimate* concurrent worker
    holds; the primitives under test are the ones that refuse to wait for it.
    """
    conn = pg_connect()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        conn.close()


def _reset_row(
    repo: SQLCircuitBreakerStateRepository, state: str | None = None
) -> None:
    repo.update_state(
        service_name=SVC,
        state=state or CircuitBreakerStateEnum.CLOSED.value,
        failure_count=0,
        success_count=0,
        clear_opened_at=True,
    )


def _hold_row_lock(conn: Any, service_name: str = SVC) -> None:
    """Take and keep a row lock, leaving the transaction open."""
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT service_name FROM {_TABLE} WHERE service_name = %s FOR UPDATE",
        (service_name,),
    )
    assert cursor.fetchone() is not None, "lock holder found no row to lock"


def _lock_not_available():
    """The psycopg2 error class a NOWAIT loser receives."""
    psycopg2 = pytest.importorskip("psycopg2")
    return psycopg2.errors.LockNotAvailable


def _row_snapshot(repo: SQLCircuitBreakerStateRepository) -> tuple:
    row = repo.get_by_service_name(SVC)
    return (row.state, row.failure_count, row.success_count, row.opened_at)


# =============================================================================
# A. The emitted clause is accepted by a server that implements it
# =============================================================================


class TestNowaitPrimitivesUncontendedOnPostgres:
    """Each primitive completes against real PostgreSQL with no contention.

    Without this half, a red test in category B would be indistinguishable
    from ``FOR UPDATE NOWAIT`` being malformed on this dialect.
    """

    def test_trip_to_open_writes_the_open_row(self, repo):
        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        stored = repo.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.OPEN.value
        assert stored.failure_count == FAILURE_COUNT
        assert stored.opened_at is not None

    def test_open_check_re_opens_from_half_open(self, repo):
        _reset_row(repo, CircuitBreakerStateEnum.HALF_OPEN.value)

        attempt = repo.record_failure_with_open_check(SVC)

        assert attempt.did_open is True
        assert repo.get_by_service_name(SVC).state == (
            CircuitBreakerStateEnum.OPEN.value
        )

    def test_close_check_closes_at_the_threshold(self, repo):
        _reset_row(repo, CircuitBreakerStateEnum.HALF_OPEN.value)

        first = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)
        second = repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert first.did_close is False
        assert second.did_close is True
        assert repo.get_by_service_name(SVC).state == (
            CircuitBreakerStateEnum.CLOSED.value
        )


# =============================================================================
# B. A held row lock makes each primitive raise, and roll back cleanly
# =============================================================================


class TestNowaitContentionRaises:
    """The loser reports failure instead of a transition it never performed.

    Swallowing the error is the dangerous shape: the caller would receive a
    ``did_open`` / ``did_close`` verdict decided against a row it never
    locked, and the Layered wrapper would skip the degraded-mode fallback
    that keeps protection local when the store cannot answer.
    """

    def test_trip_to_open_raises_under_contention(self, repo, lock_holder):
        _hold_row_lock(lock_holder)
        before = _row_snapshot(repo)

        with pytest.raises(_lock_not_available()):
            repo.trip_to_open(SVC, FAILURE_COUNT)

        # The loser's transaction rolled back: no partial write survives.
        assert _row_snapshot(repo) == before

    def test_open_check_raises_under_contention(self, repo, lock_holder):
        _reset_row(repo, CircuitBreakerStateEnum.HALF_OPEN.value)
        _hold_row_lock(lock_holder)
        before = _row_snapshot(repo)

        with pytest.raises(_lock_not_available()):
            repo.record_failure_with_open_check(SVC)

        assert _row_snapshot(repo) == before

    def test_close_check_raises_under_contention(self, repo, lock_holder):
        _reset_row(repo, CircuitBreakerStateEnum.HALF_OPEN.value)
        _hold_row_lock(lock_holder)
        before = _row_snapshot(repo)

        with pytest.raises(_lock_not_available()):
            repo.record_success_with_close_check(SVC, SUCCESS_THRESHOLD)

        assert _row_snapshot(repo) == before

    def test_contention_does_not_block_the_caller(self, repo, lock_holder):
        """NOWAIT means the loser fails immediately rather than waiting.

        A plain ``FOR UPDATE`` here would park the request thread until the
        holder committed — on a trip that fires during a failure burst, that
        is the whole reason the clause is there.
        """
        import time

        _hold_row_lock(lock_holder)

        start = time.monotonic()
        with pytest.raises(_lock_not_available()):
            repo.trip_to_open(SVC, FAILURE_COUNT)
        elapsed = time.monotonic() - start

        # Generous bound: the point is "returns rather than parks", not a
        # latency budget. A waiting lock would hold until the fixture's
        # teardown rollback, i.e. past the end of the test.
        assert elapsed < 5.0


# =============================================================================
# C. Releasing the lock restores the primitive
# =============================================================================


class TestNowaitContentionReleases:
    """The failure is the lock and nothing else."""

    def test_trip_to_open_succeeds_once_the_lock_is_released(self, repo, lock_holder):
        _hold_row_lock(lock_holder)
        with pytest.raises(_lock_not_available()):
            repo.trip_to_open(SVC, FAILURE_COUNT)

        lock_holder.rollback()

        attempt = repo.trip_to_open(SVC, FAILURE_COUNT)
        assert attempt.did_open is True
        assert repo.get_by_service_name(SVC).state == (
            CircuitBreakerStateEnum.OPEN.value
        )

    def test_repository_connection_is_reusable_after_a_contention_failure(
        self, repo, lock_holder
    ):
        """The rolled-back transaction must not strand the connection.

        psycopg2 leaves a connection in an aborted state until someone rolls
        back; a primitive that raised without rolling back would make every
        later statement on that connection fail with "current transaction is
        aborted", turning one contention event into a dead worker.
        """
        _hold_row_lock(lock_holder)
        with pytest.raises(_lock_not_available()):
            repo.trip_to_open(SVC, FAILURE_COUNT)

        lock_holder.rollback()

        # An ordinary read and an ordinary write both still work.
        assert repo.get_by_service_name(SVC) is not None
        repo.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=1,
        )
        assert repo.get_by_service_name(SVC).failure_count == 1
