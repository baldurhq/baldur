"""Unit tests for services/circuit_breaker/outcome_window.py.

The rate trigger's evidence and the shared trip predicate. This file replaces
the deleted memory-adapter ring-buffer suite: the mechanism moved from the
repository to the service, so the coverage moves with it.

Verification techniques applied:
- Contract: outcome encoding, trip-reason strings, ``__all__``
- State transition: record -> read, clear -> no evidence
- Boundary analysis: eviction at ``maxlen``, the rate gate's ``>=``,
  ``minimum_calls`` at and below the window total
- Concurrency: appends racing clears and resizes (every thread joined)
- Property-based: ``0 <= sum <= len <= maxlen`` over arbitrary op sequences,
  and the 719 D1 resize-preservation invariant
"""

from __future__ import annotations

import threading

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from baldur.services.circuit_breaker.config import CircuitBreakerConfig
from baldur.services.circuit_breaker.outcome_window import (
    FAILURE_OUTCOME,
    SUCCESS_OUTCOME,
    TRIP_REASON_COUNT,
    TRIP_REASON_RATE,
    OutcomeWindow,
    evaluate_trip,
)

WINDOW_SIZE = 10


def _fill(window: OutcomeWindow, service: str, failures: int, successes: int) -> None:
    """Record the given outcome counts, failures first."""
    for _ in range(failures):
        window.record_failure(service, WINDOW_SIZE)
    for _ in range(successes):
        window.record_success(service, WINDOW_SIZE)


def _config(**overrides) -> CircuitBreakerConfig:
    """Build a config with the trip-relevant fields explicitly set."""
    base = {
        "enabled": True,
        "failure_threshold": 5,
        "failure_rate_threshold": 50.0,
        "sliding_window_size": 100,
        "minimum_calls": 10,
    }
    base.update(overrides)
    return CircuitBreakerConfig(**base)


# =============================================================================
# Contract — the encoding the window and its readers agree on
# =============================================================================


class TestOutcomeWindowContract:
    """Values other modules depend on literally.

    The window sums to a failure count, which only holds while a failure is 1
    and a success is 0. The evaluator seeds its replay window from these same
    constants, so a flip here silently inverts every shadow simulation.
    """

    def test_failure_outcome_is_one(self):
        """A failure contributes 1, making sum(window) the failure count."""
        assert FAILURE_OUTCOME == 1

    def test_success_outcome_is_zero(self):
        """A success contributes 0, so it enlarges the denominator only."""
        assert SUCCESS_OUTCOME == 0

    def test_trip_reason_rate_value(self):
        """The rate reason string is logged and asserted on directly."""
        assert TRIP_REASON_RATE == "failure_rate_threshold_exceeded"

    def test_trip_reason_count_value(self):
        """The count reason string is logged and asserted on directly."""
        assert TRIP_REASON_COUNT == "failure_threshold_exceeded"

    def test_module_exports_the_public_surface(self):
        """``__all__`` declares the predicate, window, and both encodings."""
        from baldur.services.circuit_breaker import outcome_window

        assert set(outcome_window.__all__) == {
            "FAILURE_OUTCOME",
            "SUCCESS_OUTCOME",
            "TRIP_REASON_COUNT",
            "TRIP_REASON_RATE",
            "OutcomeWindow",
            "evaluate_trip",
        }


# =============================================================================
# Behavior — recording, reading, eviction, clearing
# =============================================================================


