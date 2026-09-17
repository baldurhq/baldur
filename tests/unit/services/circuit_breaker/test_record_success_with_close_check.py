"""
Tests for atomic record-success + close-check (497 D1 + D2).

Covers:
- TestCircuitBreakerCloseAttemptContract: dataclass shape (frozen, slots).
- TestRecordSuccessWithCloseCheckBehavior: InMemory atomic path —
  boundary (threshold-1 / threshold / threshold+1), state-transition,
  window-invariant preserved across CLOSED.
- TestAbcDefaultRaceUnsafeImplementation: ABC default delegates to
  record_success + update_state (race-unsafe default for non-InMemory).
- TestLayeredRepoCloseCheckDelegation: layered repo forwards to L1 and
  syncs the resulting state to L2.
- TestFallbackCloseWriteThroughOrderingBehavior: the L1-decided close is
  in the store when the attempt returns, so the CLOSED event's handlers
  (the replay lane loads L2 over L1 first) cannot hydrate the trip's OPEN
  back over it.
- TestRecordSuccessRaceBarrier: multi-threaded race test (N∈{2,4,8,16}).
- TestRecordSuccessRaceDeterministic: manual lock-ordering — stale-view
  caller sees did_close=False even after Thread A closes the circuit.
- TestRecordSuccessEmitGate: service-layer emits CIRCUIT_BREAKER_CLOSED
  exactly once across N record_success calls when repo returns
  did_close=True once.
"""

from __future__ import annotations

import dataclasses
import threading
from unittest.mock import MagicMock, Mock, patch

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)

# =============================================================================
# Helpers
# =============================================================================


def _open_then_half_open(
    repo: InMemoryCircuitBreakerStateRepository, service_name: str
) -> None:
    """Drive the repository to HALF_OPEN state for `service_name`.

    Uses the same `try_acquire_half_open_slot` transition as production code
    (OPEN → HALF_OPEN) so the window counters / DTO half_open fields are in
    the realistic state the close-check tests assume.
    """
    # Seed an OPEN state directly via atomic_force_open so we don't depend
    # on failure-threshold counting from CLOSED.
    repo.atomic_force_open(service_name, reason="test_seed", controlled_by_id=None)
    # Transition OPEN → HALF_OPEN (matches admission flow).
    allowed, prev, new = repo.try_acquire_half_open_slot(
        service_name, limit=10, stuck_timeout_seconds=60
    )
    assert allowed is True
    assert prev == CircuitBreakerStateEnum.OPEN.value
    assert new == CircuitBreakerStateEnum.HALF_OPEN.value


# =============================================================================
# CircuitBreakerCloseAttempt dataclass contract
# =============================================================================


class TestCircuitBreakerCloseAttemptContract:
    """Dataclass shape — frozen, slots, named-field access (497 D1)."""

    def test_dataclass_is_frozen(self):
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerCloseAttempt(state=state, did_close=True)

        with pytest.raises(dataclasses.FrozenInstanceError):
            attempt.did_close = False  # type: ignore[misc]

    def test_dataclass_has_slots(self):
        # `slots=True` removes `__dict__`; assigning a new attribute raises.
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerCloseAttempt(state=state, did_close=False)

        assert not hasattr(attempt, "__dict__")

    def test_named_field_access(self):
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerCloseAttempt(state=state, did_close=True)

        assert attempt.state is state
        assert attempt.did_close is True

    def test_did_close_false_default_value(self):
        # No defaults declared — both fields are required positional/kwargs.
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerCloseAttempt(state=state, did_close=False)

        assert attempt.did_close is False


# =============================================================================
# InMemory record_success_with_close_check — atomic path
# =============================================================================


