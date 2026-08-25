"""741 D7 — the admission path honours a manual override's lifetime.

Before this, ``_evaluate_admission`` never read the manual-control flag: a
pinned OPEN fell through to the trial path once ``recovery_timeout`` elapsed
and admitted ``half_open_max_calls`` requests per stuck-window indefinitely,
while ``record_failure`` / ``record_success`` skipped the pinned circuit so it
could neither close nor re-open. A manual Block was a leaky throttle.

The OPEN branch now splits three ways, and each way has its own failure mode:

- **live pin** — rejects, and must keep rejecting past ``recovery_timeout``
  (the leak that closed).
- **lift due** — the operator's own block at its promised lift instant goes
  straight to the trial, *bypassing* the recovery gate: ``opened_at`` is the
  moment they blocked, so applying the gate there would hold a five-minute
  block for the whole of a long ``recovery_timeout``.
- **stale flag** — an automatic OPEN written after the expiry takes the normal
  gate. Nothing clears ``manually_controlled`` on an automatic transition, so
  applying the bypass here instead would admit one request per request against
  a dependency that is still down — strictly worse than the original leak.

Driven through the real in-memory repository, since the branch's whole point
is which rows reach ``try_acquire_half_open_slot``.
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


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _service(repo, **config_overrides) -> CircuitBreakerService:
    config = CircuitBreakerConfig(enabled=True, **config_overrides)
    return CircuitBreakerService(config=config, repository=repo)


def _seed(repo, **fields) -> CircuitBreakerStateData:
    """Write a state row directly — the rows under test are already aged."""
    state = CircuitBreakerStateData(id=1, service_name=SERVICE, **fields)
    repo._storage[SERVICE] = state
    return state


# =============================================================================
# The live pin — zero admissions while the block holds
# =============================================================================


class TestPinnedAdmission:
    """The three-way OPEN branch, by row shape."""

    def test_live_pinned_block_admission_rejects_after_recovery_timeout(self, repo):
        """The leak: a Block used to start admitting again after 60 seconds.

        Negative assertion — the atomic primitive is never reached, so the row
        stays OPEN with an untouched half-open counter rather than transitioning
        and admitting ``half_open_max_calls`` requests per window.
        """
        # Given: a block set 10 minutes ago, still 80 minutes from its expiry,
        # on a breaker whose recovery timeout is a single minute.
        service = _service(repo, recovery_timeout=60, half_open_max_calls=3)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=utc_now() - timedelta(minutes=10),
            manual_override_expires_at=utc_now() + timedelta(minutes=80),
        )

        # When: requests keep arriving well past recovery_timeout.
        decisions = [service.should_allow(SERVICE) for _ in range(5)]

        # Then: none of them got through, and no trial window was opened.
        row = repo.get_by_service_name(SERVICE)
        assert decisions == [False] * 5
        assert row.state == CircuitBreakerStateEnum.OPEN.value
        assert row.half_open_request_count == 0

    def test_live_pinned_block_admission_tags_the_reject_reason_open(self, repo):
        """No new metric label — a manual block reports as an open circuit."""
        service = _service(repo, recovery_timeout=60)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=utc_now() - timedelta(minutes=10),
            manual_override_expires_at=utc_now() + timedelta(minutes=80),
        )

        with patch(
            "baldur.services.circuit_breaker.service.record_blocked"
        ) as mock_record:
            allowed = service.should_allow(SERVICE)

        assert allowed is False
        mock_record.assert_called_once_with(SERVICE, "open")

    def test_pinned_block_with_no_stored_expiry_admission_never_lifts(self, repo):
        """A pin with no lifetime blocks until an operator clears it."""
        service = _service(repo, recovery_timeout=60)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=utc_now() - timedelta(days=7),
            manual_override_expires_at=None,
        )

        assert service.should_allow(SERVICE) is False
        assert repo.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.OPEN.value
        )

    def test_pinned_closed_circuit_admission_is_allowed(self, repo):
        """A force-close pin is irrelevant to admission — CLOSED lets traffic by."""
        service = _service(repo)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=utc_now() + timedelta(minutes=80),
        )

        assert service.should_allow(SERVICE) is True

    def test_unpinned_open_admission_still_waits_for_the_recovery_timeout(self, repo):
        """Regression baseline: the automatic path is untouched by the pin branch."""
        service = _service(repo, recovery_timeout=600)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=False,
            opened_at=utc_now() - timedelta(seconds=30),
        )

        assert service.should_allow(SERVICE) is False
        assert repo.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.OPEN.value
        )


# =============================================================================
# The lift — enforcement ends at the promised instant, on every worker
# =============================================================================


class TestExpiredPinAdmission:
    """Expiry is enforced per read, so no scheduler has to have run."""

    def test_expired_pin_admission_proceeds_to_the_trial_with_no_sweep(self, repo):
        """No background pass ran here — the predicate alone lifts enforcement."""
        service = _service(repo, recovery_timeout=60, half_open_max_calls=3)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=expires_at - timedelta(minutes=5),
            manual_override_expires_at=expires_at,
        )

        allowed = service.should_allow(SERVICE)

        assert allowed is True
        assert repo.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.HALF_OPEN.value
        )

    def test_expired_pin_admission_ignores_a_recovery_timeout_longer_than_the_ttl(
        self, repo
    ):
        """A 5-minute Block must not be held for a 30-minute recovery timeout.

        ``opened_at`` is the moment the operator blocked, so the recovery gate
        would measure the wait from there and keep the circuit shut for 25
        minutes past the lift time the console promised.
        """
        # Given: recovery_timeout 1800s, a 5-minute block, 6 minutes elapsed.
        service = _service(repo, recovery_timeout=1800)
        blocked_at = utc_now() - timedelta(minutes=6)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=blocked_at,
            manual_override_expires_at=blocked_at + timedelta(minutes=5),
        )

        # When / Then: the promised lift wins over the recovery gate.
        assert service.should_allow(SERVICE) is True
        assert repo.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.HALF_OPEN.value
        )

    def test_expired_pin_admission_resumes_failure_recording(self, repo):
        """Automatic supervision comes back at the expiry, not at the sweep."""
        service = _service(repo, recovery_timeout=60)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
        )

        service.record_failure(SERVICE)

        assert repo.get_by_service_name(SERVICE).failure_count == 1


# =============================================================================
# The stale flag — one trial, then the normal recovery backoff
# =============================================================================


class TestStalePinRespectsRecovery:
    """The bypass fires once per pin, never once per request."""

    def test_stale_pin_respects_recovery_gate_after_the_trial_fails(self, repo):
        """Lifted block -> one trial -> failure -> back to the ordinary wait.

        Without the ``opened_at`` discriminator the flag survives the revert
        and every following request would skip the gate, admitting traffic
        one-for-one against a dependency that is still down.
        """
        # Given: a block whose TTL has passed.
        service = _service(repo, recovery_timeout=600, half_open_max_calls=3)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            opened_at=expires_at - timedelta(minutes=5),
            manual_override_expires_at=expires_at,
        )

        # When: the trial request is admitted and then fails.
        assert service.should_allow(SERVICE) is True
        service.record_failure(SERVICE)

        # Then: the row is OPEN again with a fresh opened_at, the stale flag
        # is still set — and the next request waits out recovery_timeout.
        reverted = repo.get_by_service_name(SERVICE)
        assert reverted.state == CircuitBreakerStateEnum.OPEN.value
        assert reverted.manually_controlled is True
        assert reverted.opened_at > reverted.manual_override_expires_at
        assert service.should_allow(SERVICE) is False

    def test_stale_pin_respects_recovery_gate_on_the_open_after_a_lapsed_allow(
        self, repo
    ):
        """The same path reached from an expired Allow rather than a Block.

        Automatic protection correctly trips the breaker once the force-close
        lapses. The trip primitive clears the lapsed flag in the same write, so
        the row rejoins the readers that still filter on it; what must hold is
        that the fresh OPEN protects.
        """
        # Given: a force-closed circuit whose override has lapsed.
        service = _service(repo, recovery_timeout=600, failure_threshold=2)
        expires_at = utc_now() - timedelta(minutes=1)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.CLOSED.value,
            manually_controlled=True,
            manual_override_expires_at=expires_at,
        )

        # When: the dependency keeps failing and the breaker trips.
        service.record_failure(SERVICE)
        service.record_failure(SERVICE)

        # Then: it is OPEN, the lapsed flag is gone — and it protects.
        tripped = repo.get_by_service_name(SERVICE)
        assert tripped.state == CircuitBreakerStateEnum.OPEN.value
        assert tripped.manually_controlled is False
        assert tripped.manual_override_expires_at is None
        assert service.should_allow(SERVICE) is False

    def test_stale_pin_respects_recovery_gate_only_until_the_timeout_elapses(
        self, repo
    ):
        """The gate applies, it does not lock the row out permanently."""
        service = _service(repo, recovery_timeout=60)
        expires_at = utc_now() - timedelta(minutes=10)
        _seed(
            repo,
            state=CircuitBreakerStateEnum.OPEN.value,
            manually_controlled=True,
            # Written after the expiry (stale flag) and long enough ago that
            # the ordinary recovery timeout has run out.
            opened_at=expires_at + timedelta(minutes=5),
            manual_override_expires_at=expires_at,
        )

        assert service.should_allow(SERVICE) is True