class TestOutcomeWindowBehavior:
    """Recording and reading the evidence for one service name."""

    def test_read_of_untracked_service_reports_no_evidence(self):
        """An unseen service reads (0, 0) — no evidence, not a 0% rate.

        The caller must be able to tell "nothing observed" apart from "observed
        and all healthy"; the rate trigger skips the former.
        """
        window = OutcomeWindow()

        assert window.read("never-called") == (0, 0)

    def test_recorded_failures_and_successes_form_the_ratio(self):
        """Failures count toward the numerator, successes only the denominator."""
        window = OutcomeWindow()

        _fill(window, "svc", failures=3, successes=5)

        assert window.read("svc") == (3, 8)

    def test_success_only_traffic_reads_zero_failures(self):
        """A healthy service has a real denominator and a zero numerator."""
        window = OutcomeWindow()

        _fill(window, "svc", failures=0, successes=4)

        assert window.read("svc") == (0, 4)

    def test_window_evicts_oldest_outcomes_beyond_the_size(self):
        """Recording past ``maxlen`` drops the oldest, keeping the total capped.

        Given/When/Then: the first WINDOW_SIZE failures are pushed out by an
        equal number of later successes, so the window reports a clean service
        rather than the historical failure burst.
        """
        # Given: the window is full of failures
        window = OutcomeWindow()
        _fill(window, "svc", failures=WINDOW_SIZE, successes=0)
        assert window.read("svc") == (WINDOW_SIZE, WINDOW_SIZE)

        # When: an equal run of successes arrives
        _fill(window, "svc", failures=0, successes=WINDOW_SIZE)

        # Then: every failure has been evicted, and the total stays at the cap
        assert window.read("svc") == (0, WINDOW_SIZE)

    def test_eviction_boundary_keeps_total_at_the_window_size(self):
        """One outcome past the cap evicts exactly one — the total does not grow."""
        window = OutcomeWindow()

        _fill(window, "svc", failures=WINDOW_SIZE, successes=0)
        at_cap = window.read("svc")
        window.record_success("svc", WINDOW_SIZE)

        assert at_cap == (WINDOW_SIZE, WINDOW_SIZE)
        assert window.read("svc") == (WINDOW_SIZE - 1, WINDOW_SIZE)

    def test_evidence_is_isolated_per_service_name(self):
        """One service's failures never enter another's denominator."""
        window = OutcomeWindow()

        _fill(window, "failing", failures=4, successes=0)
        _fill(window, "healthy", failures=0, successes=4)

        assert window.read("failing") == (4, 4)
        assert window.read("healthy") == (0, 4)

    def test_clear_drops_the_evidence_for_one_service_only(self):
        """A clear resets the cleared service and leaves its peers intact."""
        window = OutcomeWindow()
        _fill(window, "tripped", failures=6, successes=2)
        _fill(window, "peer", failures=1, successes=3)

        window.clear("tripped")

        assert window.read("tripped") == (0, 0)
        assert window.read("peer") == (1, 4)

    def test_clear_of_untracked_service_is_a_no_op(self):
        """Clearing a name that was never recorded does not raise or create it."""
        window = OutcomeWindow()

        window.clear("never-called")

        assert window.read("never-called") == (0, 0)

    def test_recording_after_clear_starts_a_fresh_window(self):
        """A cleared window accumulates again from zero."""
        window = OutcomeWindow()
        _fill(window, "svc", failures=5, successes=5)
        window.clear("svc")

        window.record_failure("svc", WINDOW_SIZE)

        assert window.read("svc") == (1, 1)

    def test_read_all_sums_every_tracked_service(self):
        """The aggregate read totals the numerators and denominators."""
        window = OutcomeWindow()
        _fill(window, "a", failures=1, successes=4)
        _fill(window, "b", failures=2, successes=3)

        assert window.read_all() == (3, 10)

    def test_read_all_with_no_services_reports_no_evidence(self):
        """An empty window aggregates to (0, 0), the caller's 0.0 rate case."""
        assert OutcomeWindow().read_all() == (0, 0)


# =============================================================================
# Behavior — resize preserves evidence (719 D1)
# =============================================================================