class TestRecordSuccessWithCloseCheckBehavior:
    """Atomic record-success + close transition under one lock (497 D1)."""

    def test_below_threshold_returns_did_close_false(self):
        # Given: HALF_OPEN with success_threshold=2, no prior successes.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "payment.charge"
        _open_then_half_open(repo, service_name)

        # When: first success arrives.
        attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )

        # Then: still HALF_OPEN, did_close False.
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value
        assert attempt.state.success_count == 1

    def test_at_threshold_transitions_to_closed_with_did_close_true(self):
        # Given: HALF_OPEN with 1 prior success, threshold=2.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "payment.charge"
        _open_then_half_open(repo, service_name)
        repo.record_success_with_close_check(service_name, success_threshold=2)

        # When: second success arrives (reaches threshold).
        attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )

        # Then: transitions to CLOSED, did_close True, counts zeroed.
        assert attempt.did_close is True
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value
        assert attempt.state.success_count == 0
        assert attempt.state.failure_count == 0
        assert attempt.state.opened_at is None
        assert attempt.state.half_open_request_count == 0
        assert attempt.state.half_open_window_started_at is None

    def test_above_threshold_only_first_caller_sees_did_close_true(self):
        # Given: HALF_OPEN, threshold=2.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "payment.charge"
        _open_then_half_open(repo, service_name)
        repo.record_success_with_close_check(service_name, success_threshold=2)
        # Crosses the threshold — closes the circuit.
        first = repo.record_success_with_close_check(service_name, success_threshold=2)

        # When: a subsequent caller (state is now CLOSED) records success.
        second = repo.record_success_with_close_check(service_name, success_threshold=2)

        # Then: the second caller does NOT see did_close=True. The
        # threshold-trigger fired exactly once.
        assert first.did_close is True
        assert second.did_close is False
        # State remains CLOSED (no spurious re-transition).
        assert second.state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_closed_state_does_not_transition(self):
        # Given: a fresh CB in CLOSED.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        repo.get_or_create(service_name)  # state defaults to CLOSED

        # When: record_success_with_close_check called while CLOSED.
        attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )

        # Then: stays CLOSED, did_close False (threshold-fire is HALF_OPEN-only).
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_counters_zeroed_in_same_lock_acquire_as_close(self):
        # Given: HALF_OPEN with one logged failure.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)
        repo.record_failure(service_name)

        # When: two successes cross the threshold and close the circuit.
        repo.record_success_with_close_check(service_name, success_threshold=2)
        attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )

        # Then: the close transition and the counter reset commit together, so
        # no caller can observe CLOSED with residual counters.
        assert attempt.did_close is True
        assert attempt.state.failure_count == 0
        assert attempt.state.success_count == 0

        stored = repo.get_by_service_name(service_name)
        assert stored.failure_count == 0
        assert stored.success_count == 0


# =============================================================================
# ABC default — race-unsafe implementation
# =============================================================================


class _MinimalABCRepoForDefault(CircuitBreakerStateRepository):
    """Minimal subclass that does NOT override record_success_with_close_check.

    Used to exercise the race-unsafe default at the ABC layer — verifies the
    default delegates to record_success + update_state on threshold crossing.
    """

    def __init__(self):
        self.state = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=0,
        )
        self.record_success_calls = 0
        self.update_state_calls: list[dict] = []

    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        return self.state

    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        return self.state

    def update_state(
        self,
        service_name: str,
        state: str,
        failure_count: int | None = None,
        success_count: int | None = None,
        opened_at=None,
        half_open_request_count: int | None = None,
        reset_half_open_count: bool = False,
    ) -> bool:
        self.update_state_calls.append(
            {
                "state": state,
                "failure_count": failure_count,
                "success_count": success_count,
                "reset_half_open_count": reset_half_open_count,
            }
        )
        # Reflect into self.state so subsequent get_by_service_name sees CLOSED.
        self.state = CircuitBreakerStateData(
            service_name=self.state.service_name,
            state=state,
            failure_count=failure_count if failure_count is not None else 0,
            success_count=success_count if success_count is not None else 0,
        )
        return True

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        return self.state

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        self.record_success_calls += 1
        # Simulate increment.
        self.state = CircuitBreakerStateData(
            service_name=self.state.service_name,
            state=self.state.state,
            failure_count=self.state.failure_count,
            success_count=self.state.success_count + 1,
        )
        return self.state

    def set_manual_control(
        self,
        service_name,
        state,
        controlled_by_id=None,
        reason="",
        expires_at=None,
    ):
        return True

    def clear_manual_control(self, service_name, preserve_reason=False):
        return True

    def get_all_states(self):
        return [self.state]

    # Remaining abstract methods — stubs (unused by the default-path tests).
    def reset(self, service_name):
        return True

    def atomic_force_open(
        self, service_name, reason="", controlled_by_id=None, ttl_minutes=None
    ):
        return (True, "", "open")

    def atomic_force_close(
        self, service_name, reason="", controlled_by_id=None, ttl_minutes=None
    ):
        return (True, "", "closed")

    def atomic_reset(self, service_name, reason="", controlled_by_id=None):
        return (True, "", "closed")

    def try_acquire_half_open_slot(self, service_name, limit, stuck_timeout_seconds):
        return (True, "open", "half_open")

    def reset_half_open_count(self, service_name):
        return None

    def delete_state(self, service_name):
        return True


