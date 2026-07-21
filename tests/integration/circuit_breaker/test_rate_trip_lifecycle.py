"""Failure-rate trip lifecycle integration tests (719).

A trip is one logical transaction spanning three collaborators that share
state, and no component expresses the invariant alone:

    1. ``CircuitBreakerService.record_failure`` reads the repository's
       consecutive-failure counter and this worker's ``OutcomeWindow``, then on
       a trip performs a repository ``update_state`` **and** a window clear that
       must agree — a repository left OPEN beside a populated window would
       re-trip the breaker the moment it recovers.
    2. ``ManualControlMixin`` (``force_open`` / ``force_close`` / ``reset``)
       clears a window the service owns, so the two mixins have to be exercised
       against the same live instance.
    3. ``CircuitBreakerEvaluator`` replays the enriched ``CIRCUIT_BREAKER_OPENED``
       payload the live service emits. Parity is the point: the evaluator imports
       the same ``evaluate_trip``, so a shadow simulation must not predict a trip
       the live breaker would not perform.

Mock-based — no infra. The repository is the real in-memory implementation, so
the counter semantics under test are the shipped ones rather than a stub's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.event_journal import JournalEntry
from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.outcome_window import (
    TRIP_REASON_COUNT,
    TRIP_REASON_RATE,
    evaluate_trip,
)
from baldur.services.circuit_breaker.service import CircuitBreakerService
from baldur.services.config_shadow.evaluators.circuit_breaker import (
    CircuitBreakerEvaluator,
)
from baldur.services.config_shadow.models import EvaluationContext
from baldur.services.event_bus import EventType

SERVICE = "checkout-api"


def _config(**overrides) -> CircuitBreakerConfig:
    base = {
        "enabled": True,
        "failure_threshold": 5,
        "success_threshold": 2,
        "failure_rate_threshold": 50.0,
        "sliding_window_size": 100,
        "minimum_calls": 10,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


class _LiveBreaker:
    """A service over a real repository, with emitted events captured.

    The audit write and the burn-rate multiplier reach outside the circuit
    breaker; everything else — repository, window, trip predicate, event
    payload — is the production path.
    """

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self.repository = InMemoryCircuitBreakerStateRepository()
        self.service = CircuitBreakerService(config=config, repository=self.repository)
        self.events: list[tuple[str, dict]] = []

    def _capture(self, event_type, data=None, **kwargs):
        self.events.append((event_type, data or kwargs.get("data") or {}))

    def drive(self, pattern: str) -> None:
        """Replay a call sequence: ``f`` is a failure, ``s`` a success."""
        with (
            patch.object(self.service, "_log_circuit_open_audit"),
            patch.object(self.service, "_apply_burn_rate_multiplier"),
            patch.object(self.service, "_emit_event", side_effect=self._capture),
        ):
            for outcome in pattern:
                if outcome == "f":
                    self.service.record_failure(SERVICE)
                else:
                    self.service.record_success(SERVICE)

    def opened_payload(self) -> dict:
        """The data of the last CIRCUIT_BREAKER_OPENED event emitted."""
        opened = [
            data
            for event_type, data in self.events
            if event_type == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert opened, "no CIRCUIT_BREAKER_OPENED event was emitted"
        return opened[-1]


def _interleave(total: int, failures: int) -> str:
    """Spread failures evenly so the count trigger never masks the rate one."""
    pattern = []
    emitted = 0
    for index in range(total):
        due = (index + 1) * failures // total
        if due > emitted:
            pattern.append("f")
            emitted = due
        else:
            pattern.append("s")
    return "".join(pattern)


def _journal_entry(payload: dict, timestamp: datetime) -> JournalEntry:
    """Build the journal row the subscriber would persist for an event.

    The subscriber stores ``event.data`` verbatim as the entry context, so the
    enriched denominators reach replay without further wiring.
    """
    return JournalEntry(
        sequence=0,
        event_type=EventType.CIRCUIT_BREAKER_OPENED.value,
        source="circuit_breaker_service",
        timestamp=timestamp,
        service_name=payload.get("service_name", SERVICE),
        context=payload,
    )


# =============================================================================
# Repository and window agree across the trip transaction
# =============================================================================


class TestRateTripLifecycleIntegration:
    """The trip writes the repository and clears the window as one step."""

    def test_rate_trip_opens_the_repository_state_and_empties_the_window(self):
        """Both halves of the trip land: state OPEN, window cleared.

        A trip that opened the repository but left the window populated would
        re-trip the breaker on its first post-recovery failure, because the
        stale evidence still reads above the threshold.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000))

        breaker.drive(_interleave(total=40, failures=24))

        assert breaker.repository.get_by_service_name(SERVICE).state == "open"
        assert breaker.service.get_window_evidence(SERVICE) == (0, 0)

    def test_opened_event_carries_the_denominators_the_decision_used(self):
        """The emitted payload reports the exact evidence, for replay.

        Without the enrichment the journal holds no denominators, and the
        evaluator falls back to counting one failure per event — which cannot
        reproduce a rate-driven trip at all.

        ``minimum_calls`` is raised to the full sequence length so the trip
        lands on a known window: the rate gate only becomes reachable on the
        40th call, which the interleaving makes a failure.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000, minimum_calls=40))

        breaker.drive(_interleave(total=40, failures=24))
        payload = breaker.opened_payload()

        assert payload["window_total_calls"] == 40
        assert payload["window_failure_count"] == 24
        assert payload["trigger"] == "auto"
        assert payload["previous_state"] == "closed"
        # The rate that opened the circuit clears the configured threshold.
        rate = payload["window_failure_count"] / payload["window_total_calls"] * 100
        assert rate >= breaker.service.config.failure_rate_threshold

    def test_opened_event_reports_the_consecutive_count_separately(self):
        """The count evidence travels alongside the window evidence.

        A count-driven trip and a rate-driven trip produce different payloads;
        replay needs both to tell them apart.
        """
        breaker = _LiveBreaker(_config(failure_threshold=5))

        breaker.drive("fffff")
        payload = breaker.opened_payload()

        assert payload["consecutive_failure_count"] == 5
        assert payload["window_failure_count"] == 5
        assert payload["window_total_calls"] == 5

    def test_snapshot_reports_window_evidence_not_repository_counters(self):
        """The audit snapshot's rate is sourced from the window.

        The snapshot used to publish ``failure_count + success_count`` from the
        repository — the same fictitious denominator the rate trigger had —
        so an incident review would have read a ~100% failure rate for a
        service failing 60% of its calls.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000, minimum_calls=40))
        captured: dict = {}

        original = breaker.service._collect_failure_snapshot

        def _capture_snapshot(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            captured.update(snapshot)
            return snapshot

        with patch.object(
            breaker.service, "_collect_failure_snapshot", side_effect=_capture_snapshot
        ):
            breaker.drive(_interleave(total=40, failures=24))

        circuit_breaker = captured["circuit_breaker"]
        assert circuit_breaker["window_total_calls"] == 40
        assert circuit_breaker["window_failure_count"] == 24
        assert circuit_breaker["failure_rate_percent"] == pytest.approx(60.0)

    def test_recovery_re_arms_the_breaker_with_fresh_evidence(self):
        """Full cycle: trip, recover, and the next closed period starts empty.

        Given/When/Then across the whole lifecycle — the invariant that ties
        the repository state to the window is what makes the second closed
        period behave like the first.
        """
        # Given: a rate trip has opened the circuit
        breaker = _LiveBreaker(_config(failure_threshold=1000, success_threshold=2))
        breaker.drive(_interleave(total=40, failures=24))
        assert breaker.service.get_state(SERVICE) == "open"

        # When: the breaker moves to HALF_OPEN and the trials succeed
        breaker.repository.update_state(service_name=SERVICE, state="half_open")
        breaker.drive("ss")

        # Then: it is closed again, with no evidence carried over
        assert breaker.service.get_state(SERVICE) == "closed"
        assert breaker.service.get_window_evidence(SERVICE) == (0, 0)

        # And: healthy traffic in the new period does not re-trip it
        breaker.drive("s" * 20)
        assert breaker.service.get_state(SERVICE) == "closed"
        assert breaker.service.get_window_evidence(SERVICE) == (0, 20)

    def test_aggregate_rate_tracks_the_live_windows_across_services(self):
        """The system-wide gate reads the same evidence the trips decided on."""
        breaker = _LiveBreaker(_config(failure_threshold=1000))

        breaker.drive("f" * 3 + "s" * 7)
        with (
            patch.object(breaker.service, "_log_circuit_open_audit"),
            patch.object(breaker.service, "_apply_burn_rate_multiplier"),
        ):
            for _ in range(10):
                breaker.service.record_success("healthy-peer")

        # 3 failures over 20 observed calls, not the near-binary ~1.0 the
        # repository-counter source produced.
        assert breaker.service.get_aggregate_failure_rate() == pytest.approx(0.15)


# =============================================================================
# Manual control clears a window owned by the service (719 D9)
# =============================================================================


class TestManualControlWindowIntegration:
    """``ManualControlMixin`` and the rate evidence stay consistent."""

    @staticmethod
    def _system_enabled():
        return patch(
            "baldur.services.circuit_breaker.manual_control._is_system_enabled",
            return_value=True,
        )

    # 4 failures over 10 calls — 40%, and every failure lands while the window
    # still holds fewer than ``minimum_calls``, so the breaker never trips on
    # its own and the evidence survives to the manual-control call.
    BELOW_THRESHOLD_TRAFFIC = "ssfsfsfsfs"

    def test_force_open_then_force_close_starts_a_clean_period(self):
        """An operator cycling the breaker does not inherit stale evidence.

        The counterfactual is concrete: carrying the 4/10 window over, the
        four failures the breaker sees once it is back under automatic control
        would reach 8 failures in 14 calls — 57%, past both the 50% threshold
        and the 10-call gate — so it would trip straight back open on traffic
        that is only 4 calls deep.

        ``force_close`` leaves the breaker manually controlled, which suspends
        recording; the manual flag is released so the post-cycle traffic is
        observed the way ordinary automatic traffic would be.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000, minimum_calls=10))
        breaker.drive(self.BELOW_THRESHOLD_TRAFFIC)
        assert breaker.service.get_window_evidence(SERVICE) == (4, 10)

        with self._system_enabled():
            assert breaker.service.force_open(SERVICE, reason="incident").success
            assert breaker.service.get_window_evidence(SERVICE) == (0, 0)
            assert breaker.service.force_close(SERVICE, reason="resolved").success

        assert breaker.service.get_window_evidence(SERVICE) == (0, 0)

        breaker.repository.clear_manual_control(SERVICE)
        breaker.drive("ffff")

        assert breaker.service.get_state(SERVICE) == "closed"
        assert breaker.service.get_window_evidence(SERVICE) == (4, 4)

    def test_reset_clears_both_the_counters_and_the_window(self):
        """``reset`` returns the breaker to a genuinely initial state."""
        breaker = _LiveBreaker(_config(failure_threshold=1000, minimum_calls=10))
        breaker.drive(self.BELOW_THRESHOLD_TRAFFIC)
        assert breaker.service.get_window_evidence(SERVICE) == (4, 10)

        with self._system_enabled():
            assert breaker.service.reset(SERVICE, reason="operator reset").success

        state = breaker.repository.get_by_service_name(SERVICE)
        assert state.failure_count == 0
        assert breaker.service.get_window_evidence(SERVICE) == (0, 0)

    def test_manual_control_suspends_recording(self):
        """While manually controlled, calls contribute no rate evidence.

        A forced-open breaker is not observing the dependency, so anything
        recorded during that period would describe the breaker's own refusal.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000))

        with self._system_enabled():
            breaker.service.force_open(SERVICE, reason="maintenance")

        breaker.drive("fffffssss")

        assert breaker.service.get_window_evidence(SERVICE) == (0, 0)


# =============================================================================
# Evaluator replays the live trip and agrees with it
# =============================================================================


class TestEvaluatorLiveParityIntegration:
    """One synthesized enriched journal, two consumers, one verdict.

    D2 makes agreement true by construction — the evaluator imports the live
    predicate. What is genuinely integration-shaped, and what these tests pin,
    is the payload contract between them: the live service must emit exactly
    the keys the evaluator replays from.
    """

    @staticmethod
    def _evaluate(entries: list[JournalEntry], config: dict) -> int:
        result = CircuitBreakerEvaluator().evaluate(
            EvaluationContext(
                baseline_config=config,
                candidate_config=config,
                events=entries,
            )
        )
        return result.baseline_metrics["open_count"]

    def test_evaluator_reproduces_a_live_rate_trip(self):
        """A trip the live breaker performed is predicted on replay."""
        # Given: a live rate-driven trip and the payload it emitted
        breaker = _LiveBreaker(_config(failure_threshold=1000))
        breaker.drive(_interleave(total=40, failures=24))
        assert breaker.service.get_state(SERVICE) == "open"
        entry = _journal_entry(
            breaker.opened_payload(), datetime(2026, 7, 22, tzinfo=UTC)
        )

        # When: the journal is replayed under the same configuration
        open_count = self._evaluate(
            [entry], {"failure_threshold": 1000, "failure_rate_threshold": 50.0}
        )

        # Then: the shadow simulation opens the circuit too
        assert open_count == 1

    def test_evaluator_does_not_predict_a_trip_the_breaker_withheld(self):
        """Below-threshold evidence replays as no trip, matching the live run.

        The old evaluator held failures only, so its window read 100% for any
        non-empty event stream — it predicted trips the live breaker never
        performed, precisely when an operator was validating a config change.
        """
        # Given: evidence the live breaker did not trip on
        breaker = _LiveBreaker(_config(failure_threshold=1000))
        breaker.drive(_interleave(total=40, failures=12))
        assert breaker.service.get_state(SERVICE) == "closed"
        window_failures, window_total = breaker.service.get_window_evidence(SERVICE)

        # When: that same evidence is replayed as an event
        entry = _journal_entry(
            {
                "service_name": SERVICE,
                "trigger": "auto",
                "previous_state": "closed",
                "window_failure_count": window_failures,
                "window_total_calls": window_total,
                "consecutive_failure_count": 1,
            },
            datetime(2026, 7, 22, tzinfo=UTC),
        )
        open_count = self._evaluate(
            [entry], {"failure_threshold": 1000, "failure_rate_threshold": 50.0}
        )

        # Then: the simulation stays closed, as the live breaker did
        assert open_count == 0

    def test_evaluator_and_live_predicate_agree_on_the_replayed_evidence(self):
        """The predicate called directly on the payload returns the live reason.

        Pins that the emitted keys map onto ``evaluate_trip``'s parameters —
        a renamed or dropped key would break replay silently, since the
        evaluator falls back to legacy seeding when a key is missing.
        """
        breaker = _LiveBreaker(_config(failure_threshold=1000))
        breaker.drive(_interleave(total=40, failures=24))
        payload = breaker.opened_payload()

        reason = evaluate_trip(
            consecutive_failures=payload["consecutive_failure_count"],
            window_failures=payload["window_failure_count"],
            window_total=payload["window_total_calls"],
            config=breaker.service.config,
        )

        assert reason == TRIP_REASON_RATE

    def test_count_driven_trip_replays_as_a_count_trip(self):
        """A consecutive-failure trip keeps its reason through the payload."""
        breaker = _LiveBreaker(_config(failure_threshold=5))
        breaker.drive("fffff")
        payload = breaker.opened_payload()

        reason = evaluate_trip(
            consecutive_failures=payload["consecutive_failure_count"],
            window_failures=payload["window_failure_count"],
            window_total=payload["window_total_calls"],
            config=breaker.service.config,
        )

        # 5 of 5 failures is 100%, but only 5 observations — below the
        # minimum_calls gate — so the count trigger is what fires.
        assert reason == TRIP_REASON_COUNT

    def test_legacy_event_without_denominators_still_replays(self):
        """Pre-enrichment journal rows degrade to one failure per event.

        Graceful degradation: an operator replaying an old journal gets an
        approximate answer covered by the confidence warnings, not a crash.
        """
        entry = _journal_entry(
            {
                "service_name": SERVICE,
                "trigger": "auto",
                "previous_state": "closed",
            },
            datetime(2026, 7, 22, tzinfo=UTC),
        )

        open_count = self._evaluate(
            [entry], {"failure_threshold": 1, "failure_rate_threshold": 0.0}
        )

        assert open_count == 1