class TestOutcomeWindowResizeBehavior:
    """A window-size change rebuilds the ring without blanking it.

    Rebuilding empty would suspend rate-trigger protection for the next
    ``sliding_window_size`` calls — reintroducing the very gap the outcome
    window closes. These tests are what falsifies a rebuild-empty implementation.
    """

    def test_growing_the_window_preserves_every_outcome(self):
        """A larger size keeps all existing outcomes."""
        window = OutcomeWindow()
        for _ in range(4):
            window.record_failure("svc", 5)

        window.record_success("svc", 50)

        assert window.read("svc") == (4, 5)

    def test_shrinking_the_window_keeps_the_most_recent_outcomes(self):
        """A smaller size truncates the oldest, retaining the rightmost entries."""
        # Given: five failures then five successes, in that order
        window = OutcomeWindow()
        for _ in range(5):
            window.record_failure("svc", 10)
        for _ in range(5):
            window.record_success("svc", 10)

        # When: the window shrinks to three
        window.record_success("svc", 3)

        # Then: only the newest outcomes survive — all successes, no failures
        assert window.read("svc") == (0, 3)

    def test_unchanged_window_size_does_not_rebuild(self):
        """Recording at the same size leaves the accumulated evidence alone."""
        window = OutcomeWindow()
        _fill(window, "svc", failures=3, successes=3)

        window.record_failure("svc", WINDOW_SIZE)

        assert window.read("svc") == (4, 7)

    def test_resize_never_empties_a_populated_window(self):
        """Any resize to a positive size leaves evidence behind."""
        window = OutcomeWindow()
        _fill(window, "svc", failures=4, successes=4)

        for new_size in (1, 3, 8, 200):
            window.record_success("svc", new_size)
            _failures, total = window.read("svc")
            assert total > 0, f"resize to {new_size} blanked the window"

    def test_zero_window_size_records_no_evidence(self):
        """A size of zero yields an unbounded-drop ring, so nothing accumulates."""
        window = OutcomeWindow()

        for _ in range(5):
            window.record_failure("svc", 0)

        assert window.read("svc") == (0, 0)

    def test_negative_window_size_is_clamped_to_zero(self):
        """A negative size is floored rather than raising on ``deque(maxlen=-1)``."""
        window = OutcomeWindow()

        window.record_failure("svc", -5)

        assert window.read("svc") == (0, 0)


# =============================================================================
# Behavior — thread safety (719 SC: window concurrency invariant)
# =============================================================================


class TestOutcomeWindowThreadSafety:
    """Appends racing clears and resizes leave no torn state.

    The single lock is the only thing standing between the hot-path append and
    a concurrent rebuild. Every spawned thread is joined before asserting — a
    leaked daemon thread crashes xdist workers.
    """

    def test_concurrent_appends_lose_no_outcomes(self):
        """With no eviction in reach, every recorded outcome is observable."""
        window = OutcomeWindow()
        n_threads = 8
        ops_per_thread = 50
        capacity = n_threads * ops_per_thread

        def worker(index: int) -> None:
            for _ in range(ops_per_thread):
                if index % 2 == 0:
                    window.record_failure("svc", capacity)
                else:
                    window.record_success("svc", capacity)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        failures, total = window.read("svc")
        assert total == capacity
        assert failures == capacity // 2

    def test_appends_racing_clears_and_resizes_hold_the_invariant(self):
        """Under contention the window still satisfies 0 <= sum <= len <= W."""
        # Given: writers appending while other threads clear and resize
        window = OutcomeWindow()
        errors: list[BaseException] = []
        max_size = 32
        stop = threading.Event()

        def appender(index: int) -> None:
            try:
                for _ in range(200):
                    if index % 2 == 0:
                        window.record_failure("svc", max_size)
                    else:
                        window.record_success("svc", max_size)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def clearer() -> None:
            try:
                while not stop.is_set():
                    window.clear("svc")
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def resizer() -> None:
            try:
                while not stop.is_set():
                    for size in (4, max_size):
                        window.record_success("svc", size)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        # When: all of them run together, then every thread is joined
        appenders = [threading.Thread(target=appender, args=(i,)) for i in range(6)]
        helpers = [threading.Thread(target=clearer), threading.Thread(target=resizer)]
        for thread in appenders + helpers:
            thread.start()
        for thread in appenders:
            thread.join()
        stop.set()
        for thread in helpers:
            thread.join()

        # Then: no thread raised, and the evidence is internally consistent
        assert errors == []
        failures, total = window.read("svc")
        assert 0 <= failures <= total <= max_size

    def test_read_all_under_concurrent_appends_stays_consistent(self):
        """The aggregate read never reports more failures than calls."""
        window = OutcomeWindow()
        errors: list[BaseException] = []
        observations: list[tuple[int, int]] = []
        stop = threading.Event()

        def appender(name: str) -> None:
            try:
                for index in range(300):
                    if index % 3 == 0:
                        window.record_failure(name, 64)
                    else:
                        window.record_success(name, 64)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def reader() -> None:
            try:
                while not stop.is_set():
                    observations.append(window.read_all())
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        writers = [
            threading.Thread(target=appender, args=(f"svc-{i}",)) for i in range(4)
        ]
        reader_thread = threading.Thread(target=reader)
        for thread in [*writers, reader_thread]:
            thread.start()
        for thread in writers:
            thread.join()
        stop.set()
        reader_thread.join()

        assert errors == []
        assert observations, "the reader thread produced no observations"
        assert all(0 <= failures <= total for failures, total in observations)


