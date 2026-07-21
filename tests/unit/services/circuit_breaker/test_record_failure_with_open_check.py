"""656 D7 — symmetric atomic record-failure + HALF_OPEN->OPEN open-check.

The failure-side mirror of ``record_success_with_close_check`` (#497/#498).
A single HALF_OPEN failure re-opens the circuit unconditionally (no
threshold), so the open-check is simpler than the close-check.

Covers:
- ``TestCircuitBreakerOpenAttemptContract``: dataclass shape (frozen, slots).
- ``TestRecordFailureWithOpenCheckBehavior``: InMemory atomic path —
  HALF_OPEN -> OPEN transition under one lock (counters zeroed, window
  cleared, ``opened_at`` set), non-HALF_OPEN no-op, stale-view single
  winner.
- ``TestOpenCheckAbcDefault``: ABC race-unsafe default delegates to
  ``update_state(OPEN)`` only when it observes HALF_OPEN.
- ``TestLayeredRepoOpenCheckDelegation``: layered repo without L2 falls
  back to L1's atomic open-check and async-syncs the result to L2.
- ``TestRecordFailureOpenRaceBarrier``: multi-threaded race (N in
  {2,4,8,16}) — exactly one thread sees ``did_open=True``.
- ``TestRecordFailureOpenEmitGate``: service-layer emits
  ``CIRCUIT_BREAKER_OPENED`` exactly once across N HALF_OPEN failures when
  the repo returns ``did_open=True`` once — the #498 multi-emit fix.
"""

from __future__ import annotations

import dataclasses
import threading
from unittest.mock import MagicMock, Mock

import pytest

