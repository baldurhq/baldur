"""
Outbound 429 observation — the per-call scope and the fan-out.

Test target: services/circuit_breaker/rate_limit_observation.py
- OutboundObservationScope bookkeeping (attempts, 429s, identity marks, claim)
- open_scope / close_scope / current_scope lifecycle and context visibility
- observe_429() fan-out: two independent, independently fail-open halves
"""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from unittest.mock import MagicMock, patch

import pytest

from baldur.services.circuit_breaker.rate_limit_observation import (
    OutboundObservationScope,
    close_scope,
    current_scope,
    observe_429,
    open_scope,
)
from baldur.services.circuit_breaker.rate_limit_tracker import RateLimitTracker
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.rate_limit_coordinator import RateLimitCoordinator
from baldur.settings.rate_limit_backoff import RateLimitBackoffSettings

_TRACKER = "baldur.services.circuit_breaker.rate_limit_tracker.get_rate_limit_tracker"
_CB_SERVICE = "baldur.services.circuit_breaker.convenience.get_circuit_breaker_service"
_COORDINATOR = "baldur.services.rate_limit_coordinator.RateLimitCoordinator"
_BACKOFF_SETTINGS = "baldur.settings.rate_limit_backoff.get_rate_limit_backoff_settings"


@pytest.fixture
def tracker():
    """The cascade rate's denominator writer, stubbed."""
    stub = MagicMock(spec=RateLimitTracker)
    with patch(_TRACKER, return_value=stub):
        yield stub


@pytest.fixture
def cb_service():
    """The single writer of the tracker's 429 counter, stubbed."""
    stub = MagicMock(spec=CircuitBreakerService)
    with patch(_CB_SERVICE, return_value=stub):
        yield stub


@pytest.fixture
def coordinator():
    """The fleet-wide cooldown installer, stubbed, with coordination enabled."""
    stub = MagicMock(spec=RateLimitCoordinator)
    settings = MagicMock(spec=RateLimitBackoffSettings)
    settings.coordination_enabled = True
    with patch(_COORDINATOR) as coordinator_cls:
        coordinator_cls.get_instance.return_value = stub
        with patch(_BACKOFF_SETTINGS, return_value=settings):
            yield stub


# =============================================================================
# Behavior Tests — OutboundObservationScope bookkeeping
# =============================================================================


class TestOutboundObservationScopeBehavior:
    """The per-call record every inner stage mutates in place."""

    def test_a_fresh_scope_counts_nothing(self):
        """State at construction: no attempt, no 429, no claim, nothing classified."""
        scope = OutboundObservationScope("payment")

        assert scope.breaker_key == "payment"
        assert scope.attempts == 0
        assert scope.rate_limited == 0
        assert scope.coordination_claimed is False
        assert scope.classified == []

    def test_note_attempt_counts_the_call_and_writes_the_denominator(self, tracker):
        """One attempt is one request on the cascade rate's denominator.

        The breaker stage reads ``attempts == 0`` to decide whether it owes the
        request write itself, so a counter that advanced without the tracker
        write would leave the rate's denominator permanently at zero.
        """
        scope = OutboundObservationScope("payment")

        scope.note_attempt()

        assert scope.attempts == 1
        tracker.record_request.assert_called_once_with("payment")

    def test_note_attempt_writes_one_request_per_call(self, tracker):
        """Three attempts are three requests, not one."""
        scope = OutboundObservationScope("payment")

        for _ in range(3):
            scope.note_attempt()

        assert scope.attempts == 3
        assert tracker.record_request.call_count == 3

    def test_note_429_records_the_cascade_but_installs_no_cooldown(
        self, cb_service, coordinator
    ):
        """A stage calling this owns its own coordinator decision.

        ``note_429`` is the cascade half alone; installing a cooldown here too
        would double the fleet-wide effect of a single 429 for every stage that
        already notifies its own coordinator.
        """
        scope = OutboundObservationScope("payment")

        scope.note_429(retry_after=30.0)

        assert scope.rate_limited == 1
        cb_service.record_rate_limit_response.assert_called_once_with("payment")
        coordinator.on_rate_limited.assert_not_called()

    def test_note_429_writes_no_request_of_its_own(self, cb_service, tracker):
        """The 429 and the call that produced it are counted separately.

        Counting the request here as well would put the same call in the
        denominator twice, capping the observed rate at 50% in a pure storm.
        """
        scope = OutboundObservationScope("payment")

        scope.note_429()

        assert scope.attempts == 0
        tracker.record_request.assert_not_called()

    def test_was_classified_matches_by_identity_not_equality(self):
        """Identity is the mark: the composer re-raises and returns by identity.

        An equality match would suppress the breaker stage's classification of a
        *different* outcome that merely compares equal — two distinct 429
        responses from two attempts, for instance.
        """

        class Response:
            def __eq__(self, other):
                return isinstance(other, Response)

            def __hash__(self):
                return 0

        marked = Response()
        equal_but_distinct = Response()
        scope = OutboundObservationScope("payment")

        scope.mark_classified(marked)

        assert marked == equal_but_distinct
        assert scope.was_classified(marked) is True
        assert scope.was_classified(equal_but_distinct) is False

    def test_every_marked_outcome_is_kept_not_only_the_last(self):
        """A single slot would lose the first of two concurrent siblings' marks."""
        first = Exception("first")
        second = Exception("second")
        scope = OutboundObservationScope("payment")

        scope.mark_classified(first)
        scope.mark_classified(second)

        assert scope.was_classified(first) is True
        assert scope.was_classified(second) is True
        assert len(scope.classified) == 2

    def test_an_unmarked_outcome_is_not_classified(self):
        """The breaker stage's gate must open for an outcome no stage saw."""
        scope = OutboundObservationScope("payment")
        scope.mark_classified(Exception("seen"))

        assert scope.was_classified(Exception("unseen")) is False

    def test_a_repeated_claim_leaves_the_call_claimed_once(self):
        """Idempotent: two stages claiming the same call is not an error state.

        The flag is read as a boolean gate by the breaker stage, so a claim that
        toggled or counted would let the second claimant re-open coordination
        the first one closed.
        """
        scope = OutboundObservationScope("payment")

        scope.claim_coordination()
        scope.claim_coordination()

        assert scope.coordination_claimed is True

    def test_a_tracker_fault_does_not_reach_the_business_call(self):
        """Fail-open: a broken tracker must not break the call it is observing."""
        scope = OutboundObservationScope("payment")

        with patch(_TRACKER, side_effect=RuntimeError("tracker down")):
            scope.note_attempt()

        assert scope.attempts == 1