# =============================================================================
# Property-based — invariants over arbitrary operation sequences
# =============================================================================

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("failure"), st.integers(min_value=0, max_value=32)),
        st.tuples(st.just("success"), st.integers(min_value=0, max_value=32)),
        st.tuples(st.just("clear"), st.just(0)),
    ),
    max_size=60,
)


class TestOutcomeWindowProperties:
    """Invariants that must hold no matter how the window is driven."""

    @given(operations=_OPERATIONS)
    @hyp_settings(max_examples=100, deadline=None)
    def test_window_invariant_holds_over_arbitrary_sequences(self, operations):
        """0 <= failures <= total <= the most recent window size, always."""
        window = OutcomeWindow()
        last_size = 0

        for kind, size in operations:
            if kind == "clear":
                window.clear("svc")
                continue
            last_size = max(size, 0)
            if kind == "failure":
                window.record_failure("svc", size)
            else:
                window.record_success("svc", size)

            failures, total = window.read("svc")
            assert 0 <= failures <= total <= last_size

    @given(
        failures=st.integers(min_value=0, max_value=20),
        successes=st.integers(min_value=0, max_value=20),
        new_size=st.integers(min_value=1, max_value=40),
    )
    @hyp_settings(max_examples=100, deadline=None)
    def test_resize_preserves_the_most_recent_outcomes(
        self, failures, successes, new_size
    ):
        """A resize keeps ``min(recorded, new_size)`` outcomes — never zero.

        This is where a rebuild-empty implementation of ``_resolve`` gets
        falsified: with any prior evidence and a positive new size, the window
        must still report a non-empty total.
        """
        window = OutcomeWindow()
        original_size = 40
        for _ in range(failures):
            window.record_failure("svc", original_size)
        for _ in range(successes):
            window.record_success("svc", original_size)
        recorded = min(failures + successes, original_size)

        # A record at the new size triggers the rebuild and adds one outcome.
        window.record_success("svc", new_size)

        _observed_failures, total = window.read("svc")
        assert total == min(recorded + 1, new_size)


# =============================================================================
# Behavior — the shared trip predicate (719 D2/D4)
# =============================================================================


