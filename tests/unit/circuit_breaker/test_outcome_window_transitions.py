"""The outcome window's transition records: epoch, in-flight marker, last state.

793 D1/D2. Beside the ring of outcomes the window keeps three small per-name
records so a state transition is observable without a repository read:

- ``observe_state`` records the state this process just saw; a change into
  CLOSED clears the ring and moves the epoch, any other change moves the epoch
  only, and the first observation of a name records without clearing.
- ``record_success_if_epoch`` appends a hinted success only while no failure
  write is in flight and the epoch still equals the hint; ``begin_write`` /
  ``end_write`` bracket the failure write and ``bump`` moves the epoch for a
  change the window cannot see.
- ``read_each`` is the per-name snapshot the aggregate reader combines with
  repository rows; ``record_rejection`` appends a failure without moving the
  epoch.

Verification techniques applied:
- State transition: every branch of ``observe_state`` (first / same / into
  CLOSED / into non-CLOSED) asserted on the ring and the epoch
- Idempotency: the same state twice bumps nothing and takes no lock
- Boundary: epoch 0 with no write in flight is the matching hint
- Concurrency: the in-flight marker is visible across threads
- Contract: ``read_each`` sums equal ``read_all``; a rejection is a failure
"""

from __future__ import annotations

import threading

import pytest

from baldur.services.circuit_breaker.outcome_window import (
    FAILURE_OUTCOME,
    SUCCESS_OUTCOME,
    OutcomeWindow,
)

WINDOW_SIZE = 10
SERVICE = "payment-api"


@pytest.fixture
def window() -> OutcomeWindow:
    return OutcomeWindow()


def _seed(window: OutcomeWindow, service: str, failures: int, successes: int) -> None:
    """Record outcomes without moving the epoch through the failure path's bracket."""
    for _ in range(failures):
        window.record_failure(service, WINDOW_SIZE)
    for _ in range(successes):
        window.record_success(service, WINDOW_SIZE)


# =============================================================================
# Behavior — observe_state
# =============================================================================


class TestOutcomeWindowObserveStateBehavior:
    """Which observations clear the ring, and which only move the epoch."""

    def test_first_observation_records_the_state_without_clearing(self, window):
        """A name seen for the first time keeps whatever evidence it holds."""
        _seed(window, SERVICE, failures=2, successes=3)
        epoch_before = window.epoch_of(SERVICE)

        cleared = window.observe_state(SERVICE, "closed")

        assert cleared is False
        assert window.read(SERVICE) == (2, 5)
        assert window.epoch_of(SERVICE) == epoch_before

    def test_same_state_observed_again_bumps_nothing(self, window):
        """Steady state: the same state as last time is a no-op on every record."""
        window.observe_state(SERVICE, "closed")
        _seed(window, SERVICE, failures=1, successes=1)
        epoch_before = window.epoch_of(SERVICE)

        cleared = window.observe_state(SERVICE, "closed")

        assert cleared is False
        assert window.read(SERVICE) == (1, 2)
        assert window.epoch_of(SERVICE) == epoch_before

    def test_same_state_is_decided_without_taking_the_lock(self, window):
        """The CLOSED admission path pays no lock acquire on the steady state.

        The lock is held by another thread for the whole call: a steady-state
        observation that needed it would block, and the join would time out.
        """
        window.observe_state(SERVICE, "closed")
        window._lock.acquire()
        result: list[bool] = []
        try:
            worker = threading.Thread(
                target=lambda: result.append(window.observe_state(SERVICE, "closed"))
            )
            worker.start()
            worker.join(timeout=2.0)
            assert not worker.is_alive(), "steady-state observation took the lock"
        finally:
            window._lock.release()
            worker.join()

        assert result == [False]

    def test_change_into_closed_clears_the_ring_and_bumps_the_epoch(self, window):
        """Whoever closed the name, the observed re-entry opens a fresh period."""
        window.observe_state(SERVICE, "open")
        window.record_rejection(SERVICE, WINDOW_SIZE)
        window.record_rejection(SERVICE, WINDOW_SIZE)
        epoch_before = window.epoch_of(SERVICE)

        cleared = window.observe_state(SERVICE, "closed")

        assert cleared is True
        assert window.read(SERVICE) == (0, 0)
        assert window.epoch_of(SERVICE) == epoch_before + 1

    @pytest.mark.parametrize(
        ("previous", "observed"),
        [("closed", "open"), ("closed", "half_open"), ("open", "half_open")],
        ids=["closed_to_open", "closed_to_half_open", "open_to_half_open"],
    )
    def test_change_into_a_non_closed_state_bumps_only(
        self, window, previous, observed
    ):
        """A trip or a trial keeps the evidence that led to it."""
        window.observe_state(SERVICE, previous)
        _seed(window, SERVICE, failures=4, successes=1)
        epoch_before = window.epoch_of(SERVICE)

        cleared = window.observe_state(SERVICE, observed)

        assert cleared is False
        assert window.read(SERVICE) == (4, 5)
        assert window.epoch_of(SERVICE) == epoch_before + 1

    def test_change_into_closed_clears_exactly_once(self, window):
        """The second CLOSED observation is the steady state, not a second clear.

        A success admitted after the close must survive: a clear on every
        CLOSED observation would wipe it.
        """
        window.observe_state(SERVICE, "open")
        window.observe_state(SERVICE, "closed")
        window.record_success(SERVICE, WINDOW_SIZE)

        cleared = window.observe_state(SERVICE, "closed")

        assert cleared is False
        assert window.read(SERVICE) == (0, 1)

    def test_change_into_closed_on_an_untracked_ring_still_bumps(self, window):
        """A name with rows but no traffic moves its epoch on the close."""
        window.observe_state(SERVICE, "open")
        epoch_before = window.epoch_of(SERVICE)

        cleared = window.observe_state(SERVICE, "closed")

        assert cleared is True
        assert window.read(SERVICE) == (0, 0)
        assert window.epoch_of(SERVICE) == epoch_before + 1

    def test_observation_is_scoped_to_the_named_service(self, window):
        """A peer's close never clears or bumps this name."""
        window.observe_state(SERVICE, "closed")
        _seed(window, SERVICE, failures=1, successes=1)
        epoch_before = window.epoch_of(SERVICE)
        window.observe_state("peer", "open")

        window.observe_state("peer", "closed")

        assert window.read(SERVICE) == (1, 2)
        assert window.epoch_of(SERVICE) == epoch_before