class TestAbcDefaultRaceUnsafeImplementation:
    """ABC default delegates to record_success + update_state (497 D1)."""

    def test_below_threshold_calls_record_success_only(self):
        # Given: HALF_OPEN, success_count=0, threshold=2.
        repo = _MinimalABCRepoForDefault()

        # When: first success.
        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        # Then: record_success called, update_state NOT called.
        assert repo.record_success_calls == 1
        assert repo.update_state_calls == []
        assert attempt.did_close is False
        assert attempt.state.state == CircuitBreakerStateEnum.HALF_OPEN.value

    def test_at_threshold_calls_update_state_with_closed(self):
        # Given: HALF_OPEN, success_count=1 (one below threshold).
        repo = _MinimalABCRepoForDefault()
        repo.state = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=1,
        )

        # When: second success crosses threshold.
        attempt = repo.record_success_with_close_check("svc", success_threshold=2)

        # Then: record_success called once + update_state(closed, success_count=0).
        assert repo.record_success_calls == 1
        assert len(repo.update_state_calls) == 1
        call = repo.update_state_calls[0]
        assert call["state"] == CircuitBreakerStateEnum.CLOSED.value
        assert call["success_count"] == 0
        assert call["failure_count"] == 0
        assert call["reset_half_open_count"] is True
        assert attempt.did_close is True


# =============================================================================
# Layered repo delegation
# =============================================================================


class TestLayeredRepoCloseCheckDelegation:
    """Layered repo forwards to L1 and syncs result to L2 (497 D1)."""

    def test_delegates_to_l1_and_returns_l1_attempt(self):
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )

        # Given: layered repo without L2 — exercises the L1 delegation path.
        repo = LayeredCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo._l1, service_name)

        # When: close-check called via layered repo (threshold=1 → closes).
        attempt = repo.record_success_with_close_check(
            service_name, success_threshold=1
        )

        # Then: returned attempt reflects L1's atomic decision.
        assert attempt.did_close is True
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_close_is_written_through_after_the_close(self):
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )

        # Given: L1 in HALF_OPEN, threshold=1 will close on first success.
        repo = LayeredCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo._l1, service_name)

        # When.
        with (
            patch.object(repo, "_sync_close_to_l2", autospec=True) as close,
            patch.object(repo, "_sync_to_l2_async", autospec=True) as snapshot,
        ):
            attempt = repo.record_success_with_close_check(
                service_name, success_threshold=1
            )

        # Then: the close write-through runs for this service -- not the
        # snapshot mirror, which never closes a stored row -- and the row it
        # fresh-reads already carries the close, so what matters is that the
        # write follows the transition.
        close.assert_called_once_with(service_name)
        snapshot.assert_not_called()
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value
        assert (
            repo._l1.get_by_service_name(service_name).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_non_closing_success_nudges_the_snapshot_mirror(self):
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )

        # Given: L1 in HALF_OPEN, threshold=2 leaves the first success short.
        repo = LayeredCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo._l1, service_name)

        # When.
        with (
            patch.object(repo, "_sync_close_to_l2", autospec=True) as close,
            patch.object(repo, "_sync_to_l2_async", autospec=True) as snapshot,
        ):
            attempt = repo.record_success_with_close_check(
                service_name, success_threshold=2
            )

        # Then: a mid-trial success is a snapshot, not a close.
        assert attempt.did_close is False
        snapshot.assert_called_once_with(service_name)
        close.assert_not_called()


# =============================================================================
# Fallback close write-through ordering
# =============================================================================


def _trip_automatically(
    repo: InMemoryCircuitBreakerStateRepository, service_name: str
) -> None:
    """Trip CLOSED -> OPEN the way failures do, leaving no operator pin.

    ``atomic_force_open`` is the operator's Block and pins the row; the
    write-through under test is pin-neutral and would rightly skip it.
    """
    repo.get_or_create(service_name)
    assert repo.trip_to_open(service_name, failure_count=5).did_open is True


