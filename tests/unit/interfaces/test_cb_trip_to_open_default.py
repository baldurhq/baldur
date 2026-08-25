"""The ``trip_to_open`` ABC default and the declined-by-pin result shape.

``trip_to_open`` is the write half of the automatic CLOSED->OPEN trip. Every
adapter that can perform the read-decide-write atomically overrides it; the
default here is what an adapter without that capability falls back to, and it
is the reference the three overrides are read against — its branch contract is
what the Redis Lua, the SQL row lock and the InMemory lock hold each have to
reproduce.

Covered:
- the stored-state matrix (active pin / lapsed pin / closed / missing / open /
  half_open / corrupted), including which branches write and which do not;
- the declined-by-pin result, whose sentinel token is what tells the layered
  wrapper to deliver the pinned row instead of writing a state back.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
    pinned_trip_attempt,
)
from baldur.utils.time import utc_now

FAILURE_COUNT = 5


class _RecordingRepo(CircuitBreakerStateRepository):
    """Minimal concrete repository that records its own writes.

    Implements only what the ABC demands, so ``trip_to_open`` runs against the
    inherited default rather than an override. Writes are recorded rather than
    merely applied: the default's contract is as much about which calls it
    makes as about what it returns.
    """

    def __init__(
        self,
        state: str,
        *,
        manually_controlled: bool = False,
        manual_override_expires_at: datetime | None = None,
        opened_at: datetime | None = None,
        exists: bool = True,
    ):
        self.state = CircuitBreakerStateData(
            service_name="svc",
            state=state,
            failure_count=2,
            success_count=1,
            opened_at=opened_at,
            manually_controlled=manually_controlled,
            manual_override_expires_at=manual_override_expires_at,
        )
        self._exists = exists
        self.update_state_calls: list[dict] = []
        self.clear_manual_control_calls: list[dict] = []

    # -- reads ------------------------------------------------------------
    def get_or_create(self, service_name):
        self._exists = True
        return self.state

    def get_by_service_name(self, service_name):
        return self.state if self._exists else None

    def get_all_states(self):
        return [self.state]

    # -- writes -----------------------------------------------------------
    def update_state(
        self,
        service_name,
        state,
        failure_count=None,
        success_count=None,
        opened_at=None,
        last_failure_at=None,
        half_open_request_count=None,
        reset_half_open_count=False,
        clear_opened_at=False,
        skip_if_pinned=False,
    ):
        self.update_state_calls.append(
            {
                "state": state,
                "failure_count": failure_count,
                "success_count": success_count,
                "opened_at": opened_at,
                "reset_half_open_count": reset_half_open_count,
                "clear_opened_at": clear_opened_at,
                "skip_if_pinned": skip_if_pinned,
            }
        )
        self.state = CircuitBreakerStateData(
            service_name="svc",
            state=state,
            failure_count=failure_count if failure_count is not None else 0,
            success_count=success_count if success_count is not None else 0,
            opened_at=opened_at,
            manually_controlled=self.state.manually_controlled,
            manual_override_expires_at=self.state.manual_override_expires_at,
        )
        return True

    def set_manual_control(
        self, service_name, state, controlled_by_id=None, reason="", expires_at=None
    ):
        return True

    def clear_manual_control(self, service_name, preserve_reason=False):
        self.clear_manual_control_calls.append({"preserve_reason": preserve_reason})
        self.state = CircuitBreakerStateData(
            service_name="svc",
            state=self.state.state,
            failure_count=self.state.failure_count,
            success_count=self.state.success_count,
            opened_at=self.state.opened_at,
            manually_controlled=False,
            manual_override_expires_at=None,
        )
        return True

    # -- unused by trip_to_open, required by the ABC -----------------------
    def record_failure(self, service_name):
        return self.state

    def record_success(self, service_name):
        return self.state

    def reset(self, service_name):
        return True

    def reset_half_open_count(self, service_name):
        return None

    def delete_state(self, service_name):
        return True

    def atomic_force_open(
        self, service_name, reason="", controlled_by_id=None, ttl_minutes=None
    ):
        return (True, "", "open")

    def atomic_force_close(
        self, service_name, reason="", controlled_by_id=None, ttl_minutes=None
    ):
        return (True, "", "closed")

    def atomic_reset(self, service_name, reason="", controlled_by_id=None):
        return (True, "", "closed")

    def try_acquire_half_open_slot(self, service_name, limit, stuck_timeout_seconds):
        return (True, "open", "half_open")


# =============================================================================
# Contract — the declined-by-pin result shape
# =============================================================================


class TestPinnedTripAttemptContract:
    """One spelling of "an override declined this trip", shared by all four
    implementations so the token and the reported fields cannot drift.
    """

    def test_sentinel_token_value(self):
        # The token is compared as a literal by the layered router and by the
        # service; it is never a stored circuit state.
        assert CIRCUIT_BREAKER_PINNED_TOKEN == "pinned"
        assert CIRCUIT_BREAKER_PINNED_TOKEN not in {
            member.value for member in CircuitBreakerStateEnum
        }

    def test_declined_attempt_carries_the_token_and_the_expiry(self):
        expires_at = utc_now() + timedelta(minutes=10)

        attempt = pinned_trip_attempt("svc", expires_at)

        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.service_name == "svc"
        assert attempt.state.manually_controlled is True
        assert attempt.state.manual_override_expires_at == expires_at

    def test_declined_attempt_carries_no_opened_at(self):
        # Nothing a writeback could mistake for a circuit state: the row
        # reports the override, not a transition.
        attempt = pinned_trip_attempt("svc", None)

        assert attempt.state.opened_at is None
        assert attempt.state.manual_override_expires_at is None


# =============================================================================
# Behavior — the ABC default's branch matrix
# =============================================================================


class TestTripToOpenDefaultBehavior:
    """Read-then-write default: the branch contract every override mirrors."""

    def test_closed_state_writes_the_open_row_and_reports_did_open(self):
        repo = _RecordingRepo(CircuitBreakerStateEnum.CLOSED.value)

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        # The whole row is written, not a partial patch: the caller's failure
        # count verbatim, a cleared success count, a fresh opened_at, and the
        # half-open counter reset in the same write.
        assert len(repo.update_state_calls) == 1
        call = repo.update_state_calls[0]
        assert call["state"] == CircuitBreakerStateEnum.OPEN.value
        assert call["failure_count"] == FAILURE_COUNT
        assert call["success_count"] == 0
        assert call["opened_at"] is not None
        assert call["reset_half_open_count"] is True

    def test_missing_row_is_treated_as_closed_and_trips(self):
        # Absent is exactly the state a trip fires from — unlike the
        # open-check, there is no "stale relative to the caller" reading here.
        repo = _RecordingRepo(CircuitBreakerStateEnum.CLOSED.value, exists=False)

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value

    def test_open_state_is_a_race_loser_that_writes_nothing(self):
        opened = utc_now() - timedelta(seconds=30)
        repo = _RecordingRepo(CircuitBreakerStateEnum.OPEN.value, opened_at=opened)

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        # The existing opened_at is carried, not restamped — the winner's
        # timestamp is what the loser writes back to its own L1.
        assert attempt.state.opened_at == opened
        assert repo.update_state_calls == []

    def test_half_open_state_writes_nothing_and_does_not_clobber(self):
        # A peer already tripped and the cluster progressed to recovery
        # testing; clobbering back to OPEN would revert a legitimate
        # transition.
        repo = _RecordingRepo(CircuitBreakerStateEnum.HALF_OPEN.value)

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert repo.update_state_calls == []

    def test_unrecognized_stored_state_writes_nothing_and_is_returned_as_is(self):
        repo = _RecordingRepo("corrupted")

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.did_open is False
        assert attempt.state.state == "corrupted"
        assert repo.update_state_calls == []

    def test_active_pin_declines_the_write_before_any_state_branch(self):
        # Given: a CLOSED row an operator has pinned — the branch that would
        # otherwise write OPEN.
        expires_at = utc_now() + timedelta(minutes=10)
        repo = _RecordingRepo(
            CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
        )

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        # Then: no write lands over the operator's decision, and the caller is
        # told which override suppressed the trip and until when.
        assert attempt.did_open is False
        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert attempt.state.manual_override_expires_at == expires_at
        assert repo.update_state_calls == []

    def test_open_ended_pin_declines_the_write(self):
        repo = _RecordingRepo(
            CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=None,
        )

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert attempt.state.state == CIRCUIT_BREAKER_PINNED_TOKEN
        assert repo.update_state_calls == []

    def test_lapsed_pin_trips_and_clears_the_stale_flag(self):
        # Given: a CLOSED row whose override expired but whose flag nobody
        # cleared — the sweep that does runs in one process per host.
        repo = _RecordingRepo(
            CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(seconds=1),
        )

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        # Then: the trip lands, and the lapsed flag is dropped in the same
        # operation — left set, it would hide the freshly opened row from the
        # recovery lane, which still filters on the raw flag.
        assert attempt.did_open is True
        assert len(repo.update_state_calls) == 1
        assert repo.clear_manual_control_calls == [{"preserve_reason": True}]

    def test_unpinned_trip_does_not_touch_manual_control(self):
        # Negative half of the lapsed-pin clear: an ordinary trip must not
        # issue a manual-control write at all.
        repo = _RecordingRepo(CircuitBreakerStateEnum.CLOSED.value)

        repo.trip_to_open("svc", FAILURE_COUNT)

        assert repo.clear_manual_control_calls == []

    @pytest.mark.parametrize(
        "stored_state",
        [
            CircuitBreakerStateEnum.CLOSED.value,
            CircuitBreakerStateEnum.OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            "corrupted",
        ],
    )
    def test_every_branch_returns_an_open_attempt(self, stored_state):
        repo = _RecordingRepo(stored_state)

        attempt = repo.trip_to_open("svc", FAILURE_COUNT)

        assert isinstance(attempt, CircuitBreakerOpenAttempt)
        assert isinstance(attempt.state, CircuitBreakerStateData)