# =============================================================================
# Behavior — the epoch, the in-flight marker, and the hinted append
# =============================================================================


class TestOutcomeWindowEpochBehavior:
    """When a hinted success may append read-free, and when it must not."""

    def test_fresh_name_with_epoch_zero_hint_appends(self, window):
        """Boundary: a name nothing has happened to matches a zero hint."""
        assert window.epoch_of(SERVICE) == 0

        appended = window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint_epoch=0)

        assert appended is True
        assert window.read(SERVICE) == (0, 1)

    def test_hint_taken_at_the_current_epoch_appends(self, window):
        """The hint is whatever ``epoch_of`` said at admission."""
        window.record_failure(SERVICE, WINDOW_SIZE)
        hint = window.epoch_of(SERVICE)

        appended = window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint)

        assert appended is True
        assert window.read(SERVICE) == (1, 2)

    @pytest.mark.parametrize(
        "move",
        [
            lambda w: w.record_failure(SERVICE, WINDOW_SIZE),
            lambda w: w.clear(SERVICE),
            lambda w: w.bump(SERVICE),
            lambda w: (
                w.observe_state(SERVICE, "closed"),
                w.observe_state(SERVICE, "open"),
            ),
            lambda w: (w.begin_write(SERVICE), w.end_write(SERVICE)),
        ],
        ids=["failure", "clear", "bump", "observed_transition", "write_bracket"],
    )
    def test_hint_that_predates_a_move_does_not_append(self, window, move):
        """Every epoch mover invalidates a hint taken before it."""
        hint = window.epoch_of(SERVICE)
        move(window)
        total_before = window.read(SERVICE)[1]

        appended = window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint)

        assert appended is False
        assert window.read(SERVICE)[1] == total_before

    def test_marker_held_refuses_the_append_even_at_the_matching_epoch(self, window):
        """The epoch alone cannot cover a write that has not landed."""
        hint = window.epoch_of(SERVICE)
        window.begin_write(SERVICE)

        appended = window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint)

        assert appended is False
        assert window.read(SERVICE) == (0, 0)

    def test_end_write_releases_the_marker_and_moves_the_epoch(self, window):
        """After the write lands the pre-write hint is stale, a fresh one is not."""
        stale_hint = window.epoch_of(SERVICE)
        window.begin_write(SERVICE)

        window.end_write(SERVICE)

        assert window.epoch_of(SERVICE) == stale_hint + 1
        assert window.record_success_if_epoch(SERVICE, WINDOW_SIZE, stale_hint) is False
        assert (
            window.record_success_if_epoch(
                SERVICE, WINDOW_SIZE, window.epoch_of(SERVICE)
            )
            is True
        )

    def test_nested_writes_hold_the_marker_until_the_last_end(self, window):
        """Two in-flight failure writes: the marker clears on the second end."""
        window.begin_write(SERVICE)
        window.begin_write(SERVICE)
        window.end_write(SERVICE)
        assert (
            window.record_success_if_epoch(
                SERVICE, WINDOW_SIZE, window.epoch_of(SERVICE)
            )
            is False
        )

        window.end_write(SERVICE)

        assert (
            window.record_success_if_epoch(
                SERVICE, WINDOW_SIZE, window.epoch_of(SERVICE)
            )
            is True
        )

    def test_end_write_without_begin_does_not_go_negative(self, window):
        """An unmatched end leaves no marker behind and still moves the epoch."""
        window.end_write(SERVICE)
        window.begin_write(SERVICE)
        window.end_write(SERVICE)

        assert (
            window.record_success_if_epoch(
                SERVICE, WINDOW_SIZE, window.epoch_of(SERVICE)
            )
            is True
        )

    def test_marker_is_visible_across_threads(self, window):
        """A success recording on another thread sees the writer's marker.

        The writer holds the marker until released; the recorder's verdict is
        taken while it is held and again after it is released.
        """
        released = threading.Event()
        verdicts: dict[str, bool] = {}
        hint = window.epoch_of(SERVICE)

        def writer() -> None:
            window.begin_write(SERVICE)
            released.wait(timeout=5.0)
            window.end_write(SERVICE)

        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        # Wait until the marker is observably held.
        for _ in range(1000):
            if window._writes_in_flight.get(SERVICE, 0):
                break
            threading.Event().wait(0.001)

        verdicts["during"] = window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint)
        released.set()
        writer_thread.join(timeout=5.0)
        verdicts["after_stale"] = window.record_success_if_epoch(
            SERVICE, WINDOW_SIZE, hint
        )
        verdicts["after_fresh"] = window.record_success_if_epoch(
            SERVICE, WINDOW_SIZE, window.epoch_of(SERVICE)
        )

        assert verdicts == {"during": False, "after_stale": False, "after_fresh": True}

    def test_bump_is_scoped_to_the_named_service(self, window):
        """A peer's bump leaves this name's hint valid."""
        hint = window.epoch_of(SERVICE)

        window.bump("peer")

        assert window.record_success_if_epoch(SERVICE, WINDOW_SIZE, hint) is True


