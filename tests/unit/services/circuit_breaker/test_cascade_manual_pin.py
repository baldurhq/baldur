"""The automatic 429 cascade yields to an operator's live manual pin.

Every sibling automatic path already did: the record paths skip on the pin,
recovery transitions filter it out, the expiry sweep leaves a due lift to
admission. The 429 cascade force-open did not — so it could replace an
operator's Allow with a Block, or restamp a live Block with a TTL nobody typed.
That was an asymmetry, not a design.

The suppression emits both halves of the sibling observe-only branch's
signature: the fixed-field ``INTERVENTION_EVALUATED`` decision record and an
in-band WARNING naming the service and the measured rate.

Accepted consequence, asserted here rather than left implicit: while an Allow
is pinned, traffic keeps flowing to a 429-ing dependency for the pin's
lifetime — bounded by its TTL and by the operator's explicit instruction.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import structlog

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.core.decision_logger import DecisionBoundaryEventType, ReasonCode
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.rate_limit_tracker import (
    reset_rate_limit_tracker,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.utils.time import utc_now

SERVICE = "payment-api"

# Small enough to drive in a loop, and consistent with each other: five 429s
# out of five calls is 100%, over both the absolute floor and the rate.
CASCADE_THRESHOLD = 5
CASCADE_MINIMUM_CALLS = 5


@pytest.fixture(autouse=True)
def _isolated_tracker():
    """The 429 tracker is a process singleton; a leaked window skews the rate."""
    reset_rate_limit_tracker()
    yield
    reset_rate_limit_tracker()


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


@pytest.fixture
def service(repo) -> CircuitBreakerService:
    return CircuitBreakerService(
        config=CircuitBreakerConfig(
            enabled=True,
            rate_limit_cascade_threshold=CASCADE_THRESHOLD,
            rate_limit_cascade_minimum_calls=CASCADE_MINIMUM_CALLS,
            rate_limit_cascade_rate=10.0,
            rate_limit_cascade_window_seconds=60,
        ),
        repository=repo,
    )


def _pin(repo, state: str) -> None:
    repo.set_manual_control(
        SERVICE,
        state=state,
        reason="operator decision",
        expires_at=utc_now() + timedelta(minutes=90),
    )


def _drive_cascade(service) -> list:
    """Report enough 429s to cross the threshold; return every result."""
    return [
        service.record_rate_limit_response(SERVICE)
        for _ in range(CASCADE_THRESHOLD + 1)
    ]


def _decision_records(logs: list[dict]) -> list[dict]:
    """Parse the fixed-field JSON decision records out of captured logs."""
    out = []
    for entry in logs:
        event = entry.get("event")
        if isinstance(event, str) and event.startswith("{"):
            try:
                out.append(json.loads(event))
            except ValueError:
                continue
    return out


class TestCascadeRespectsManualPinBehavior:
    """Pre-fix red run: with the pin gate removed, the pinned row comes back
    OPEN with a restamped expiry and ``force_open`` fires."""

    def test_the_cascade_still_trips_an_unpinned_service(self, service, repo):
        """Positive control, first: the drive genuinely reaches the cascade.

        Without this the suppression tests could pass by never detecting a
        cascade at all — the classic vacuous negative.
        """
        results = _drive_cascade(service)

        assert any(r is not None and r.success for r in results)
        assert repo.get_by_service_name(SERVICE).state == "open"

    @pytest.mark.parametrize("pinned_state", ["closed", "open"])
    def test_cascade_respects_manual_pin_in_both_directions(
        self, service, repo, pinned_state
    ):
        """Both pin directions: an Allow is not replaced, a Block not restamped."""
        _pin(repo, pinned_state)
        before = repo.get_by_service_name(SERVICE)

        results = _drive_cascade(service)

        after = repo.get_by_service_name(SERVICE)
        assert all(r is None for r in results)
        assert after.state == pinned_state
        assert after.manually_controlled is True
        assert after.manual_override_expires_at == before.manual_override_expires_at

    def test_cascade_respects_manual_pin_and_never_calls_force_open(
        self, service, repo
    ):
        """Interaction assertion — the suppression is upstream of the write.

        The gate sits at this call site and not inside ``force_open``, so the
        manual force path stays live; this pins that placement.
        """
        _pin(repo, "closed")
        calls: list[str] = []
        service.force_open = lambda *args, **kwargs: calls.append("force_open")

        _drive_cascade(service)

        assert calls == []

    def test_cascade_respects_manual_pin_with_no_open_state_write(self, service, repo):
        """Negative assertion at the repository boundary, not the service one.

        Both write shapes are watched. The force path writes OPEN through
        ``atomic_force_open``, so a test that only recorded ``update_state``
        would pass against the unguarded cascade — the assertion has to cover
        every way an OPEN can be written, not the one that came to mind.
        """
        _pin(repo, "closed")
        writes: list[str] = []

        original_update = repo.update_state
        original_force = repo.atomic_force_open

        def _recording_update_state(service_name, state, **kwargs):
            writes.append(str(state))
            return original_update(service_name, state, **kwargs)

        def _recording_force_open(*args, **kwargs):
            writes.append("open")
            return original_force(*args, **kwargs)

        repo.update_state = _recording_update_state
        repo.atomic_force_open = _recording_force_open

        _drive_cascade(service)

        assert "open" not in writes

    def test_cascade_respects_manual_pin_and_emits_the_decision_record(
        self, service, repo
    ):
        """Half one of the sibling branch's signature."""
        _pin(repo, "closed")

        with structlog.testing.capture_logs() as logs:
            _drive_cascade(service)

        evaluated = [
            r
            for r in _decision_records(logs)
            if r.get("event") == DecisionBoundaryEventType.INTERVENTION_EVALUATED.value
        ]
        assert evaluated
        record = evaluated[0]
        assert record["allowed"] is False
        assert record["reason"] == ReasonCode.POLICY_CONSTRAINT_ACTIVE.value
        assert record["service_name"] == SERVICE

    def test_cascade_respects_manual_pin_and_emits_the_in_band_warning(
        self, service, repo
    ):
        """Half two: the operator sees why a detected cascade did not act.

        The measured numbers travel with it, so the log answers "how close was
        it" without a second query.
        """
        _pin(repo, "closed")

        with structlog.testing.capture_logs() as logs:
            _drive_cascade(service)

        blocked = [
            entry
            for entry in logs
            if entry.get("event") == "circuit_breaker.rate_limit_force_open_blocked"
        ]
        assert blocked
        assert blocked[0]["log_level"] == "warning"
        assert blocked[0]["service_name"] == SERVICE
        assert blocked[0]["rate_limit_count"] >= CASCADE_THRESHOLD
        assert blocked[0]["window_seconds"] == 60

    def test_cascade_ignores_a_lapsed_manual_pin(self, service, repo):
        """The gate reads the predicate, not the raw flag.

        A row whose override lifetime has passed is not an operator decision
        any more, and the automatic protection must come back on its own.
        """
        repo.set_manual_control(
            SERVICE,
            state="closed",
            reason="lapsed",
            expires_at=utc_now() - timedelta(minutes=1),
        )

        results = _drive_cascade(service)

        assert any(r is not None and r.success for r in results)
        assert repo.get_by_service_name(SERVICE).state == "open"