# =============================================================================
# Behavior Tests — scope lifecycle and context visibility
# =============================================================================


class TestObservationScopeLifecycleBehavior:
    """open_scope / close_scope / current_scope across context boundaries."""

    def test_no_open_scope_reads_none(self):
        """A bare inner stage counts nothing: there is no breaker to trip."""
        assert current_scope() is None

    def test_open_scope_publishes_a_scope_for_the_key(self):
        """The published scope is the one returned, keyed by the protected name."""
        token, scope = open_scope("payment")
        try:
            assert current_scope() is scope
            assert scope.breaker_key == "payment"
        finally:
            close_scope(token)

    def test_close_scope_restores_the_absent_scope(self):
        """A closed scope leaves nothing behind for the next call on this thread."""
        token, _ = open_scope("payment")
        close_scope(token)

        assert current_scope() is None

    def test_a_nested_scope_restores_the_outer_one_untouched(self):
        """A nested protected call gets its own record and never steals the outer.

        Without the token reset the inner call's scope would stay current for
        the rest of the outer call, filing the outer call's own 429s under the
        inner breaker's key.
        """
        outer_token, outer = open_scope("outer")
        try:
            inner_token, inner = open_scope("inner")
            inner.attempts = 7
            close_scope(inner_token)

            assert current_scope() is outer
            assert outer.attempts == 0
            assert inner is not outer
        finally:
            close_scope(outer_token)

    def test_an_in_place_mutation_inside_a_copied_context_reaches_the_opener(self):
        """This is why inner stages mutate rather than ``set``.

        A timeout stage runs the inner chain under a copied context. The record
        object is shared across the copy, so counting on it is visible to the
        frame that opened the scope.
        """
        token, scope = open_scope("payment")
        try:

            def inner_stage():
                current_scope().attempts += 1

            copy_context().run(inner_stage)

            assert scope.attempts == 1
        finally:
            close_scope(token)

    def test_a_set_inside_a_copied_context_is_invisible_to_the_opener(self):
        """The negative half: a replacement scope never reaches the breaker frame.

        Pins the constraint the in-place rule exists for — an inner stage that
        published its own scope would have every count silently discarded at the
        copied context's boundary.
        """
        token, scope = open_scope("payment")
        try:

            def inner_stage():
                replacement_token, replacement = open_scope("payment")
                replacement.attempts += 1
                close_scope(replacement_token)

            copy_context().run(inner_stage)

            assert scope.attempts == 0
        finally:
            close_scope(token)

    def test_the_scope_is_visible_from_a_worker_thread_the_context_reaches(self):
        """``asyncio.to_thread`` copies the context, so an async inner stage counts.

        The async breaker path runs the business call this way; a scope invisible
        there would leave every async 429 uncounted.
        """

        async def drive():
            token, scope = open_scope("payment")
            try:
                await asyncio.to_thread(lambda: current_scope().note_429())
                return scope.rate_limited
            finally:
                close_scope(token)

        with patch(_CB_SERVICE, return_value=MagicMock(spec=CircuitBreakerService)):
            assert asyncio.run(drive()) == 1


# =============================================================================
# Behavior Tests — observe_429 fan-out
# =============================================================================