class TestEvaluateTripBehavior:
    """``evaluate_trip`` is the single trip model for the live breaker and the
    config-shadow evaluator alike."""

    @pytest.mark.parametrize(
        ("consecutive", "window_failures", "window_total", "expected"),
        [
            (0, 0, 0, None),
            (0, 3, 10, None),
            (0, 5, 10, TRIP_REASON_RATE),
            (0, 6, 10, TRIP_REASON_RATE),
            (0, 9, 9, None),
            (4, 0, 0, None),
            (5, 0, 0, TRIP_REASON_COUNT),
            (6, 1, 20, TRIP_REASON_COUNT),
            (5, 5, 10, TRIP_REASON_RATE),
        ],
        ids=[
            "no_evidence",
            "rate_below_threshold",
            "rate_at_threshold",
            "rate_above_threshold",
            "rate_high_but_below_minimum_calls",
            "consecutive_below_threshold",
            "consecutive_at_threshold",
            "consecutive_above_threshold_low_rate",
            "both_triggers_fire",
        ],
    )
    def test_trip_matrix_over_evidence_combinations(
        self, consecutive, window_failures, window_total, expected
    ):
        """The two OR'd triggers resolve to one reason, or to no trip."""
        config = _config(failure_threshold=5, failure_rate_threshold=50.0)

        assert (
            evaluate_trip(
                consecutive_failures=consecutive,
                window_failures=window_failures,
                window_total=window_total,
                config=config,
            )
            == expected
        )

    @pytest.mark.parametrize(
        ("window_failures", "expected"),
        [(4, None), (5, TRIP_REASON_RATE), (6, TRIP_REASON_RATE)],
        ids=["below_boundary", "at_boundary", "above_boundary"],
    )
    def test_rate_gate_is_inclusive_at_the_threshold(self, window_failures, expected):
        """The rate gate is ``>=``: exactly the threshold trips.

        A ``>=`` to ``>`` drift still trips at 60% and hides, yet holds a
        failing dependency open one tick too long at exactly 50%.
        """
        config = _config(failure_threshold=100, failure_rate_threshold=50.0)

        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=window_failures,
                window_total=10,
                config=config,
            )
            == expected
        )

    @pytest.mark.parametrize(
        ("window_total", "expected"),
        [(9, None), (10, TRIP_REASON_RATE), (11, TRIP_REASON_RATE)],
        ids=["below_minimum", "at_minimum", "above_minimum"],
    )
    def test_minimum_calls_gate_is_inclusive(self, window_total, expected):
        """The rate trigger becomes reachable at exactly ``minimum_calls``."""
        config = _config(failure_threshold=100, minimum_calls=10)

        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=window_total,  # 100% failure concentration
                window_total=window_total,
                config=config,
            )
            == expected
        )

    def test_minimum_calls_does_not_gate_the_count_trigger(self):
        """Consecutive-failure evidence is traffic-independent (719 D4).

        Negative assertion for the behavior this change removed: the old gate
        preceded both triggers, so ``minimum_calls=10`` silently raised the
        effective trip point from ``failure_threshold`` to 10.
        """
        config = _config(failure_threshold=5, minimum_calls=10)

        assert (
            evaluate_trip(
                consecutive_failures=5,
                window_failures=5,
                window_total=5,  # well below minimum_calls
                config=config,
            )
            == TRIP_REASON_COUNT
        )

    def test_rate_trigger_is_unreachable_when_minimum_exceeds_window_size(self):
        """``minimum_calls > sliding_window_size`` disables the rate trigger.

        The window never holds that many calls, so only the count trigger can
        fire — the inert combination the settings validator warns about.
        """
        config = _config(
            failure_threshold=100,
            minimum_calls=50,
            sliding_window_size=10,
        )

        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=10,
                window_total=10,  # a full window, all failures
                config=config,
            )
            is None
        )

    def test_zero_rate_threshold_disables_the_rate_trigger(self):
        """A threshold of 0 leaves only the count trigger (preserved contract)."""
        config = _config(failure_threshold=100, failure_rate_threshold=0.0)

        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=20,
                window_total=20,
                config=config,
            )
            is None
        )

    def test_empty_window_does_not_divide_by_zero(self):
        """No observations means no rate — not a ZeroDivisionError, not 0%."""
        config = _config(failure_threshold=100, minimum_calls=0)

        assert (
            evaluate_trip(
                consecutive_failures=0,
                window_failures=0,
                window_total=0,
                config=config,
            )
            is None
        )

    def test_rate_reason_wins_when_both_triggers_fire(self):
        """The rate gate is evaluated first, so it names the reason."""
        config = _config(failure_threshold=5, failure_rate_threshold=50.0)

        assert (
            evaluate_trip(
                consecutive_failures=10,
                window_failures=10,
                window_total=10,
                config=config,
            )
            == TRIP_REASON_RATE
        )

    @pytest.mark.parametrize("window_total", [10, 20, 50], ids=["w10", "w20", "w50"])
    def test_additional_failure_never_untrips_a_tripping_input(self, window_total):
        """Monotonicity: converting a success to a failure cannot close the gate.

        Guards against an inverted comparison or a denominator that grows with
        the numerator.
        """
        config = _config(failure_threshold=1000, failure_rate_threshold=50.0)
        tripping_failures = window_total // 2

        for extra in range(window_total - tripping_failures):
            assert (
                evaluate_trip(
                    consecutive_failures=0,
                    window_failures=tripping_failures + extra,
                    window_total=window_total,
                    config=config,
                )
                == TRIP_REASON_RATE
            )
