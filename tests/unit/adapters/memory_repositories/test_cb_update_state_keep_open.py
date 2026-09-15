"""``update_state(keep_open=)`` on the in-memory circuit-breaker repository.

793 D3. A snapshot writer — the consecutive-count reset, the L1 -> L2 mirror —
may refresh a CLOSED row but never close one: with ``keep_open`` the write is
declined, inside the lock hold that would perform it, when the stored row is
``open`` or ``half_open``. A declined write returns ``True`` exactly as the pin
guard does; the stored row, ``opened_at`` included, is untouched. Without the
directive the write behaves as before, and an operator's close (which passes
``False``) still moves a stored OPEN row.

Verification techniques applied:
- Contract (parametrized): stored state x ``keep_open`` -> return value and
  the stored row read back, never the return value alone
- Boundary: an absent row returns ``False`` under either directive
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now

SERVICE = "payment-api"
CLOSED = CircuitBreakerStateEnum.CLOSED.value
OPEN = CircuitBreakerStateEnum.OPEN.value
HALF_OPEN = CircuitBreakerStateEnum.HALF_OPEN.value


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _store(repo, state: str, failure_count: int = 3) -> CircuitBreakerStateData:
    repo.hydrate_snapshot(
        CircuitBreakerStateData(
            service_name=SERVICE,
            state=state,
            failure_count=failure_count,
            opened_at=utc_now() - timedelta(seconds=5) if state != CLOSED else None,
        )
    )
    return repo.get_by_service_name(SERVICE)


class TestUpdateStateKeepOpenContract:
    """The decline table: which stored rows a guarded CLOSED write may move."""

    @pytest.mark.parametrize(
        ("stored_state", "keep_open", "expected_return", "expect_moved"),
        [
            pytest.param(CLOSED, True, True, True, id="closed_keep_open_writes"),
            pytest.param(OPEN, True, True, False, id="open_keep_open_declines"),
            pytest.param(
                HALF_OPEN, True, True, False, id="half_open_keep_open_declines"
            ),
            pytest.param(CLOSED, False, True, True, id="closed_unguarded_writes"),
            pytest.param(OPEN, False, True, True, id="open_unguarded_writes"),
            pytest.param(HALF_OPEN, False, True, True, id="half_open_unguarded_writes"),
        ],
    )
    def test_closed_write_against_a_stored_row(
        self, repo, stored_state, keep_open, expected_return, expect_moved
    ):
        """One ``update_state(state='closed', failure_count=0)`` per stored row."""
        # Given
        before = _store(repo, stored_state)

        # When
        result = repo.update_state(
            service_name=SERVICE, state=CLOSED, failure_count=0, keep_open=keep_open
        )

        # Then: the return value is the caller's contract, the row is the truth.
        assert result is expected_return
        after = repo.get_by_service_name(SERVICE)
        if expect_moved:
            assert after.state == CLOSED
            assert after.failure_count == 0
        else:
            assert after.state == before.state
            assert after.failure_count == before.failure_count
            assert after.opened_at == before.opened_at
            assert after.updated_at == before.updated_at

    @pytest.mark.parametrize("keep_open", [True, False], ids=["guarded", "unguarded"])
    def test_absent_row_returns_false(self, repo, keep_open):
        """Boundary: nothing stored -> nothing written, under either directive."""
        result = repo.update_state(
            service_name=SERVICE, state=CLOSED, failure_count=0, keep_open=keep_open
        )

        assert result is False
        assert repo.get_by_service_name(SERVICE) is None

    def test_guarded_open_write_onto_a_closed_row_still_writes(self, repo):
        """The guard reads the *stored* state: an OPEN write is never declined."""
        _store(repo, CLOSED)

        result = repo.update_state(
            service_name=SERVICE, state=OPEN, opened_at=utc_now(), keep_open=True
        )

        assert result is True
        assert repo.get_by_service_name(SERVICE).state == OPEN

    def test_guard_is_decided_in_the_same_lock_hold_as_the_pin_guard(self, repo):
        """A pinned OPEN row declines through either guard; the row is untouched."""
        repo.hydrate_snapshot(
            CircuitBreakerStateData(
                service_name=SERVICE,
                state=OPEN,
                failure_count=5,
                opened_at=utc_now(),
                manually_controlled=True,
                manual_override_expires_at=utc_now() + timedelta(minutes=10),
            )
        )
        before = repo.get_by_service_name(SERVICE)

        assert (
            repo.update_state(
                service_name=SERVICE, state=CLOSED, failure_count=0, keep_open=True
            )
            is True
        )
        assert (
            repo.update_state(
                service_name=SERVICE,
                state=CLOSED,
                failure_count=0,
                skip_if_pinned=True,
                keep_open=False,
            )
            is True
        )

        assert repo.get_by_service_name(SERVICE) == before