def _layered_over_memory_l2():
    """Layered repo whose L2 is a plain in-memory store, quarantined.

    The in-memory L2 stands in for the degraded backend's fallback: the
    trip's OPEN row is what a store that stood down during the trip still
    holds. L1 carries the same trip and has admitted its trial call.
    ``_l2_healthy`` False routes the close-check to the L1 fallback branch,
    the path a zero-config or Redis-quarantined process takes.
    """
    from baldur.adapters.memory.layered_repository import (
        LayeredCircuitBreakerStateRepository,
    )

    l2 = InMemoryCircuitBreakerStateRepository()
    repo = LayeredCircuitBreakerStateRepository(l2_repo=l2, adapter_type="memory")
    service_name = "svc"
    _trip_automatically(l2, service_name)
    _trip_automatically(repo._l1, service_name)
    allowed, _prev, new = repo._l1.try_acquire_half_open_slot(
        service_name, limit=10, stuck_timeout_seconds=60
    )
    assert allowed is True
    assert new == CircuitBreakerStateEnum.HALF_OPEN.value
    repo._l2_healthy = False
    return repo, l2, service_name


class _LazyFuture:
    """Runs its callable the first time someone waits on it."""

    def __init__(self, fn, args, kwargs):
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._done = False
        self._value = None

    def result(self, timeout=None):
        if not self._done:
            self._value = self._fn(*self._args, **self._kwargs)
            self._done = True
        return self._value

    def done(self) -> bool:
        return self._done

    def cancel(self) -> bool:
        return not self._done


class _LazyExecutor:
    """Executor stub whose work runs only when its future is awaited.

    A write the caller waits for lands before the caller continues; a write
    left on the queue never lands. That is the property under test, stated
    as an executor — no threads, no sleeps, no timing margin to get wrong.
    """

    def submit(self, fn, *args, **kwargs):
        return _LazyFuture(fn, args, kwargs)


class TestFallbackCloseWriteThroughOrderingBehavior:
    """The close is in the store before the attempt returns.

    The CLOSED event published on return is handled by the replay lane,
    whose affirmation loads L2 over L1 before reading the row. A close that
    reached the store only after that load would be undone by it: the trip's
    OPEN hydrates back over L1, the replay is refused as ``circuit_reopened``,
    and the breaker flaps. So the write-through is awaited, on the same
    timeout-bounded lane as the healthy branch's store-side close-check.
    """

    def test_store_row_is_closed_when_the_attempt_returns(self):
        repo, l2, service_name = _layered_over_memory_l2()

        with patch.object(repo, "_get_executor", return_value=_LazyExecutor()):
            attempt = repo.record_success_with_close_check(
                service_name, success_threshold=1
            )

        assert attempt.did_close is True
        assert (
            l2.get_by_service_name(service_name).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_load_from_l2_after_the_close_keeps_l1_closed(self):
        """The replay lane's affirmation shape: load L2 over L1, then read L1.

        The load body runs on the calling thread here, as it does on one of
        the production pool's workers; the lazy executor never runs a write
        the close did not wait for, which is what the pool's parallel workers
        make possible.
        """
        repo, _l2, service_name = _layered_over_memory_l2()

        with patch.object(repo, "_get_executor", return_value=_LazyExecutor()):
            repo.record_success_with_close_check(service_name, success_threshold=1)
            repo._load_from_l2()

        assert (
            repo._l1.get_by_service_name(service_name).state
            == CircuitBreakerStateEnum.CLOSED.value
        )

    def test_write_through_is_unguarded_and_pin_neutral(self):
        """A close must close the stored row (no keep-open), never a pinned one."""
        repo, _l2, service_name = _layered_over_memory_l2()

        with patch.object(repo, "_sync_to_l2_with_timeout", autospec=True) as write:
            repo.record_success_with_close_check(service_name, success_threshold=1)

        write.assert_called_once()
        _, kwargs = write.call_args
        assert kwargs == {"skip_if_pinned": True, "keep_open": False}


# =============================================================================
# Race tests — multi-threaded barrier
# =============================================================================


class TestRecordSuccessRaceBarrier:
    """Multi-threaded race: exactly one thread sees did_close=True."""

    @pytest.mark.parametrize("n_threads", [2, 4, 8, 16])
    def test_only_one_thread_closes_across_n_callers(self, n_threads: int):
        # Given: HALF_OPEN, success_threshold=2. All N threads race to record
        # a success. With the atomic close-check, only the caller crossing
        # the threshold under the lock should see did_close=True.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)
        # Pre-seed 1 success so a single concurrent success crosses threshold=2.
        repo.record_success_with_close_check(service_name, success_threshold=2)

        barrier = threading.Barrier(n_threads)
        results: list[CircuitBreakerCloseAttempt] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            attempt = repo.record_success_with_close_check(
                service_name, success_threshold=2
            )
            with results_lock:
                results.append(attempt)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then: exactly one thread crossed the threshold and closed the circuit.
        close_count = sum(1 for r in results if r.did_close)
        assert close_count == 1

        # Final storage state is CLOSED.
        final = repo.get_by_service_name(service_name)
        assert final is not None
        assert final.state == CircuitBreakerStateEnum.CLOSED.value


class TestRecordSuccessRaceDeterministic:
    """Manual lock-ordering — stale-view caller sees did_close=False."""

    def test_stale_view_caller_observes_did_close_false_after_close(self):
        # Given: HALF_OPEN with 1 prior success, threshold=2. Thread A has
        # crossed the threshold and closed the circuit. Thread B, which had
        # taken a pre-close `state.state == "half_open"` view, now calls
        # record_success_with_close_check.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)
        # Thread A's history.
        repo.record_success_with_close_check(service_name, success_threshold=2)
        thread_a_attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )
        assert thread_a_attempt.did_close is True

        # When: Thread B calls record_success_with_close_check (state is now
        # CLOSED in storage, even though Thread B's stale view said HALF_OPEN).
        thread_b_attempt = repo.record_success_with_close_check(
            service_name, success_threshold=2
        )

        # Then: Thread B does NOT see did_close=True — the close-fire happened
        # exactly once.
        assert thread_b_attempt.did_close is False