# =============================================================================
# Contract — read_each and record_rejection
# =============================================================================


class TestOutcomeWindowReadEachContract:
    """The per-name snapshot the aggregate reader consumes."""

    def test_read_each_sums_equal_read_all(self, window):
        """One consistent snapshot: the per-name pairs sum to the total pair."""
        _seed(window, "a", failures=2, successes=3)
        _seed(window, "b", failures=0, successes=4)
        window.record_rejection("c", WINDOW_SIZE)

        per_name = window.read_each()

        assert per_name == {"a": (2, 5), "b": (0, 4), "c": (1, 1)}
        assert (
            sum(f for f, _ in per_name.values()),
            sum(t for _, t in per_name.values()),
        ) == window.read_all()

    def test_read_each_with_no_services_is_empty(self, window):
        assert window.read_each() == {}

    def test_read_each_returns_a_snapshot_not_the_live_rings(self, window):
        """Mutating the returned mapping never reaches the window."""
        _seed(window, "a", failures=1, successes=0)

        snapshot = window.read_each()
        snapshot["a"] = (99, 99)
        snapshot["ghost"] = (1, 1)

        assert window.read("a") == (1, 1)
        assert window.read("ghost") == (0, 0)

    def test_record_rejection_appends_a_failure_outcome(self, window):
        """A refused call is a call that did not succeed."""
        window.record_rejection(SERVICE, WINDOW_SIZE)

        assert window.read(SERVICE) == (FAILURE_OUTCOME, 1)
        assert list(window._windows[SERVICE]) == [FAILURE_OUTCOME]

    def test_record_rejection_does_not_move_the_epoch(self, window):
        """A refusal is evidence, not a transition — the hint stays valid."""
        hint = window.epoch_of(SERVICE)

        window.record_rejection(SERVICE, WINDOW_SIZE)

        assert window.epoch_of(SERVICE) == hint

    def test_rejections_are_bounded_by_the_window_size(self, window):
        """Refusals during a long OPEN period never grow past the ring."""
        for _ in range(WINDOW_SIZE + 5):
            window.record_rejection(SERVICE, WINDOW_SIZE)

        assert window.read(SERVICE) == (WINDOW_SIZE, WINDOW_SIZE)

    def test_success_and_rejection_encodings_are_distinct(self, window):
        """The encoding the aggregate sums over: a success is not a failure."""
        window.record_success(SERVICE, WINDOW_SIZE)
        window.record_rejection(SERVICE, WINDOW_SIZE)

        assert list(window._windows[SERVICE]) == [SUCCESS_OUTCOME, FAILURE_OUTCOME]
