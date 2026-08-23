"""476 — InMemoryCircuitBreakerStateRepository HALF_OPEN atomicity.

Covers the L1 side of the HALF_OPEN slot acquisition contract:

- ``try_acquire_half_open_slot`` boundary + state-matrix behavior
  (including D8 stuck-recovery branch).
- ``_last_acquire_marker`` exposure read by LayeredCircuitBreakerStateRepository
  to emit the stuck-recovery observability counter.
- ``reset_half_open_count`` idempotency + missing-entry handling.
- ``update_state(reset_half_open_count=True)`` D9 atomic state-and-counter
  clear in a single round-trip.
- The absent-watermark acquire contract: under the limit the first acquire
  stamps the missing window, at the limit an absent watermark counts as
  older than any timeout.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
from baldur.interfaces.repositories import (
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
)
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time, get_fixed_datetime


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _force_state(
    repo: InMemoryCircuitBreakerStateRepository,
    service: str,
    *,
    state: str,
    half_open_request_count: int = 0,
    success_count: int | None = None,
    window_age_seconds: float | None = None,
) -> None:
    """Drive the repo into a target state without going through try_acquire.

    ``window_age_seconds`` lets the test set ``half_open_window_started_at``
    to an arbitrary point in the past so the D8 stuck-recovery branch can
    be exercised deterministically without sleeping. Leaving it ``None``
    keeps the watermark unset — the shape every state-copy lane produces,
    which the absent-watermark cases below drive.
    """
    repo.get_or_create(service)
    repo.update_state(
        service_name=service,
        state=state,
        success_count=success_count,
        half_open_request_count=half_open_request_count,
    )
    if window_age_seconds is not None:
        with repo._lock:
            entry = repo._storage[service]
            backdated = entry.updated_at - timedelta(seconds=window_age_seconds)
            object.__setattr__(entry, "half_open_window_started_at", backdated)


# =============================================================================
# try_acquire_half_open_slot — state matrix
# =============================================================================


class TestInMemoryTryAcquireBehavior:
    """State-machine + boundary coverage for the L1 atomic primitive."""

    def test_open_to_half_open_transition_returns_open_half_open_tuple(self, repo):
        """OPEN + try_acquire → (True, 'open', 'half_open'); count initialized to 1."""
        _force_state(repo, "svc", state=CircuitBreakerStateEnum.OPEN.value)

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (
            True,
            CircuitBreakerStateEnum.OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        state = repo.get_by_service_name("svc")
        assert state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert state.half_open_request_count == 1
        assert state.success_count == 0
        assert state.half_open_window_started_at is not None
        assert repo._last_acquire_marker == "transition"

    def test_half_open_under_limit_increments_counter(self, repo):
        """HALF_OPEN with count<limit → (True, 'half_open', 'half_open')."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=3,
        )

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (
            True,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        assert repo.get_by_service_name("svc").half_open_request_count == 4
        assert repo._last_acquire_marker == "increment"

    def test_half_open_at_limit_rejects_without_stuck_recovery(self, repo):
        """HALF_OPEN with count==limit and fresh window → (False, ..., 'rejected')."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=10,
            window_age_seconds=0.0,
        )

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert allowed is False
        assert prev_state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert new_state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert repo.get_by_service_name("svc").half_open_request_count == 10
        assert repo._last_acquire_marker == "rejected"

    def test_closed_state_returns_no_op_marker(self, repo):
        """CLOSED state → (False, 'closed', 'closed') no-op marker."""
        repo.get_or_create("svc")  # CLOSED by default

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (
            False,
            CircuitBreakerStateEnum.CLOSED.value,
            CircuitBreakerStateEnum.CLOSED.value,
        )
        assert repo._last_acquire_marker == "no_op"

    def test_d8_stuck_window_auto_resets_counter(self, repo):
        """D8: HALF_OPEN at limit with stale watermark auto-resets the window."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=10,
            window_age_seconds=120.0,  # > stuck_timeout=60
        )

        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=10, stuck_timeout_seconds=60
        )

        assert (allowed, prev_state, new_state) == (
            True,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 1
        assert state.success_count == 0
        # Watermark is refreshed to "now" — must be newer than the backdated value.
        assert state.half_open_window_started_at >= utc_now() - timedelta(seconds=5)
        assert repo._last_acquire_marker == "stuck_recovery"

    def test_stuck_recovery_excludes_exact_timeout_boundary(self, repo):
        """At window_age == stuck_timeout exactly, the slot is REJECTED.

        The stuck-recovery guard uses strict ``>``, so the boundary instant is
        not yet "stuck". Frozen time makes window_age exactly equal to the
        timeout (no wall-clock epsilon), which pins the ``>`` vs ``>=`` choice
        that a non-frozen test cannot distinguish.
        """
        stuck_timeout = 60
        with freeze_time("2026-02-10 10:00:00"):
            _force_state(
                repo,
                "svc",
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                half_open_request_count=10,
                window_age_seconds=float(stuck_timeout),  # age == timeout exactly
            )

            allowed, _prev, _new = repo.try_acquire_half_open_slot(
                service_name="svc", limit=10, stuck_timeout_seconds=stuck_timeout
            )

        assert allowed is False
        assert repo._last_acquire_marker == "rejected"

    @pytest.mark.parametrize(
        ("limit", "initial_count", "expected_allowed"),
        [
            (1, 0, True),  # under limit, ok
            (1, 1, False),  # at limit (rejected without stuck recovery)
            (10, 9, True),  # N-1
            (10, 10, False),  # N
            (10, 11, False),  # N+1 (over-limit treated as at-or-above)
        ],
    )
    def test_limit_boundary_matrix(self, repo, limit, initial_count, expected_allowed):
        """Limit boundary cases: 1, N-1, N, N+1 around half_open_max_calls."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=initial_count,
            window_age_seconds=0.0,
        )

        allowed, _prev, _new = repo.try_acquire_half_open_slot(
            service_name="svc", limit=limit, stuck_timeout_seconds=60
        )

        assert allowed is expected_allowed

    def test_zero_limit_rejects_immediately(self, repo):
        """limit=0 → no slot ever acquirable; HALF_OPEN at count==0 rejects."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=0,
            window_age_seconds=0.0,
        )

        allowed, _prev, _new = repo.try_acquire_half_open_slot(
            service_name="svc", limit=0, stuck_timeout_seconds=60
        )

        assert allowed is False
        # Counter is unchanged.
        assert repo.get_by_service_name("svc").half_open_request_count == 0


