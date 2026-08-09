"""Neither record path decides from a hint that may predate an operator's pin.

``should_allow_with_state`` hands ``record_success`` / ``record_failure`` the
state row it already loaded. That hint used to be *adopted* whenever its
service name matched, which made it a substitute for the read — and a hint
taken at admission time can predate a Block taken while the protected call was
still in flight.

The counterexample that made this live: the CLOSED branch then wrote the pinned
row back to CLOSED. Admission short-circuits on CLOSED, so every later request
was admitted; every later record call read the (now fresh) pin and skipped, so
nothing ever restored the OPEN state. One stale hint disabled the block for the
rest of its lifetime.

The hint survives as a fast-path gate only: a steady-state CLOSED hint still
returns without touching the repository at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def service(repo) -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(enabled=True, failure_threshold=3),
        repository=repo,
    )


def _block(repo, state: str = CircuitBreakerStateEnum.OPEN.value) -> None:
    """Pin the row the way an operator's force does — after the hint was taken."""
    repo.set_manual_control(
        SERVICE,
        state=state,
        reason="incident",
        expires_at=utc_now() + timedelta(minutes=90),
    )


# =============================================================================
# D9 — the stale hint cannot authorize a write or answer the pin check
# =============================================================================


class TestStaleHintPinImmunityBehavior:
    """A hint taken before the Block, replayed after it.

    Pre-fix red run: every test in this class fails with the hint-adoption
    branch (``if hint_state is not None and hint_state.service_name == ...``)
    restored on the slow paths — the pinned row comes back CLOSED and
    unpinned-in-effect.
    """

    def test_stale_hint_pin_survives_record_success(self, service, repo):
        """The counterexample: the CLOSED branch used to un-open the block."""
        # Given: a request is admitted on a CLOSED row carrying failures, so
        # the caller's hint is CLOSED with failure_count > 0.
        repo.get_or_create(SERVICE)
        repo.record_failure(SERVICE)
        hint = repo.get_by_service_name(SERVICE)
        assert hint.state == CircuitBreakerStateEnum.CLOSED.value
        assert hint.failure_count > 0

        # And: the operator blocks the service while that call is in flight.
        _block(repo)

        # When: the in-flight call succeeds and reports with its stale hint.
        service.record_success(SERVICE, hint_state=hint)

        # Then: the operator's row is untouched — still OPEN, still pinned.
        after = repo.get_by_service_name(SERVICE)
        assert after.state == CircuitBreakerStateEnum.OPEN.value
        assert after.manually_controlled is True

    def test_stale_hint_pin_survives_record_failure(self, service, repo):
        """The mirror direction: an Allow must not accumulate failures either.

        Negative assertion — the failure counter does not move, so the pinned
        CLOSED row cannot be tripped by traffic the operator chose to admit.
        """
        # Given: an admitted request on a clean CLOSED row.
        hint = repo.get_or_create(SERVICE)

        # And: the operator pins the service open (an "allow") mid-flight.
        _block(repo, state=CircuitBreakerStateEnum.CLOSED.value)
        before = repo.get_by_service_name(SERVICE)

        # When: the in-flight call fails and reports with its stale hint.
        service.record_failure(SERVICE, hint_state=hint)

        # Then: nothing was recorded against the pinned row.
        after = repo.get_by_service_name(SERVICE)
        assert after.failure_count == before.failure_count
        assert after.state == CircuitBreakerStateEnum.CLOSED.value
        assert after.manually_controlled is True

    def test_stale_hint_pin_check_runs_on_a_fresh_read(self, service, repo):
        """The read is what makes the pin visible — assert it happens.

        Interaction assertion rather than an outcome one: without the read the
        two tests above could pass for the wrong reason (e.g. a branch that
        happens not to write for this particular hint shape).
        """
        hint = repo.get_or_create(SERVICE)
        repo.record_failure(SERVICE)
        hint = repo.get_by_service_name(SERVICE)
        reads: list[str] = []
        original = repo.get_or_create

        def _counting_get_or_create(service_name: str):
            reads.append(service_name)
            return original(service_name)

        repo.get_or_create = _counting_get_or_create

        service.record_success(SERVICE, hint_state=hint)

        assert reads == [SERVICE]

    def test_steady_state_closed_hint_still_skips_the_repository_entirely(
        self, service, repo
    ):
        """The fast path survives: a clean CLOSED hint does zero repository I/O.

        This is the whole reason the hint still exists. If the D9 fix had
        simply deleted it, the CLOSED steady state would pay a repository
        acquire on every successful call.
        """
        # Given: a hint that is fresh, CLOSED, unpinned and has no failures.
        hint = repo.get_or_create(SERVICE)
        touched: list[str] = []
        for name in ("get_or_create", "update_state", "record_success"):
            original = getattr(repo, name)

            def _tracked(*args, _name=name, _original=original, **kwargs):
                touched.append(_name)
                return _original(*args, **kwargs)

            setattr(repo, name, _tracked)

        # When
        service.record_success(SERVICE, hint_state=hint)

        # Then: not one repository method was called.
        assert touched == []
