"""Which admission exits record a refusal as failure evidence, and which do not.

793 D1. A call the breaker turns away because the dependency is cut off is a
call that did not succeed, so the admission path's refusal exits append a
failure to the outcome window through ``record_rejection``. Two refusals are
not evidence about the dependency and record nothing: a downstream checker's
verdict (about a different name) and an operator's Block in force. An admitted
CLOSED call records nothing here either — it carries the window's epoch out as
the hint for its own success record.

Verification techniques applied:
- Exit inventory (parametrized): E1 downstream / E3 pinned / E3 open /
  E3' frozen / E4 half_open_full / E5 trial, each asserted on the window delta
  and the decision, so a refusal that stopped recording (or an admission that
  started) flips the test
- Contract: the CLOSED decision carries the epoch admission saw
- Dependency interaction: ``record_rejection`` forwards the row the refusal
  was decided on, and the effective window size
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"
RECOVERY_TIMEOUT = 60
WINDOW_SIZE = 50

_FREEZE_GATE = "baldur.services.circuit_breaker.service.should_allow_cb_state_change"


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def service(repo) -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            failure_threshold=5,
            recovery_timeout=RECOVERY_TIMEOUT,
            half_open_max_calls=1,
            sliding_window_size=WINDOW_SIZE,
            minimum_calls=10,
        ),
        repository=repo,
    )


def _hydrate(repo, state: str, *, opened_ago: int = 0, **overrides) -> None:
    repo.hydrate_snapshot(
        CircuitBreakerStateData(
            service_name=SERVICE,
            state=state,
            opened_at=(
                utc_now() - timedelta(seconds=opened_ago)
                if state != CircuitBreakerStateEnum.CLOSED.value
                else None
            ),
            **overrides,
        )
    )


def _open_within_timeout(repo) -> None:
    _hydrate(repo, CircuitBreakerStateEnum.OPEN.value, opened_ago=1)


def _open_past_timeout(repo) -> None:
    _hydrate(repo, CircuitBreakerStateEnum.OPEN.value, opened_ago=RECOVERY_TIMEOUT + 5)


def _pinned_open(repo) -> None:
    _hydrate(
        repo,
        CircuitBreakerStateEnum.OPEN.value,
        opened_ago=1,
        manually_controlled=True,
        manual_override_expires_at=utc_now() + timedelta(minutes=10),
    )


def _half_open(repo) -> None:
    _hydrate(
        repo, CircuitBreakerStateEnum.HALF_OPEN.value, opened_ago=RECOVERY_TIMEOUT + 5
    )


# =============================================================================
# Behavior — the exit inventory
# =============================================================================


class TestAdmissionRefusalEvidenceBehavior:
    """Every ``_evaluate_admission`` exit, by its window delta."""

    @pytest.mark.parametrize(
        ("prepare", "prior_admissions", "expected_allowed", "expected_recorded"),
        [
            pytest.param(_open_within_timeout, 0, False, True, id="e3_open_records"),
            pytest.param(_pinned_open, 0, False, False, id="e3_pinned_records_nothing"),
            pytest.param(_half_open, 0, True, False, id="e5_trial_records_nothing"),
            pytest.param(_half_open, 1, False, True, id="e4_half_open_full_records"),
        ],
    )
    def test_refusal_exit_records_iff_the_dependency_refused_the_call(
        self,
        service,
        repo,
        prepare,
        prior_admissions,
        expected_allowed,
        expected_recorded,
    ):
        """One admission against a prepared row; the window is the witness.

        E4 is reached by exhausting the single trial slot with a prior
        admission, so the refusal under test is the slot's, not the row's.
        """
        # Given
        prepare(repo)
        for _ in range(prior_admissions):
            service.should_allow_with_state(SERVICE)
        before = service.get_window_evidence(SERVICE)

        # When
        decision = service.should_allow_with_state(SERVICE)

        # Then
        after = service.get_window_evidence(SERVICE)
        assert decision.allowed is expected_allowed
        expected_delta = (1, 1) if expected_recorded else (0, 0)
        assert (after[0] - before[0], after[1] - before[1]) == expected_delta

    def test_e3_prime_frozen_refusal_records(self, service, repo):
        """An OPEN row past its timeout, held by the freeze: still a refusal."""
        _open_past_timeout(repo)

        with patch(_FREEZE_GATE, return_value=False):
            decision = service.should_allow_with_state(SERVICE)

        assert decision.allowed is False
        assert service.get_window_evidence(SERVICE) == (1, 1)
        # The freeze withheld the trial: the row never moved to HALF_OPEN.
        assert (
            repo.get_by_service_name(SERVICE).state
            == CircuitBreakerStateEnum.OPEN.value
        )

    def test_e1_downstream_refusal_records_nothing(self, service, repo):
        """A checker's verdict is about a different name — not this one's evidence."""
        repo.get_or_create(SERVICE)
        service.register_downstream_checker(lambda name: False)

        decision = service.should_allow_with_state(SERVICE)

        assert decision.allowed is False
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_e2_closed_admission_records_nothing_and_carries_the_epoch(
        self, service, repo
    ):
        """The admitted call records its own outcome later; the hint is the epoch."""
        repo.get_or_create(SERVICE)
        service._outcome_window.bump(SERVICE)
        service._outcome_window.bump(SERVICE)

        decision = service.should_allow_with_state(SERVICE)

        assert decision.allowed is True
        assert decision.window_epoch == service._outcome_window.epoch_of(SERVICE) == 2
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_refused_decision_carries_no_epoch(self, service, repo):
        """Only the CLOSED path hands out a hint; a refusal carries the default."""
        _open_within_timeout(repo)
        service._outcome_window.bump(SERVICE)

        decision = service.should_allow_with_state(SERVICE)

        assert decision.window_epoch == 0

    def test_repeated_refusals_accumulate_for_the_whole_open_period(
        self, service, repo
    ):
        """Every refused call counts, bounded by the window size."""
        _open_within_timeout(repo)

        for _ in range(WINDOW_SIZE + 3):
            service.should_allow(SERVICE)

        assert service.get_window_evidence(SERVICE) == (WINDOW_SIZE, WINDOW_SIZE)

    def test_record_rejection_forwards_the_effective_window_size(self, service, repo):
        """Dependency interaction: the ring is sized by the name's effective config."""
        _open_within_timeout(repo)
        row = repo.get_by_service_name(SERVICE)

        with patch.object(
            service._outcome_window, "record_rejection", autospec=True
        ) as record:
            service.record_rejection(SERVICE, row)

        record.assert_called_once_with(SERVICE, WINDOW_SIZE)

    def test_record_rejection_on_a_pin_active_row_reaches_no_window(
        self, service, repo
    ):
        """The pin check sits in front of the append, not in the window."""
        _pinned_open(repo)
        row = repo.get_by_service_name(SERVICE)

        with patch.object(
            service._outcome_window, "record_rejection", autospec=True
        ) as record:
            service.record_rejection(SERVICE, row)

        record.assert_not_called()

    def test_record_rejection_on_a_lapsed_pin_records(self, service, repo):
        """Control: a pin past its expiry no longer shields the refusal."""
        _hydrate(
            repo,
            CircuitBreakerStateEnum.OPEN.value,
            opened_ago=1,
            manually_controlled=True,
            manual_override_expires_at=utc_now() - timedelta(seconds=1),
        )
        row = repo.get_by_service_name(SERVICE)

        service.record_rejection(SERVICE, row)

        assert service.get_window_evidence(SERVICE) == (1, 1)