# =============================================================================
# try_acquire_half_open_slot — absent watermark (adoption stamp + recovery)
# =============================================================================


class TestInMemoryTryAcquireWatermarkAbsentBehavior:
    """A ``half_open`` row can arrive without its window watermark.

    Every lane that copies ``state`` without the window fields produces
    that shape: snapshot hydration, drift reconciliation's remote-wins
    copy, and the whole-row mirrors into the durable store. The acquire
    boundary owns it — under the limit the first acquire starts the
    window, at the limit an absent watermark counts as older than any
    timeout so the row cannot pin the breaker in HALF_OPEN.
    """

    def test_watermark_absent_under_limit_stamps_the_window_with_now(self, repo):
        """Branch 3: the first acquire on an unwatermarked window starts it."""
        # Given — the state-copy shape: half_open, count 0, no watermark.
        with freeze_time("2026-02-10 10:00:00"):
            _force_state(
                repo,
                "svc",
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                half_open_request_count=0,
            )
            assert repo.get_by_service_name("svc").half_open_window_started_at is None

            # When — a trial call takes the under-limit branch.
            allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
                service_name="svc", limit=3, stuck_timeout_seconds=60
            )

        # Then — granted as an ordinary increment, and the window now exists.
        assert (allowed, prev_state, new_state) == (
            True,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        assert repo._last_acquire_marker == "increment"
        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 1
        assert state.half_open_window_started_at == get_fixed_datetime(
            2026, 2, 10, 10, 0, 0
        )

    def test_watermark_present_under_limit_is_not_overwritten_by_the_stamp(self, repo):
        """The stamp is an adoption, not a refresh: a live window survives.

        Restamping on every increment would keep sliding the window's start
        forward, so a genuinely stuck window would never age past
        ``stuck_timeout`` and the recovery branch would be unreachable.
        """
        # Given — a window opened 30 s ago with room left in it.
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=1,
            window_age_seconds=30.0,
        )
        watermark_before = repo.get_by_service_name("svc").half_open_window_started_at

        # When
        repo.try_acquire_half_open_slot(
            service_name="svc", limit=3, stuck_timeout_seconds=60
        )

        # Then — counter moved, watermark did not.
        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 2
        assert state.half_open_window_started_at == watermark_before

    def test_watermark_absent_at_limit_recovers_the_window(self, repo):
        """Branch 1: an at-limit row with no watermark is older than any timeout.

        This is the fail-open defense line for a writer the adoption stamp
        does not cover — the alternative reading (reject) is a permanent
        lockout with no branch that can ever write the missing watermark.
        """
        # Given — at the limit, no watermark, a non-zero success_count that
        # the auto-reset must clear.
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=3,
            success_count=5,
        )

        # When
        allowed, prev_state, new_state = repo.try_acquire_half_open_slot(
            service_name="svc", limit=3, stuck_timeout_seconds=60
        )

        # Then — the stuck-window auto-reset fires: grant, fresh window.
        assert (allowed, prev_state, new_state) == (
            True,
            CircuitBreakerStateEnum.HALF_OPEN.value,
            CircuitBreakerStateEnum.HALF_OPEN.value,
        )
        assert repo._last_acquire_marker == "stuck_recovery"
        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 1
        assert state.success_count == 0
        assert state.half_open_window_started_at >= utc_now() - timedelta(seconds=5)

    def test_watermark_absent_row_driven_to_limit_rejects_within_stuck_timeout(
        self, repo
    ):
        """A hydrated window admits exactly ``limit`` trials, then rejects.

        The negative half of the adoption stamp: once the counter climbs
        through the under-limit branch the watermark exists, so reaching the
        limit inside ``stuck_timeout`` is an ordinary rejection — not the
        stuck-window recovery that an unwatermarked row used to trigger.
        Frozen time keeps the window age at 0, well inside the timeout.
        """
        limit = 3
        with freeze_time("2026-02-10 10:00:00"):
            # Given — the hydrated shape, driven to the limit by real acquires.
            _force_state(
                repo,
                "svc",
                state=CircuitBreakerStateEnum.HALF_OPEN.value,
                half_open_request_count=0,
            )
            for _ in range(limit):
                repo.try_acquire_half_open_slot(
                    service_name="svc", limit=limit, stuck_timeout_seconds=60
                )

            # When — one trial call too many, still inside the window.
            allowed, _prev, _new = repo.try_acquire_half_open_slot(
                service_name="svc", limit=limit, stuck_timeout_seconds=60
            )

        # Then — rejected. Pre-adoption this row still had no watermark and
        # took the stuck-recovery branch, admitting another full window.
        assert allowed is False
        assert repo._last_acquire_marker == "rejected"
        assert repo.get_by_service_name("svc").half_open_request_count == limit


