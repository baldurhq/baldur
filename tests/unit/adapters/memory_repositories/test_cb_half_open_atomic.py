"""476 — InMemoryCircuitBreakerStateRepository HALF_OPEN atomicity.

Covers the L1 side of the HALF_OPEN slot acquisition contract:

- ``try_acquire_half_open_slot`` boundary + state-matrix behavior
  (including D8 stuck-recovery branch).
- ``_last_acquire_marker`` exposure read by LayeredCircuitBreakerStateRepository
  to emit the stuck-recovery observability counter.
- ``reset_half_open_count`` idempotency + missing-entry handling.
- ``update_state(reset_half_open_count=True)`` D9 atomic state-and-counter
  clear in a single round-trip.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from baldur.adapters.memory import InMemoryCircuitBreakerStateRepository
from baldur.interfaces.repositories import CircuitBreakerStateEnum
from baldur.utils.time import utc_now
from tests.factories.time_helpers import freeze_time


@pytest.fixture
def repo() -> InMemoryCircuitBreakerStateRepository:
    return InMemoryCircuitBreakerStateRepository()


def _force_state(
    repo: InMemoryCircuitBreakerStateRepository,
    service: str,
    *,
    state: str,
    half_open_request_count: int = 0,
    window_age_seconds: float | None = None,
) -> None:
    """Drive the repo into a target state without going through try_acquire.

    ``window_age_seconds`` lets the test set ``half_open_window_started_at``
    to an arbitrary point in the past so the D8 stuck-recovery branch can
    be exercised deterministically without sleeping.
    """
    repo.get_or_create(service)
    repo.update_state(
        service_name=service,
        state=state,
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