from baldur.adapters.memory.circuit_breaker import (
    InMemoryCircuitBreakerStateRepository,
)
from baldur.interfaces.repositories import (
    CircuitBreakerOpenAttempt,
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
    """Drive the repository to HALF_OPEN state for ``service_name``.

    Uses the same OPEN -> HALF_OPEN ``try_acquire_half_open_slot`` transition
    as production code so the window counters / DTO half_open fields are in
    the realistic state the open-check tests assume.
    """
    repo.atomic_force_open(service_name, reason="test_seed", controlled_by_id=None)
    allowed, prev, new = repo.try_acquire_half_open_slot(
        service_name, limit=10, stuck_timeout_seconds=60
    )
    assert allowed is True
    assert prev == CircuitBreakerStateEnum.OPEN.value
    assert new == CircuitBreakerStateEnum.HALF_OPEN.value


# =============================================================================
# CircuitBreakerOpenAttempt dataclass contract
# =============================================================================


class TestCircuitBreakerOpenAttemptContract:
    """Dataclass shape — frozen, slots, named-field access (656 D7).

    Symmetric mirror of ``CircuitBreakerCloseAttempt``.
    """

    def test_dataclass_is_frozen(self):
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerOpenAttempt(state=state, did_open=True)

        with pytest.raises(dataclasses.FrozenInstanceError):
            attempt.did_open = False  # type: ignore[misc]

    def test_dataclass_has_slots(self):
        # ``slots=True`` removes ``__dict__``; assigning a new attribute raises.
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerOpenAttempt(state=state, did_open=False)

        assert not hasattr(attempt, "__dict__")

    def test_named_field_access(self):
        state = CircuitBreakerStateData(service_name="svc")
        attempt = CircuitBreakerOpenAttempt(state=state, did_open=True)

        assert attempt.state is state
        assert attempt.did_open is True


# =============================================================================
# InMemory record_failure_with_open_check — atomic path
# =============================================================================


class TestRecordFailureWithOpenCheckBehavior:
    """Atomic record-failure + HALF_OPEN -> OPEN transition under one lock."""

    def test_half_open_transitions_to_open_with_did_open_true(self):
        # Given: HALF_OPEN with a primed failure window.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "payment.charge"
        _open_then_half_open(repo, service_name)
        repo.record_failure(service_name)

        # When: a HALF_OPEN failure arrives.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: transitions to OPEN, did_open True, counters zeroed,
        # opened_at set (the writeback depends on it).
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value
        assert attempt.state.failure_count == 0
        assert attempt.state.success_count == 0
        assert attempt.state.half_open_request_count == 0
        assert attempt.state.half_open_window_started_at is None
        assert attempt.state.opened_at is not None

    def test_half_open_zeroes_counters_in_same_lock_acquire(self):
        # Given: HALF_OPEN with logged failures.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)
        for _ in range(3):
            repo.record_failure(service_name)
        assert repo.get_by_service_name(service_name).failure_count > 0

        # When: the atomic re-open fires.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: the re-open transition and the counter reset commit together.
        assert attempt.did_open is True
        assert attempt.state.failure_count == 0
        assert attempt.state.success_count == 0

        stored = repo.get_by_service_name(service_name)
        assert stored.failure_count == 0
        assert stored.success_count == 0

    def test_open_state_returns_did_open_false_no_transition(self):
        # Given: a CB already OPEN.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        repo.atomic_force_open(service_name, reason="seed", controlled_by_id=None)

        # When: open-check called while OPEN.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: no second re-open — only HALF_OPEN re-opens.
        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value

    def test_closed_state_returns_did_open_false(self):
        # Given: a fresh CB in CLOSED.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        repo.get_or_create(service_name)  # defaults to CLOSED

        # When.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: stays CLOSED, did_open False (re-open is HALF_OPEN-only).
        assert attempt.did_open is False
        assert attempt.state.state == CircuitBreakerStateEnum.CLOSED.value

    def test_stale_view_second_caller_observes_did_open_false(self):
        # Given: HALF_OPEN; the first caller re-opens.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)
        first = repo.record_failure_with_open_check(service_name)
        assert first.did_open is True

        # When: a second caller (state is now OPEN) records a failure.
        second = repo.record_failure_with_open_check(service_name)

        # Then: the re-open fired exactly once.
        assert second.did_open is False
        assert second.state.state == CircuitBreakerStateEnum.OPEN.value


# =============================================================================
# ABC default — race-unsafe implementation
# =============================================================================


class _MinimalABCRepoForOpenDefault(CircuitBreakerStateRepository):
    """Minimal subclass that does NOT override record_failure_with_open_check.

    Exercises the race-unsafe ABC default — verifies it delegates to
    ``update_state(OPEN)`` only when it observes HALF_OPEN.
    """

    def __init__(self, initial_state: str):
        self.state = CircuitBreakerStateData(
            service_name="svc",
            state=initial_state,
        )
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
                "opened_at": opened_at,
                "reset_half_open_count": reset_half_open_count,
            }
        )
        self.state = CircuitBreakerStateData(
            service_name=self.state.service_name,
            state=state,
            failure_count=failure_count if failure_count is not None else 0,
            success_count=success_count if success_count is not None else 0,
            opened_at=opened_at,
        )
        return True

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        return self.state

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        return self.state

    def set_manual_control(
        self, service_name, state, controlled_by_id=None, reason="", expires_at=None
    ):
        return True

    def clear_manual_control(self, service_name, preserve_reason=False):
        return True

    def get_all_states(self):
        return [self.state]

    def reset(self, service_name):
        return True

    def atomic_force_open(
        self, service_name, reason="", controlled_by_id=None, ttl_minutes=90
    ):
        return (True, "", "open")

    def atomic_force_close(self, service_name, reason="", controlled_by_id=None):
        return (True, "", "closed")

    def atomic_reset(self, service_name, reason="", controlled_by_id=None):
        return (True, "", "closed")

    def try_acquire_half_open_slot(self, service_name, limit, stuck_timeout_seconds):
        return (True, "open", "half_open")

    def reset_half_open_count(self, service_name):
        return None

    def delete_state(self, service_name):
        return True


class TestOpenCheckAbcDefault:
    """ABC default delegates to update_state(OPEN) on HALF_OPEN (656 D7)."""

    def test_half_open_calls_update_state_with_open(self):
        # Given: HALF_OPEN.
        repo = _MinimalABCRepoForOpenDefault(CircuitBreakerStateEnum.HALF_OPEN.value)

        # When.
        attempt = repo.record_failure_with_open_check("svc")

        # Then: a single update_state(OPEN, counters zeroed, reset_half_open).
        assert len(repo.update_state_calls) == 1
        call = repo.update_state_calls[0]
        assert call["state"] == CircuitBreakerStateEnum.OPEN.value
        assert call["failure_count"] == 0
        assert call["success_count"] == 0
        assert call["reset_half_open_count"] is True
        assert call["opened_at"] is not None
        assert attempt.did_open is True

    @pytest.mark.parametrize(
        "state",
        [
            CircuitBreakerStateEnum.CLOSED.value,
            CircuitBreakerStateEnum.OPEN.value,
        ],
    )
    def test_non_half_open_does_not_call_update_state(self, state):
        # Given: a non-HALF_OPEN state.
        repo = _MinimalABCRepoForOpenDefault(state)

        # When.
        attempt = repo.record_failure_with_open_check("svc")

        # Then: no update_state, did_open False.
        assert repo.update_state_calls == []
        assert attempt.did_open is False
        assert attempt.state.state == state


# =============================================================================
# Layered repo delegation (no L2)
# =============================================================================


