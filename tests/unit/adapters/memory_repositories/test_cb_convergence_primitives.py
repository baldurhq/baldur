"""771 — the L1 write primitives a convergence lane uses.

Both primitives exist so a lane acting on a deliberately older remote read can
make its pin decision inside the very lock hold that performs its write. A
read-then-write pair would leave a window in which an operator's just-placed
override is inverted (state flipped while the manual-control fields survive,
leaving a row pinned closed that can no longer trip) or overwritten by the
stale remote view.

Covers:

- ``converge_to_closed_unless_pinned`` — the CLOSED transition, declined while
  an override is in force, writing the same fields as the close writeback and
  leaving the manual-control fields exactly as it found them.
- ``hydrate_snapshot(skip_if_local_pin_active=)`` — the wholesale restore
  declining to overwrite a local pin. Off by default, so every pre-existing
  restore lane keeps its unconditional replace.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time

FROZEN_NOW = "2026-02-10 10:00:00"


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _open_row(
    repo: InMemoryCircuitBreakerStateRepository,
    service: str,
    *,
    failure_count: int = 7,
) -> None:
    """Drive a row into the shape a stuck reject leaves behind.

    OPEN with counters and an OPEN-era timestamp, plus a half-open window
    part-way through — every field the CLOSED recipe is expected to clear.
    """
    repo.get_or_create(service)
    repo.update_state(
        service_name=service,
        state=CircuitBreakerStateEnum.OPEN.value,
        failure_count=failure_count,
        success_count=2,
        opened_at=utc_now(),
        half_open_request_count=3,
    )
    with repo._lock:
        entry = repo._storage[service]
        object.__setattr__(entry, "half_open_window_started_at", utc_now())


# =============================================================================
# converge_to_closed_unless_pinned
# =============================================================================


class TestConvergeToClosedUnlessPinnedBehavior:
    """The CLOSED write half of the convergence lane."""

    def test_absent_row_reports_no_write_and_creates_nothing(self, repo):
        """No local row means nothing to converge — and nothing to invent."""
        applied = repo.converge_to_closed_unless_pinned("svc")

        assert applied is False
        assert repo.get_by_service_name("svc") is None

    def test_open_row_transitions_to_closed_and_clears_the_open_era_fields(self, repo):
        """The recipe: state closed, counters and window discarded."""
        # Given — the shape a worker is stuck rejecting on
        _open_row(repo, "svc")

        # When
        applied = repo.converge_to_closed_unless_pinned("svc")

        # Then
        assert applied is True
        row = repo.get_by_service_name("svc")
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
        assert row.failure_count == 0
        assert row.success_count == 0
        assert row.opened_at is None
        assert row.half_open_request_count == 0
        assert row.half_open_window_started_at is None

    def test_half_open_row_transitions_to_closed(self, repo):
        """The producer-2 shape: a hydrated trial state the cluster left behind."""
        repo.get_or_create("svc")
        repo.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.HALF_OPEN.value
        )

        applied = repo.converge_to_closed_unless_pinned("svc")

        assert applied is True
        assert (
            repo.get_by_service_name("svc").state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    @pytest.mark.parametrize(
        ("expiry_offset_seconds", "expect_written"),
        [
            (None, False),
            (60, False),
            (0, True),
            (-60, True),
        ],
        ids=["open_ended", "before_expiry", "at_expiry", "after_expiry"],
    )
    def test_pin_declines_the_write_only_while_the_override_is_in_force(
        self, repo, expiry_offset_seconds, expect_written
    ):
        """Boundary: the override stops declining at its expiry, not after it."""
        with freeze_time(FROZEN_NOW):
            expires_at = (
                None
                if expiry_offset_seconds is None
                else utc_now() + timedelta(seconds=expiry_offset_seconds)
            )
            repo.set_manual_control(
                service_name="svc",
                state=CircuitBreakerStateEnum.OPEN.value,
                controlled_by_id=42,
                reason="incident-1",
                expires_at=expires_at,
            )

            applied = repo.converge_to_closed_unless_pinned("svc")

        assert applied is expect_written
        row = repo.get_by_service_name("svc")
        expected_state = (
            CircuitBreakerStateEnum.CLOSED.value
            if expect_written
            else CircuitBreakerStateEnum.OPEN.value
        )
        assert row.state == expected_state

    def test_manual_control_fields_survive_a_write_over_a_lifted_pin(self, repo):
        """A lifted override is data the lane must not rewrite while converging.

        The write is allowed once the pin is no longer in force, but the
        manual-control fields describe an operator decision and its expiry —
        clearing them here would erase the discriminator that tells a lifted
        pin from a row that was never pinned.
        """
        with freeze_time(FROZEN_NOW):
            expired_at = utc_now() - timedelta(seconds=60)
            repo.set_manual_control(
                service_name="svc",
                state=CircuitBreakerStateEnum.OPEN.value,
                controlled_by_id=42,
                reason="incident-1",
                expires_at=expired_at,
            )

            assert repo.converge_to_closed_unless_pinned("svc") is True

        row = repo.get_by_service_name("svc")
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
        assert row.manually_controlled is True
        assert row.controlled_by_id == 42
        assert row.control_reason == "incident-1"
        assert row.manual_override_expires_at == expired_at

    def test_repeated_convergence_leaves_the_same_row(self, repo):
        """Idempotency: a retried task must not observe a different outcome."""
        _open_row(repo, "svc")

        first_applied = repo.converge_to_closed_unless_pinned("svc")
        first_row = repo.get_by_service_name("svc")
        second_applied = repo.converge_to_closed_unless_pinned("svc")
        second_row = repo.get_by_service_name("svc")

        assert (first_applied, second_applied) == (True, True)
        assert second_row.state == first_row.state
        assert second_row.failure_count == first_row.failure_count
        assert second_row.opened_at == first_row.opened_at
        assert second_row.half_open_window_started_at is None

    def test_written_fields_match_the_reset_counts_then_close_recipe(self, repo):
        """The atomic primitive writes what the two-call recipe would.

        Atomicity is the reason the pair was collapsed; the fields it lands
        must stay the ones the store-authoritative close writeback produces,
        or the two convergence directions would leave differently-shaped rows.
        """
        # Given — the same starting row driven two ways
        atomic_repo = InMemoryCircuitBreakerStateRepository()
        _open_row(repo, "svc")
        _open_row(atomic_repo, "svc")

        # When
        repo.reset_counts("svc")
        repo.update_state(
            service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
        )
        atomic_repo.converge_to_closed_unless_pinned("svc")

        # Then — every field but the write timestamp agrees
        two_call = repo.get_by_service_name("svc")
        atomic = atomic_repo.get_by_service_name("svc")
        assert (
            atomic.state,
            atomic.failure_count,
            atomic.success_count,
            atomic.opened_at,
            atomic.manually_controlled,
            atomic.half_open_request_count,
            atomic.half_open_window_started_at,
        ) == (
            two_call.state,
            two_call.failure_count,
            two_call.success_count,
            two_call.opened_at,
            two_call.manually_controlled,
            two_call.half_open_request_count,
            two_call.half_open_window_started_at,
        )

    def test_last_failure_at_is_carried_over(self, repo):
        """Closing does not erase when the service last failed."""
        last_failure = utc_now() - timedelta(seconds=30)
        repo.get_or_create("svc")
        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            last_failure_at=last_failure,
        )

        repo.converge_to_closed_unless_pinned("svc")

        assert repo.get_by_service_name("svc").last_failure_at == last_failure


# =============================================================================
# hydrate_snapshot(skip_if_local_pin_active=)
# =============================================================================


class TestHydrateSnapshotPinGuardBehavior:
    """The restore lane's opt-in guard against a newer local pin."""

    @staticmethod
    def _pinned_local_row(repo, service: str, *, expires_at=None) -> None:
        repo.set_manual_control(
            service_name=service,
            state=CircuitBreakerStateEnum.OPEN.value,
            controlled_by_id=7,
            reason="local operator block",
            expires_at=expires_at,
        )

    def test_default_keyword_replaces_a_pinned_local_row(self, repo):
        """Off by default: the pre-existing restore lanes keep replacing.

        The construction-time load and the L1-miss hydration have no local
        decision to protect — an absent or never-hydrated row is exactly what
        they are filling in — so the guard must not change their behavior.
        """
        self._pinned_local_row(repo, "svc")
        snapshot = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
        )

        applied = repo.hydrate_snapshot(snapshot)

        assert applied is True
        row = repo.get_by_service_name("svc")
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
        assert row.manually_controlled is False

    def test_guard_declines_when_a_local_pin_is_in_force(self, repo):
        """A pin placed after the caller's remote read wins over that read."""
        self._pinned_local_row(repo, "svc")
        snapshot = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
        )

        applied = repo.hydrate_snapshot(snapshot, skip_if_local_pin_active=True)

        assert applied is False
        row = repo.get_by_service_name("svc")
        assert row.state == CircuitBreakerStateEnum.OPEN.value
        assert row.manually_controlled is True
        assert row.control_reason == "local operator block"

    def test_guard_allows_the_restore_once_the_local_pin_has_expired(self, repo):
        """An expired override is no longer a decision to protect."""
        with freeze_time(FROZEN_NOW):
            self._pinned_local_row(
                repo, "svc", expires_at=utc_now() - timedelta(seconds=1)
            )
            snapshot = CircuitBreakerStateData(
                service_name="svc", state=CircuitBreakerStateEnum.CLOSED.value
            )

            applied = repo.hydrate_snapshot(snapshot, skip_if_local_pin_active=True)

        assert applied is True
        assert (
            repo.get_by_service_name("svc").state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_guard_creates_the_row_when_none_exists_locally(self, repo):
        """No local row means no local pin — the guard has nothing to decline."""
        snapshot = CircuitBreakerStateData(
            service_name="svc", state=CircuitBreakerStateEnum.HALF_OPEN.value
        )

        applied = repo.hydrate_snapshot(snapshot, skip_if_local_pin_active=True)

        assert applied is True
        assert (
            repo.get_by_service_name("svc").state
            == CircuitBreakerStateEnum.HALF_OPEN.value
        )

    def test_guard_declines_an_unpinned_snapshot_over_a_pinned_local_row(self, repo):
        """The guard reads the LOCAL row, never the snapshot's own pin fields.

        The snapshot the convergence lane carries is a remote row that may be
        unpinned; what decides is whether this worker has an override of its
        own that the remote read predates.
        """
        self._pinned_local_row(repo, "svc")
        unpinned_snapshot = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=False,
        )

        applied = repo.hydrate_snapshot(
            unpinned_snapshot, skip_if_local_pin_active=True
        )

        assert applied is False
        assert repo.get_by_service_name("svc").manually_controlled is True

    def test_restore_delivers_the_snapshot_pin_fields_to_an_unpinned_row(self, repo):
        """The reason the lane hydrates instead of copying state alone.

        A state-only copy would leave this worker unpinned and free to record
        outcomes, re-trip, and mirror an OPEN over the operator's still-active
        decision. The wholesale restore carries the override across.
        """
        repo.get_or_create("svc")
        expires_at = utc_now() + timedelta(seconds=300)
        pinned_snapshot = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            controlled_by_id=99,
            control_reason="remote operator allow",
            manual_override_expires_at=expires_at,
        )

        applied = repo.hydrate_snapshot(pinned_snapshot, skip_if_local_pin_active=True)

        assert applied is True
        row = repo.get_by_service_name("svc")
        assert row.manually_controlled is True
        assert row.controlled_by_id == 99
        assert row.control_reason == "remote operator allow"
        assert row.manual_override_expires_at == expires_at
