"""741 — one manual Block, from the operator's click to automatic protection.

The pieces of the fix live in four modules and only make sense together: the
service resolves and stores a lifetime, the admission path reads it on every
request, the repository's atomic primitive owns the state transition when the
lift is taken, and the sweep clears the flag afterwards. Each has unit tests;
what none of them can show is that the *sequence* is right — that a block holds
for exactly the lifetime it promised and then hands the circuit back to the
ordinary recovery machinery rather than to an unbounded bypass.

Mock-based integration: the real service over the real in-memory repository,
with the clock advanced rather than slept through. No infra.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"
TTL_MINUTES = 5
RECOVERY_TIMEOUT_SECONDS = 600

# Every clock the lifecycle reads. Advancing only some of them would let a
# transition stamp a timestamp in a different era from the one the predicates
# compare it against, and the test would prove nothing about ordering.
_CLOCKS = (
    "baldur.adapters.memory.circuit_breaker._now",
    "baldur.interfaces.repositories.utc_now",
    "baldur.services.circuit_breaker.manual_control.utc_now",
    "baldur.services.circuit_breaker.service.utc_now",
)


@contextlib.contextmanager
def _at(instant: datetime):
    """Run the block with every lifecycle clock reading ``instant``."""
    with contextlib.ExitStack() as stack:
        for target in _CLOCKS:
            stack.enter_context(patch(target, return_value=instant))
        yield


@pytest.fixture
def service():
    repository = InMemoryCircuitBreakerStateRepository()
    cb = CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            recovery_timeout=RECOVERY_TIMEOUT_SECONDS,
            half_open_max_calls=3,
        ),
        repository=repository,
    )
    with patch(
        "baldur.services.circuit_breaker.manual_control._is_system_enabled",
        return_value=True,
    ):
        yield cb


class TestManualBlockLifecycle:
    """Block -> hold -> lift -> trial -> ordinary recovery -> flag cleared."""

    def test_block_holds_for_its_lifetime_then_returns_to_automatic_recovery(
        self, service
    ):
        """
        Purpose:
            One manual Block, end to end: stored lifetime, zero admissions
            while it holds, the promised lift, and the hand-back to ordinary
            recovery — the sequence no single unit test can show.
        Expected:
            - stored expiry equals the typed TTL and the reported expiry
            - zero admissions until the lift instant, one trial after it
            - a failed trial re-opens under the normal recovery gate
            - the sweep clears the flag without touching state
        """
        repository = service.repository
        blocked_at = utc_now()

        # 1. The operator blocks the service for five minutes.
        with _at(blocked_at):
            result = service.force_open(
                SERVICE, reason="dependency incident", ttl_minutes=TTL_MINUTES
            )
        stored = repository.get_by_service_name(SERVICE)
        assert result.success is True
        assert result.expires_at == stored.manual_override_expires_at
        assert stored.manual_override_expires_at == blocked_at + timedelta(
            minutes=TTL_MINUTES
        )

        # 2. Nothing gets through while it holds — including at the four-minute
        #    mark, long past the point where an unpinned OPEN would be probing.
        for offset in (0, 60, 240):
            with _at(blocked_at + timedelta(seconds=offset)):
                assert service.should_allow(SERVICE) is False
        held = repository.get_by_service_name(SERVICE)
        assert held.state == CircuitBreakerStateEnum.OPEN.value
        assert held.half_open_request_count == 0

        # 3. At the promised lift instant traffic resumes — one trial request,
        #    with no scheduler having run and despite a ten-minute recovery
        #    timeout that would otherwise still be counting.
        lift_at = blocked_at + timedelta(minutes=TTL_MINUTES, seconds=1)
        with _at(lift_at):
            assert service.should_allow(SERVICE) is True
        lifted = repository.get_by_service_name(SERVICE)
        assert lifted.state == CircuitBreakerStateEnum.HALF_OPEN.value

        # 4. The trial fails: the breaker re-opens on its own evidence, and the
        #    next request waits out recovery_timeout like any automatic OPEN.
        with _at(lift_at):
            service.record_failure(SERVICE)
            assert service.should_allow(SERVICE) is False
        reopened = repository.get_by_service_name(SERVICE)
        assert reopened.state == CircuitBreakerStateEnum.OPEN.value
        assert reopened.opened_at > reopened.manual_override_expires_at

        # 5. The sweep clears the stale flag without touching the state it
        #    found — the breaker is protecting for real reasons now.
        with _at(lift_at + timedelta(minutes=1)):
            expired = service.check_and_expire_manual_overrides()
        swept = repository.get_by_service_name(SERVICE)
        assert expired == [SERVICE]
        assert swept.manually_controlled is False
        assert swept.state == CircuitBreakerStateEnum.OPEN.value

        # 6. Once the recovery timeout elapses the ordinary probe resumes.
        with _at(lift_at + timedelta(seconds=RECOVERY_TIMEOUT_SECONDS + 1)):
            assert service.should_allow(SERVICE) is True

    def test_block_admits_nothing_across_a_full_half_open_window(self, service):
        """The leak, stated as a count: it used to be three per window.

        ``half_open_max_calls`` requests were admitted per stuck-window for as
        long as the block stayed in place, so a long enough drive is what
        distinguishes "blocks" from "throttles".
        """
        repository = service.repository
        blocked_at = utc_now()
        with _at(blocked_at):
            service.force_open(SERVICE, reason="incident", ttl_minutes=60)

        admitted = 0
        for minute in range(1, 31):
            with _at(blocked_at + timedelta(minutes=minute)):
                if service.should_allow(SERVICE):
                    admitted += 1

        assert admitted == 0
        assert repository.get_by_service_name(SERVICE).state == (
            CircuitBreakerStateEnum.OPEN.value
        )


class TestManualAllowLifecycle:
    """A force-allow suspends protection, so it has to expire too."""

    def test_allow_expires_and_automatic_protection_resumes(self, service):
        """
        Purpose:
            A force-allow (force_close) is a suspension of protection, so its
            expiry must hand the breaker back to automatic supervision.
        Expected:
            - failures are ignored while the allow holds
            - past the lifetime, ``record_failure`` counts again with no sweep
            - the sweep clears the flag and the circuit stays CLOSED
        """
        repository = service.repository
        allowed_at = utc_now()

        # 1. The operator forces the circuit closed for five minutes.
        with _at(allowed_at):
            result = service.force_close(
                SERVICE, reason="known-good deploy", ttl_minutes=TTL_MINUTES
            )
        assert result.success is True
        assert result.expires_at == allowed_at + timedelta(minutes=TTL_MINUTES)

        # 2. While it holds, failures are not counted — that is the point of it.
        with _at(allowed_at + timedelta(minutes=1)):
            for _ in range(10):
                service.record_failure(SERVICE)
        assert repository.get_by_service_name(SERVICE).failure_count == 0

        # 3. Past the lifetime, supervision is back without any sweep running.
        lapsed_at = allowed_at + timedelta(minutes=TTL_MINUTES, seconds=1)
        with _at(lapsed_at):
            service.record_failure(SERVICE)
        assert repository.get_by_service_name(SERVICE).failure_count == 1

        # 4. The sweep clears the flag and leaves the circuit CLOSED — the
        #    expiry of an allow is not a state transition.
        with _at(lapsed_at):
            expired = service.check_and_expire_manual_overrides()
        row = repository.get_by_service_name(SERVICE)
        assert expired == [SERVICE]
        assert row.manually_controlled is False
        assert row.state == CircuitBreakerStateEnum.CLOSED.value