class TestLayeredRepoOpenCheckDelegation:
    """Layered repo without L2 falls back to L1 and syncs to L2 (656 D7)."""

    def test_delegates_to_l1_and_returns_l1_attempt(self):
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )

        # Given: layered repo without L2 — exercises the L1 fallback path.
        repo = LayeredCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo._l1, service_name)

        # When.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: returned attempt reflects L1's atomic re-open decision.
        assert attempt.did_open is True
        assert attempt.state.state == CircuitBreakerStateEnum.OPEN.value

    def test_sync_to_l2_called_with_attempt_state(self):
        from baldur.adapters.memory.layered_repository import (
            LayeredCircuitBreakerStateRepository,
        )

        # Given: L1 in HALF_OPEN, no L2.
        repo = LayeredCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo._l1, service_name)
        repo._sync_to_l2_async = MagicMock()

        # When.
        attempt = repo.record_failure_with_open_check(service_name)

        # Then: sync receives the post-re-open state (not pre).
        repo._sync_to_l2_async.assert_called_once_with(service_name, attempt.state)
        synced_state = repo._sync_to_l2_async.call_args[0][1]
        assert synced_state.state == CircuitBreakerStateEnum.OPEN.value


# =============================================================================
# Race tests — multi-threaded barrier
# =============================================================================


class TestRecordFailureOpenRaceBarrier:
    """Multi-threaded race: exactly one thread sees did_open=True."""

    @pytest.mark.parametrize("n_threads", [2, 4, 8, 16])
    def test_only_one_thread_re_opens_across_n_callers(self, n_threads: int):
        # Given: HALF_OPEN. All N threads race to record a failure. With the
        # atomic open-check, only the caller that performed the re-open under
        # the lock should see did_open=True.
        repo = InMemoryCircuitBreakerStateRepository()
        service_name = "svc"
        _open_then_half_open(repo, service_name)

        barrier = threading.Barrier(n_threads)
        results: list[CircuitBreakerOpenAttempt] = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            attempt = repo.record_failure_with_open_check(service_name)
            with results_lock:
                results.append(attempt)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Then: exactly one thread performed the re-open.
        open_count = sum(1 for r in results if r.did_open)
        assert open_count == 1

        # Final storage state is OPEN.
        final = repo.get_by_service_name(service_name)
        assert final is not None
        assert final.state == CircuitBreakerStateEnum.OPEN.value


# =============================================================================
# Service-level emit gate (D7 / #498)
# =============================================================================


class TestRecordFailureOpenEmitGate:
    """Service emits CIRCUIT_BREAKER_OPENED exactly once across N failures."""

    def _build_service_with_mocked_emit(self, did_open_pattern: list[bool]):
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
        )
        repo.get_or_create = Mock(return_value=half_open_state)
        repo.update_state = Mock(return_value=True)
        open_state = CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.OPEN.value,
        )
        attempts = [
            CircuitBreakerOpenAttempt(state=open_state, did_open=did_open)
            for did_open in did_open_pattern
        ]
        repo.record_failure_with_open_check = Mock(side_effect=attempts)

        service = CircuitBreakerService(config=config, repository=repo)
        service._emit_event = MagicMock()
        return service, repo

    def _half_open_hint(self) -> CircuitBreakerStateData:
        return CircuitBreakerStateData(
            service_name="svc",
            state=CircuitBreakerStateEnum.HALF_OPEN.value,
        )

    def test_emit_fires_only_when_did_open_true(self):
        # Given: mock repo returns did_open=True once then False.
        service, repo = self._build_service_with_mocked_emit(
            did_open_pattern=[True, False, False, False]
        )

        # When: N HALF_OPEN failures land.
        for _ in range(4):
            service.record_failure("svc", hint_state=self._half_open_hint())

        # Then: repo called N times, but the OPENED emit fires exactly once.
        assert repo.record_failure_with_open_check.call_count == 4
        from baldur.services.event_bus import EventType

        opened_emits = [
            c
            for c in service._emit_event.call_args_list
            if c[0][0] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_emits) == 1

    def test_no_emit_when_all_did_open_false(self):
        # Given: every attempt returns did_open=False (a stale-view loser).
        service, _ = self._build_service_with_mocked_emit(
            did_open_pattern=[False, False, False]
        )

        # When.
        for _ in range(3):
            service.record_failure("svc", hint_state=self._half_open_hint())

        # Then: no CIRCUIT_BREAKER_OPENED emits at all.
        from baldur.services.event_bus import EventType

        opened_emits = [
            c
            for c in service._emit_event.call_args_list
            if c[0][0] == EventType.CIRCUIT_BREAKER_OPENED
        ]
        assert len(opened_emits) == 0
