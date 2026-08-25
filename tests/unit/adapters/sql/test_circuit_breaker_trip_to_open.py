"""SQL ``trip_to_open`` and the two new ``update_state`` directives.

Runs against a real in-memory sqlite database, so the branch matrix is
verified through actual SQL rather than through a mock's idea of it — the
``UPDATE ... WHERE NOT (<active pin>)`` guard in particular is only meaningful
if a real statement matches (or fails to match) a real row.

What sqlite cannot supply is the ``FOR UPDATE NOWAIT`` half: the dialect split
in the production code omits the clause for sqlite, and lock contention has no
sqlite analogue. Contention is covered here at the transaction-discipline
level (the exception is re-raised after a rollback so the layered wrapper can
record degraded mode and fall back); the locking behavior itself is verified
against a real server in
``tests/integration/postgres/test_cb_nowait_contention_postgres.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.sql.circuit_breaker import (
    _TABLE,
    SQLCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now

SVC = "payment.charge"
FAILURE_COUNT = 5


@pytest.fixture
def cb(get_sqlite_conn) -> SQLCircuitBreakerStateRepository:
    return SQLCircuitBreakerStateRepository(get_sqlite_conn)


def _pin(cb, *, expires_in_seconds: int | None, state: str | None = None) -> None:
    """Place a manual override, optionally already lapsed.

    ``set_manual_control`` is the operator's own entry point, so the row it
    leaves is the shape the trip and the mirror actually meet in production.
    """
    cb.set_manual_control(
        SVC,
        state or CircuitBreakerStateEnum.CLOSED.value,
        controlled_by_id=7,
        reason="operator window",
        expires_at=(
            None
            if expires_in_seconds is None
            else utc_now() + timedelta(seconds=expires_in_seconds)
        ),
    )


# =============================================================================
# Behavior — trip branch matrix under the locked read-decide-write
# =============================================================================


class TestSqlTripToOpenBehavior:
    """Branch contract (see the ABC's ``trip_to_open``) against real SQL."""

    def test_closed_row_transitions_to_open_with_the_callers_failure_count(self, cb):
        cb.get_or_create(SVC)
        cb.record_success(SVC)

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.failure_count == FAILURE_COUNT
        stored = cb.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.OPEN.value
        assert stored.failure_count == FAILURE_COUNT
        assert stored.success_count == 0
        assert stored.opened_at is not None

    def test_trip_clears_the_half_open_counter_and_watermark(self, cb):
        cb.get_or_create(SVC)
        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            half_open_request_count=3,
        )

        cb.trip_to_open(SVC, FAILURE_COUNT)

        stored = cb.get_by_service_name(SVC)
        assert stored.half_open_request_count == 0
        assert stored.half_open_window_started_at is None

    def test_missing_row_is_created_and_tripped(self, cb):
        # The primitive opens with get_or_create, so absent resolves to the
        # CLOSED default rather than to a no-write sentinel.
        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        assert cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.OPEN.value

    def test_open_row_is_a_race_loser_that_keeps_its_opened_at(self, cb):
        cb.get_or_create(SVC)
        opened = utc_now() - timedelta(seconds=30)
        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=opened,
        )

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.opened_at is not None
        # No restamp: the stored timestamp is the one the winner wrote.
        assert cb.get_by_service_name(SVC).opened_at is not None
        assert abs((cb.get_by_service_name(SVC).opened_at - opened).total_seconds()) < 1

    def test_half_open_row_is_not_clobbered_back_to_open(self, cb):
        cb.get_or_create(SVC)
        cb.update_state(service_name=SVC, state=CircuitBreakerStateEnum.HALF_OPEN.value)

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert (
            cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.HALF_OPEN.value
        )

    def test_unrecognized_stored_state_writes_nothing(self, cb):
        cb.get_or_create(SVC)
        cb._execute(
            f"UPDATE {_TABLE} SET state = %s WHERE service_name = %s",
            ("corrupted", SVC),
        )

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == "corrupted"
        assert cb.get_by_service_name(SVC).state == "corrupted"

    def test_active_pin_declines_the_write_and_leaves_the_row_untouched(self, cb):
        _pin(cb, expires_in_seconds=600)
        before = cb.get_by_service_name(SVC)

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at is not None
        after = cb.get_by_service_name(SVC)
        assert after.state == before.state
        assert after.manually_controlled is True

    def test_open_ended_pin_declines_the_write(self, cb):
        _pin(cb, expires_in_seconds=None)

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert cb.get_by_service_name(SVC).manually_controlled is True

    def test_lapsed_pin_trips_and_clears_the_stale_flag(self, cb):
        _pin(cb, expires_in_seconds=-1)

        attempt = cb.trip_to_open(SVC, FAILURE_COUNT)

        assert attempt.did_open is True
        stored = cb.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.OPEN.value
        assert stored.manually_controlled is False
        assert stored.manual_override_expires_at is None
        assert stored.controlled_by_id is None
        # The reason survives, as the expiry sweep leaves it.
        assert stored.control_reason == "operator window"

    def test_lock_contention_rolls_back_and_re_raises(self, cb, monkeypatch):
        # On PostgreSQL a NOWAIT loser gets a driver exception. It must reach
        # the layered wrapper — that is what makes the loser record degraded
        # mode and fall back to L1 rather than silently reporting a trip it
        # did not perform.
        cb.get_or_create(SVC)
        fake_conn = MagicMock(spec=sqlite3.Connection)
        fake_conn.cursor.return_value.execute.side_effect = RuntimeError(
            "could not obtain lock on row"
        )
        monkeypatch.setattr(cb, "_borrow_connection", lambda: fake_conn)
        monkeypatch.setattr(cb, "_should_commit", lambda conn: True)

        with pytest.raises(RuntimeError, match="could not obtain lock"):
            cb.trip_to_open(SVC, FAILURE_COUNT)

        fake_conn.rollback.assert_called_once()
        fake_conn.commit.assert_not_called()