class TestInMemoryTryAcquireWatermarkMatrixContract:
    """Acquire matrix: watermark class x counter position -> contract outcome.

    The hardcoded cells are the contract itself — the same table the SQL and
    Redis implementations must satisfy, which is why an absent watermark
    reads as "not yet started" below the limit and as "older than any
    timeout" at it.
    """

    _STUCK_TIMEOUT = 60
    _LIMIT = 3

    @pytest.mark.parametrize(
        ("window_age_seconds", "initial_count", "expected_allowed", "expected_marker"),
        [
            (None, 0, True, "increment"),
            (None, 3, True, "stuck_recovery"),
            (0.0, 0, True, "increment"),
            (0.0, 3, False, "rejected"),
            (120.0, 0, True, "increment"),
            (120.0, 3, True, "stuck_recovery"),
        ],
        ids=[
            "absent_under_limit",
            "absent_at_limit",
            "fresh_under_limit",
            "fresh_at_limit",
            "stale_under_limit",
            "stale_at_limit",
        ],
    )
    def test_watermark_absent_matrix_matches_the_contract(
        self,
        repo,
        window_age_seconds,
        initial_count,
        expected_allowed,
        expected_marker,
    ):
        """Every cell also leaves a watermark behind when the slot is granted."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=initial_count,
            window_age_seconds=window_age_seconds,
        )

        allowed, _prev, _new = repo.try_acquire_half_open_slot(
            service_name="svc",
            limit=self._LIMIT,
            stuck_timeout_seconds=self._STUCK_TIMEOUT,
        )

        assert allowed is expected_allowed
        assert repo._last_acquire_marker == expected_marker
        if allowed:
            state = repo.get_by_service_name("svc")
            assert state.half_open_window_started_at is not None


# =============================================================================
# Property: the counter never advances without a window watermark
# =============================================================================

# Every writer of the (half_open_request_count, half_open_window_started_at)
# pair that a production caller can reach. ``update_state`` with an explicit
# ``half_open_request_count`` is deliberately absent: it can set the counter
# without a watermark, and no production caller passes that argument.
_PAIR_WRITERS = (
    "acquire",
    "reset_half_open_count",
    "update_state_reset_flag",
    "mirror_half_open",
    "mirror_open",
    "hydrate_half_open_snapshot",
)


class TestInMemoryHalfOpenWatermarkPairProperties:
    """``count > 0`` implies a watermark, whatever order the writers run in.

    The example-based cases above pin the two acquire branches; this searches
    interleavings of every other writer that touches the pair for a sequence
    that strands a counter without its window — the shape that made a healthy
    window read as stalled.
    """

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        writers=st.lists(st.sampled_from(_PAIR_WRITERS), min_size=1, max_size=25),
    )
    def test_counter_never_advances_without_a_watermark(self, writers):
        """After every writer, a non-zero counter carries a window start."""
        # Given — a repo seeded with the hydrated shape the property targets.
        repo = InMemoryCircuitBreakerStateRepository()
        half_open = CircuitBreakerStateEnum.HALF_OPEN.value
        repo.hydrate_snapshot(
            CircuitBreakerStateData(service_name="svc", state=half_open)
        )

        for writer in writers:
            # When — one writer runs.
            if writer == "acquire":
                repo.try_acquire_half_open_slot(
                    service_name="svc", limit=3, stuck_timeout_seconds=60
                )
            elif writer == "reset_half_open_count":
                repo.reset_half_open_count("svc")
            elif writer == "update_state_reset_flag":
                repo.update_state(
                    service_name="svc",
                    state=half_open,
                    reset_half_open_count=True,
                )
            elif writer == "mirror_half_open":
                repo.update_state(service_name="svc", state=half_open, success_count=1)
            elif writer == "mirror_open":
                repo.update_state(
                    service_name="svc", state=CircuitBreakerStateEnum.OPEN.value
                )
            else:
                repo.hydrate_snapshot(
                    CircuitBreakerStateData(service_name="svc", state=half_open)
                )

            # Then — the pair is still coherent.
            state = repo.get_by_service_name("svc")
            if state.half_open_request_count > 0:
                assert state.half_open_window_started_at is not None, (
                    f"counter {state.half_open_request_count} without a watermark "
                    f"after {writer!r}"
                )


# =============================================================================
# reset_half_open_count
# =============================================================================


class TestInMemoryResetHalfOpenCountBehavior:
    """G8: counter+watermark clear, idempotent, tolerates missing entry."""

    def test_clears_counter_and_watermark(self, repo):
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=7,
            window_age_seconds=0.0,
        )

        repo.reset_half_open_count("svc")

        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 0
        assert state.half_open_window_started_at is None

    def test_idempotent_when_already_zero(self, repo):
        repo.get_or_create("svc")  # fresh entry, count is 0

        repo.reset_half_open_count("svc")
        repo.reset_half_open_count("svc")

        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 0
        assert state.half_open_window_started_at is None

    def test_missing_service_silently_no_ops(self, repo):
        """Calling reset on a service we've never seen must not raise."""
        repo.reset_half_open_count("never-seen-service")

        # The repository did NOT auto-create an entry on a reset.
        assert repo.get_by_service_name("never-seen-service") is None


