"""
Unit tests for SQLCircuitBreakerStateRepository.

Coverage:
- get_or_create is idempotent — second call returns the same row.
- record_failure / record_success increment the right counters.
- State transitions (CLOSED → OPEN → CLOSED via atomic_force_*).
- atomic_force_open returns (True, previous, new) tuple contract.
- set_manual_control / clear_manual_control with TTL.
- delete_state removes row and returns boolean.
"""

from __future__ import annotations

import pytest

from baldur.adapters.sql.circuit_breaker import (
    SQLCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateEnum


@pytest.fixture
def cb(get_sqlite_conn) -> SQLCircuitBreakerStateRepository:
    return SQLCircuitBreakerStateRepository(get_sqlite_conn)


class TestSQLCircuitBreakerCreationBehavior:
    """get_or_create + initial state."""

    def test_get_or_create_returns_closed_initial_state(self, cb):
        state = cb.get_or_create("openai")
        assert state.service_name == "openai"
        assert state.state == CircuitBreakerStateEnum.CLOSED.value
        assert state.failure_count == 0
        assert state.success_count == 0

    def test_get_or_create_is_idempotent(self, cb):
        """Second call returns the same logical row (same id)."""
        first = cb.get_or_create("openai")
        second = cb.get_or_create("openai")
        assert first.id == second.id

    def test_get_by_service_name_returns_none_for_missing(self, cb):
        assert cb.get_by_service_name("absent") is None


class TestSQLCircuitBreakerCounterBehavior:
    """record_failure / record_success."""

    def test_record_failure_increments_counter(self, cb):
        cb.record_failure("openai")
        cb.record_failure("openai")
        state = cb.get_by_service_name("openai")
        assert state.failure_count == 2
        assert state.last_failure_at is not None

    def test_record_success_increments_counter(self, cb):
        cb.record_success("openai")
        cb.record_success("openai")
        cb.record_success("openai")
        state = cb.get_by_service_name("openai")
        assert state.success_count == 3


class TestSQLCircuitBreakerStateTransitionBehavior:
    """Circuit state transitions via atomic_force_* operations."""

    def test_atomic_force_open_returns_previous_and_new_states(self, cb):
        cb.get_or_create("openai")
        ok, previous, new = cb.atomic_force_open("openai", reason="maintenance")

        assert ok is True
        assert previous == CircuitBreakerStateEnum.CLOSED.value
        assert new == CircuitBreakerStateEnum.OPEN.value

        state = cb.get_by_service_name("openai")
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.manually_controlled is True
        assert state.control_reason == "maintenance"
        assert state.opened_at is not None

    def test_atomic_force_close_resets_counters(self, cb):
        cb.record_failure("openai")
        cb.record_failure("openai")
        cb.atomic_force_open("openai", reason="x")

        ok, previous, new = cb.atomic_force_close("openai", reason="recover")
        assert ok is True
        assert previous == CircuitBreakerStateEnum.OPEN.value
        assert new == CircuitBreakerStateEnum.CLOSED.value

        state = cb.get_by_service_name("openai")
        assert state.failure_count == 0
        assert state.success_count == 0
        assert state.opened_at is None
        assert state.control_reason == "recover"

    def test_atomic_reset_returns_false_for_missing_service(self, cb):
        ok, previous, new = cb.atomic_reset("absent")
        assert ok is False
        assert previous == ""
        assert new == ""

    def test_atomic_reset_clears_manual_control_and_counters(self, cb):
        cb.record_failure("openai")
        cb.atomic_force_open("openai", reason="x")

        ok, _, new = cb.atomic_reset("openai", reason="ops-ack")
        assert ok is True
        assert new == CircuitBreakerStateEnum.CLOSED.value

        state = cb.get_by_service_name("openai")
        assert state.manually_controlled is False
        assert state.failure_count == 0


class TestSQLCircuitBreakerManualControlBehavior:
    """set_manual_control / clear_manual_control."""

    def test_set_manual_control_open_flags_and_opened_at(self, cb):
        cb.set_manual_control(
            "openai",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=42,
            reason="maint",
        )
        state = cb.get_by_service_name("openai")
        assert state.manually_controlled is True
        assert state.controlled_by_id == 42
        assert state.state == CircuitBreakerStateEnum.OPEN.value
        assert state.opened_at is not None

    def test_clear_manual_control_resets_reason_by_default(self, cb):
        cb.set_manual_control(
            "openai",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=42,
            reason="maint",
        )
        cb.clear_manual_control("openai")

        state = cb.get_by_service_name("openai")
        assert state.manually_controlled is False
        assert state.controlled_by_id is None
        assert state.control_reason == ""

    def test_clear_manual_control_preserves_reason_when_requested(self, cb):
        cb.set_manual_control(
            "openai",
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=42,
            reason="maint",
        )
        cb.clear_manual_control("openai", preserve_reason=True)

        state = cb.get_by_service_name("openai")
        assert state.manually_controlled is False
        assert state.control_reason == "maint"

    def test_clear_manual_control_missing_service_returns_false(self, cb):
        assert cb.clear_manual_control("absent") is False


class TestSQLCircuitBreakerDeleteBehavior:
    """delete_state removes the row."""

    def test_delete_existing_returns_true(self, cb):
        cb.get_or_create("openai")
        assert cb.delete_state("openai") is True
        assert cb.get_by_service_name("openai") is None

    def test_delete_missing_returns_false(self, cb):
        assert cb.delete_state("absent") is False


class TestSQLCircuitBreakerListingBehavior:
    """get_all_states ordering."""

    def test_get_all_states_returns_every_row(self, cb):
        cb.get_or_create("a")
        cb.get_or_create("b")
        cb.get_or_create("c")
        names = sorted(s.service_name for s in cb.get_all_states())
        assert names == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# PR2 review fix #1 — record_failure / record_success raise on missing row
# ---------------------------------------------------------------------------


class TestRecordEventVanishedRowBehavior:
    """When the row disappears mid-call, raise RuntimeError (not assert).

    ``record_failure`` / ``record_success`` call ``get_by_service_name``
    twice: once via ``get_or_create`` (the row exists), and once after the
    UPDATE (where the row may have been concurrently deleted). The patch
    returns the real row on the first call and ``None`` on the second so
    the guard is exercised without losing the row from the schema.
    """

    def _patch_second_lookup_to_none(self, cb, monkeypatch):
        real_lookup = cb.get_by_service_name
        call_state = {"n": 0}

        def _fake_lookup(name):
            call_state["n"] += 1
            if call_state["n"] == 1:
                return real_lookup(name)
            return None

        monkeypatch.setattr(cb, "get_by_service_name", _fake_lookup)

    def test_record_failure_raises_when_row_vanishes(self, cb, monkeypatch):
        """Simulated concurrent delete after UPDATE → loud RuntimeError."""
        cb.get_or_create("openai")
        self._patch_second_lookup_to_none(cb, monkeypatch)

        with pytest.raises(RuntimeError, match="vanished after record_failure"):
            cb.record_failure("openai")

    def test_record_success_raises_when_row_vanishes(self, cb, monkeypatch):
        """Same guard for the success counter path."""
        cb.get_or_create("openai")
        self._patch_second_lookup_to_none(cb, monkeypatch)

        with pytest.raises(RuntimeError, match="vanished after record_success"):
            cb.record_success("openai")


# ---------------------------------------------------------------------------
# 498 D8 — record_success_with_close_check (SELECT FOR UPDATE NOWAIT)
# ---------------------------------------------------------------------------


class TestSQLRecordSuccessWithCloseCheckBehavior:
    """498 D8: atomic HALF_OPEN -> CLOSED close-check via the SQL override.

    Mirrors the Redis Lua at D1: ``half_open`` increments and conditionally
    transitions to CLOSED; ``closed`` is a no-write race-loser / post-crash
    convergence signal; other states return the stale sentinel unchanged.
    """

    def _force_half_open(self, cb, service_name: str, success_count: int) -> None:
        """Seed the SQL row in HALF_OPEN with a primed success_count."""
        cb.get_or_create(service_name)
        cb.update_state(
            service_name=service_name,
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=success_count,
        )

    def test_threshold_minus_one_increments_without_closing(self, cb):
        # Given: HALF_OPEN with success_count one below the threshold.
        self._force_half_open(cb, "svc", success_count=0)

        # When: a single success arrives and threshold=2.
        attempt = cb.record_success_with_close_check("svc", success_threshold=2)

        # Then: counter increments but the breaker stays HALF_OPEN.
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert attempt.state.success_count == 1

        persisted = cb.get_by_service_name("svc")
        assert persisted.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert persisted.success_count == 1

    def test_threshold_boundary_closes_and_resets_counters(self, cb):
        # Given: HALF_OPEN with success_count just below the threshold.
        self._force_half_open(cb, "svc", success_count=1)

        # When: the incoming success crosses the threshold of 2.
        attempt = cb.record_success_with_close_check("svc", success_threshold=2)

        # Then: did_close=True, state CLOSED, counters and opened_at cleared.
        assert attempt.did_close is True
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value
        assert attempt.state.success_count == 0

        persisted = cb.get_by_service_name("svc")
        assert persisted.state == CircuitBreakerStateEnum.CLOSED.value
        assert persisted.failure_count == 0
        assert persisted.success_count == 0
        assert persisted.half_open_request_count == 0
        assert persisted.opened_at is None

    def test_closed_state_returns_no_write_race_loser_sentinel(self, cb):
        # Given: another worker has already closed the breaker.
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
        )

        attempt = cb.record_success_with_close_check("svc", success_threshold=2)

        # Race-loser / post-crash convergence: did_close=False with state=closed.
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value
        assert attempt.state.success_count == 0

    def test_open_state_returns_stale_sentinel_without_writing(self, cb):
        # Given: stale OPEN state (the wrapper's stale-L2 guard handles this).
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        attempt = cb.record_success_with_close_check("svc", success_threshold=2)

        # Stale-state sentinel: did_close=False, returned state mirrors L2.
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.success_count == 0