class TestRefusalOnAClosedStoreAnswerBehavior:
    """A refusal the store answered with CLOSED is convergence, not evidence.

    Regression (793 /verify, refuter C20): this process holds a stale OPEN
    row past ``recovery_timeout`` while a peer has already closed the name in
    the store. The atomic acquire answers ``no_op`` with the store's CLOSED
    row; the admission is refused once (the reject-path convergence) and the
    fresh CLOSED period must not open with that refusal as its first failure.
    """

    def test_no_op_acquire_on_a_closed_row_records_no_rejection(self, service, repo):
        # Given: a stale OPEN row past its timeout; the store closes it between
        # the admission read and the acquire.
        _open_past_timeout(repo)
        service._outcome_window.observe_state(
            SERVICE, CircuitBreakerStateEnum.OPEN.value
        )
        service._outcome_window.record_rejection(SERVICE, WINDOW_SIZE)
        original_acquire = repo.try_acquire_half_open_slot

        def _peer_closes_then_acquire(**kwargs):
            _hydrate(repo, CircuitBreakerStateEnum.CLOSED.value)
            return original_acquire(**kwargs)

        # When
        with patch.object(
            repo, "try_acquire_half_open_slot", side_effect=_peer_closes_then_acquire
        ):
            decision = service.should_allow_with_state(SERVICE)

        # Then: refused once (convergence), the window cleared on the observed
        # CLOSED, and the refusal is not the new period's first failure.
        assert decision.allowed is False
        assert decision.state.state == CircuitBreakerStateEnum.CLOSED.value
        assert service.get_window_evidence(SERVICE) == (0, 0)

    def test_half_open_full_refusal_is_still_recorded(self, service, repo):
        """Control: a refusal on a row still HALF_OPEN is dependency evidence."""
        _half_open(repo)
        service.should_allow_with_state(SERVICE)  # takes the single trial slot

        decision = service.should_allow_with_state(SERVICE)

        assert decision.allowed is False
        assert decision.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert service.get_window_evidence(SERVICE) == (1, 1)