class TestObserve429FanOutBehavior:
    """One observed 429, two independent side effects."""

    def test_both_halves_run_when_both_are_requested(self, cb_service, coordinator):
        """The cascade counter and the fleet-wide cooldown, each once."""
        observe_429("payment", 30.0, record_cascade=True, notify_coordinator=True)

        cb_service.record_rate_limit_response.assert_called_once_with("payment")
        coordinator.on_rate_limited.assert_called_once_with(
            key="payment", retry_after=30.0
        )

    def test_the_coordinator_half_is_off_by_default(self, cb_service, coordinator):
        """Default: cascade only. A stage that owns coordination asks for it."""
        observe_429("payment")

        cb_service.record_rate_limit_response.assert_called_once_with("payment")
        coordinator.on_rate_limited.assert_not_called()

    def test_record_cascade_false_skips_the_cascade_half(self, cb_service, coordinator):
        """An outcome an inner stage already counted is not counted again here."""
        observe_429("payment", None, record_cascade=False, notify_coordinator=True)

        cb_service.record_rate_limit_response.assert_not_called()
        coordinator.on_rate_limited.assert_called_once()

    def test_neither_half_runs_when_neither_is_requested(self, cb_service, coordinator):
        """Both gates off is a no-op, not a half-observation."""
        observe_429("payment", None, record_cascade=False, notify_coordinator=False)

        cb_service.record_rate_limit_response.assert_not_called()
        coordinator.on_rate_limited.assert_not_called()

    def test_a_cascade_fault_still_lets_the_cooldown_be_installed(self, coordinator):
        """Independence: neither half is chained through the other.

        Chaining them would let a breaker-service fault silently drop every
        fleet-wide cooldown, so a storm would keep every worker calling.
        """
        with patch(_CB_SERVICE, side_effect=RuntimeError("breaker service down")):
            observe_429("payment", 30.0, notify_coordinator=True)

        coordinator.on_rate_limited.assert_called_once()

    def test_a_coordinator_fault_still_lets_the_cascade_be_recorded(self, cb_service):
        """The other direction of the same independence."""
        settings = MagicMock(spec=RateLimitBackoffSettings)
        settings.coordination_enabled = True
        with (
            patch(_BACKOFF_SETTINGS, return_value=settings),
            patch(_COORDINATOR, side_effect=RuntimeError("coordinator down")),
        ):
            observe_429("payment", 30.0, notify_coordinator=True)

        cb_service.record_rate_limit_response.assert_called_once_with("payment")

    def test_a_fault_in_both_halves_never_reaches_the_business_call(self):
        """Fail-open end to end: observation is never worth failing a call for."""
        settings = MagicMock(spec=RateLimitBackoffSettings)
        settings.coordination_enabled = True
        with (
            patch(_CB_SERVICE, side_effect=RuntimeError("breaker service down")),
            patch(_BACKOFF_SETTINGS, return_value=settings),
            patch(_COORDINATOR, side_effect=RuntimeError("coordinator down")),
        ):
            observe_429("payment", 30.0, notify_coordinator=True)

    def test_the_kill_switch_suppresses_the_cooldown_but_not_the_cascade(
        self, cb_service
    ):
        """The deployment switch governs default coordination, not observation.

        An operator who turns fleet-wide coordination off still wants the
        breaker to see the storm.
        """
        settings = MagicMock(spec=RateLimitBackoffSettings)
        settings.coordination_enabled = False
        coordinator_stub = MagicMock(spec=RateLimitCoordinator)

        with (
            patch(_BACKOFF_SETTINGS, return_value=settings),
            patch(_COORDINATOR) as coordinator_cls,
        ):
            coordinator_cls.get_instance.return_value = coordinator_stub
            observe_429("payment", 30.0, notify_coordinator=True)

        cb_service.record_rate_limit_response.assert_called_once_with("payment")
        coordinator_stub.on_rate_limited.assert_not_called()

    def test_the_placeholder_key_gets_no_fleet_wide_cooldown(
        self, cb_service, coordinator
    ):
        """An unidentified downstream would share one cooldown record with all others.

        The cascade half still runs: the breaker key is local to this process,
        so it carries no cross-service confusion.
        """
        from baldur.services.retry_handler.rate_limit_detection import (
            UNIDENTIFIED_COORDINATION_KEY,
        )

        observe_429(UNIDENTIFIED_COORDINATION_KEY, 30.0, notify_coordinator=True)

        cb_service.record_rate_limit_response.assert_called_once_with(
            UNIDENTIFIED_COORDINATION_KEY
        )
        coordinator.on_rate_limited.assert_not_called()

    def test_an_absent_retry_after_is_forwarded_as_none(self, coordinator):
        """No provider-stated wait leaves the coordinator on its own ladder."""
        with patch(_CB_SERVICE, return_value=MagicMock(spec=CircuitBreakerService)):
            observe_429("payment", None, notify_coordinator=True)

        assert coordinator.on_rate_limited.call_args.kwargs["retry_after"] is None