# =============================================================================
# update_state with reset_half_open_count flag (D9 single round-trip)
# =============================================================================


class TestInMemoryUpdateStateResetFlagBehavior:
    """D9: state transition + counter clear must happen atomically.

    Same call must update ``state`` AND clear ``half_open_request_count`` and
    ``half_open_window_started_at``. The reset flag has precedence over an
    explicit ``half_open_request_count`` arg, so callers can't accidentally
    leave a stale count behind on a HALF_OPEN→OPEN/CLOSED transition.
    """

    def test_reset_flag_clears_counter_and_watermark_with_state_change(self, repo):
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=5,
            window_age_seconds=0.0,
        )

        # HALF_OPEN → CLOSED transition with atomic counter clear (success path).
        result = repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.CLOSED.value,
            failure_count=0,
            success_count=0,
            opened_at=None,
            reset_half_open_count=True,
        )

        assert result is True
        state = repo.get_by_service_name("svc")
        assert state.state == CircuitBreakerStateEnum.CLOSED.value
        assert state.half_open_request_count == 0
        assert state.half_open_window_started_at is None

    def test_reset_flag_overrides_explicit_count_arg(self, repo):
        """If both reset_half_open_count=True and half_open_request_count=N are
        passed, the reset wins. Callers that pass both have a bug; the adapter
        chooses safety (clear) over the explicit value.
        """
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=3,
        )

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
            half_open_request_count=99,  # ignored
            reset_half_open_count=True,
        )

        assert repo.get_by_service_name("svc").half_open_request_count == 0

    def test_no_reset_flag_preserves_counter(self, repo):
        """Default behavior: state-only update doesn't touch the counter."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=4,
            window_age_seconds=0.0,
        )

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        state = repo.get_by_service_name("svc")
        assert state.half_open_request_count == 4
        assert state.half_open_window_started_at is not None

    def test_explicit_count_arg_without_reset_flag_applies(self, repo):
        """half_open_request_count=N alone updates the counter."""
        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=2,
        )

        repo.update_state(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            half_open_request_count=8,
        )

        assert repo.get_by_service_name("svc").half_open_request_count == 8


# =============================================================================
# Concurrent increment safety (RLock)
# =============================================================================


class TestInMemoryTryAcquireConcurrencyBehavior:
    """Verify the RLock-protected counter holds the §382 contract.

    50 threads racing on a HALF_OPEN window with limit=10 must yield
    *exactly* 10 acquires across the cluster. Pre-476 behavior (unlocked
    dict) returned 11 or 21 in 5/5 runs — this test is the L1-side
    regression guard. The Redis Lua's cluster-wide guarantee is covered
    separately by the integration suite.
    """

    def test_thread_safety_caps_acquires_at_limit(self, repo):
        import threading

        _force_state(
            repo,
            "svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )

        limit = 10
        n_threads = 50
        results: list[bool] = []
        results_lock = threading.Lock()

        def attempt():
            allowed, _prev, _new = repo.try_acquire_half_open_slot(
                service_name="svc", limit=limit, stuck_timeout_seconds=60
            )
            with results_lock:
                results.append(allowed)

        threads = [threading.Thread(target=attempt) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly `limit` threads observe allowed=True. The first call
        # transitions OPEN→HALF_OPEN and counts as the first slot.
        assert sum(1 for ok in results if ok) == limit
        assert repo.get_by_service_name("svc").half_open_request_count == limit