class TestSQLRecordSuccessWithCloseCheckContract:
    """498 D8 hardcoded contract: race-loser shape + state_data field defaults."""

    def test_close_branch_returns_attempt_with_zero_success_count(self, cb):
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=2,
        )

        attempt = cb.record_success_with_close_check("svc", success_threshold=3)

        # Boundary just below threshold (success_count goes from 2 to 3).
        assert attempt.did_close is True
        assert attempt.state.state == "closed"
        assert attempt.state.success_count == 0
        # Synthetic-default auxiliary fields per D2 (callers read only
        # did_close / state.state / state.success_count).
        assert attempt.state.failure_count == 0
        assert attempt.state.opened_at is None
        assert attempt.state.half_open_request_count == 0


# ---------------------------------------------------------------------------
# 656 D7 — record_failure_with_open_check (SELECT FOR UPDATE NOWAIT)
# ---------------------------------------------------------------------------


class TestSQLRecordFailureWithOpenCheckBehavior:
    """656 D7: atomic HALF_OPEN -> OPEN re-open via the SQL override.

    Symmetric mirror of the close-check at D8. A single HALF_OPEN failure
    re-opens unconditionally (no threshold); ``open`` is a no-write
    race-loser carrying the existing ``opened_at``; ``closed`` is the
    trust-L2 quorum-close sentinel that the Layered wrapper routes on.
    """

    def test_half_open_re_opens_and_resets_counters(self, cb):
        # Given: HALF_OPEN with a primed success_count + half_open window.
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=3,
            half_open_request_count=2,
        )

        # When: a HALF_OPEN failure arrives.
        attempt = cb.record_failure_with_open_check("svc")

        # Then: did_open=True, OPEN, opened_at set, counters/watermark reset.
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.opened_at is not None

        persisted = cb.get_by_service_name("svc")
        assert persisted.state == CircuitBreakerStateEnum.OPEN.value
        assert persisted.failure_count == 0
        assert persisted.success_count == 0
        assert persisted.half_open_request_count == 0
        assert persisted.opened_at is not None

    def test_open_state_returns_race_loser_carrying_opened_at(self, cb):
        # Given: another worker already re-opened the breaker.
        from baldur.utils.time import utc_now

        opened = utc_now()
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            opened_at=opened,
        )

        attempt = cb.record_failure_with_open_check("svc")

        # Race-loser / already-open: did_open=False, opened_at carried (no
        # write, so the Layered wrapper writes back L1=open with the timestamp).
        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.opened_at is not None

    def test_closed_state_returns_stale_sentinel_without_writing(self, cb):
        # Given: a concurrent quorum closed the breaker (trust-L2 branch).
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
        )

        attempt = cb.record_failure_with_open_check("svc")

        # Stale-state sentinel: did_open=False, returned state mirrors the row.
        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value

        # No re-open: the row stays CLOSED (a straggler failure never
        # overrides the cluster's recovery).
        persisted = cb.get_by_service_name("svc")
        assert persisted.state == CircuitBreakerStateEnum.CLOSED.value


class TestSQLRecordFailureWithOpenCheckContract:
    """656 D7 hardcoded contract: synthetic-default auxiliary fields."""

    def test_open_branch_returns_attempt_with_synthetic_defaults(self, cb):
        cb.get_or_create("svc")
        cb.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        attempt = cb.record_failure_with_open_check("svc")

        assert attempt.did_open is True
        assert attempt.state.state == "open"
        # Synthetic-default auxiliary fields (callers read only
        # did_open / state.state / state.opened_at).
        assert attempt.state.failure_count == 0
        assert attempt.state.success_count == 0
        assert attempt.state.half_open_request_count == 0