# =============================================================================
# Service-level emit gate (D2)
# =============================================================================


class TestRecordSuccessEmitGate:
    """Service emits CIRCUIT_BREAKER_CLOSED exactly once across N calls."""

    def _build_service_with_mocked_emit(self, did_close_pattern: list[bool]):
        from baldur.services.circuit_breaker.config import CircuitBreakerConfig
        from baldur.services.circuit_breaker.service import CircuitBreakerService

        config = CircuitBreakerConfig(
            enabled=True,
            failure_threshold=100,
            success_threshold=2,
            minimum_calls=10,
        )
        repo = Mock()
        half_open_state = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
            success_count=1,
        )
        repo.get_or_create = Mock(return_value=half_open_state)
        repo.update_state = Mock(return_value=True)
        # Sequence of did_close values matches the pattern arg.
        attempts = [
            CircuitBreakerCloseAttempt(state=half_open_state, did_close=did_close)
            for did_close in did_close_pattern
        ]
        repo.record_success_with_close_check = Mock(side_effect=attempts)

        service = CircuitBreakerService(config=config, repository=repo)
        service._emit_event = MagicMock()
        return service, repo

    def test_emit_fires_only_when_did_close_true(self):
        # Given: service whose mock repo returns did_close=True once then False.
        service, repo = self._build_service_with_mocked_emit(
            did_close_pattern=[True, False, False, False]
        )
        hint = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        # When: N service-level record_success calls land in HALF_OPEN.
        for _ in range(4):
            service.record_success("svc", hint_state=hint)

        # Then: repository called N times, but the emit fires exactly once —
        # only for the did_close=True attempt.
        assert repo.record_success_with_close_check.call_count == 4
        emit_calls = service._emit_event.call_args_list
        from baldur.services.event_bus import EventType

        closed_emits = [
            c for c in emit_calls if c[0][0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert len(closed_emits) == 1

    def test_no_emit_when_all_did_close_false(self):
        # Given: every attempt returns did_close=False (no caller crossed
        # the threshold under the lock).
        service, _ = self._build_service_with_mocked_emit(
            did_close_pattern=[False, False, False]
        )
        hint = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

        # When.
        for _ in range(3):
            service.record_success("svc", hint_state=hint)

        # Then: no CIRCUIT_BREAKER_CLOSED emits at all.
        from baldur.services.event_bus import EventType

        closed_emits = [
            c
            for c in service._emit_event.call_args_list
            if c[0][0] == EventType.CIRCUIT_BREAKER_CLOSED
        ]
        assert len(closed_emits) == 0