# =============================================================================
# Behavior — update_state(clear_opened_at=...)
# =============================================================================


class TestSqlUpdateStateClearOpenedAtBehavior:
    """The CLOSED row must not go on reporting when it opened."""

    def test_directive_nulls_the_open_era_timestamp(self, cb):
        cb.get_or_create(SVC)
        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            clear_opened_at=True,
        )

        stored = cb.get_by_service_name(SVC)
        assert stored.state == CircuitBreakerStateEnum.CLOSED.value
        assert stored.opened_at is None

    def test_without_the_directive_the_timestamp_is_kept(self, cb):
        cb.get_or_create(SVC)
        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
        )

        cb.update_state(service_name=SVC, state=CircuitBreakerStateEnum.CLOSED.value)

        assert cb.get_by_service_name(SVC).opened_at is not None

    def test_directive_wins_over_a_supplied_timestamp(self, cb):
        cb.get_or_create(SVC)

        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            opened_at=utc_now(),
            clear_opened_at=True,
        )

        assert cb.get_by_service_name(SVC).opened_at is None


# =============================================================================
# Behavior — update_state(skip_if_pinned=...)
# =============================================================================


class TestSqlUpdateStateSkipIfPinnedBehavior:
    """The pin test rides in the statement, not in a preceding read."""

    def test_active_pin_declines_the_write_and_reports_success(self, cb):
        _pin(cb, expires_in_seconds=600, state=CircuitBreakerStateEnum.OPEN.value)
        before = cb.get_by_service_name(SVC)

        result = cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            skip_if_pinned=True,
        )

        # A statement that matches no row is a decline, not a failure.
        assert result is True
        after = cb.get_by_service_name(SVC)
        assert after.state == before.state
        assert after.manually_controlled is True

    def test_open_ended_pin_declines_the_write(self, cb):
        _pin(cb, expires_in_seconds=None, state=CircuitBreakerStateEnum.OPEN.value)

        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            skip_if_pinned=True,
        )

        assert cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.OPEN.value

    def test_lapsed_pin_does_not_decline_the_write(self, cb):
        _pin(cb, expires_in_seconds=-1, state=CircuitBreakerStateEnum.OPEN.value)

        result = cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            skip_if_pinned=True,
        )

        assert result is True
        assert cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.CLOSED.value

    def test_unpinned_row_is_written_normally(self, cb):
        cb.get_or_create(SVC)

        cb.update_state(
            service_name=SVC,
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=utc_now(),
            skip_if_pinned=True,
        )

        assert cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.OPEN.value

    def test_without_the_directive_a_pinned_row_is_overwritten(self, cb):
        # The operator's own write-through must keep landing on a pinned row.
        _pin(cb, expires_in_seconds=600, state=CircuitBreakerStateEnum.OPEN.value)

        cb.update_state(service_name=SVC, state=CircuitBreakerStateEnum.CLOSED.value)

        assert cb.get_by_service_name(SVC).state == CircuitBreakerStateEnum.CLOSED.value
