"""
In-Memory Circuit Breaker State Repository Implementation.

Thread-safe in-memory storage for circuit breaker states.
Includes L1+L2 Layered Storage with Drift Reconciliation support.

Note: This module has been refactored for better maintainability:
- DriftReconciler, DriftReconciliationResult → drift_reconciliation.py
- ShadowLogger, L2SyncFailureRecord → shadow_logger.py
- LayeredCircuitBreakerStateRepository → layered_repository.py
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

import structlog

from baldur.adapters.memory.base import _now

# Re-export for backward compatibility
from baldur.adapters.memory.drift_reconciliation import (
    DriftReconciler,
    DriftReconciliationRecord,
    DriftReconciliationResult,
    get_drift_reconciler,
)
from baldur.adapters.memory.layered_repository import (
    LayeredCircuitBreakerStateRepository,
)
from baldur.adapters.memory.shadow_logger import (
    L2SyncFailureRecord,
    ShadowLogger,
    get_shadow_logger,
)
from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    CircuitBreakerStateEnum,
    CircuitBreakerStateRepository,
)

logger = structlog.get_logger()


class InMemoryCircuitBreakerStateRepository(CircuitBreakerStateRepository):
    """
    In-memory implementation of CircuitBreakerStateRepository.

    Thread-safe storage for circuit breaker states in memory.

    When sliding_window_size is specified, record_failure() / record_success()
    perform Ring Buffer-based Sliding Window counting.
    failure_count and success_count reflect the count within the most recent N calls.
    """

    def __init__(self, sliding_window_size: int = 100):
        self._storage: dict[str, CircuitBreakerStateData] = {}
        self._next_id = 1
        self._lock = threading.RLock()  # RLock for reentrant calls

        # Sliding Window: per-service ring buffer (True=success, False=failure)
        self._sliding_window_size = sliding_window_size
        self._call_windows: dict[str, deque[bool]] = {}

        # 490 D1: per-name incremental counters parallel to _call_windows.
        # Maintains the invariant `_success_cnt[name] + _failure_cnt[name]
        # == len(_call_windows[name])` so record_success / record_failure can
        # avoid the O(W) sum() pass that previously inflated lock hold time.
        self._failure_cnt: dict[str, int] = {}
        self._success_cnt: dict[str, int] = {}

        # 476: marker for the most recent try_acquire_half_open_slot result.
        # Read by LayeredCircuitBreakerStateRepository to emit the stuck-recovery
        # observability counter. Values: "transition" | "increment" | "rejected"
        # | "stuck_recovery" | "no_op" | "" (no acquire attempted yet).
        self._last_acquire_marker: str = ""

    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        """Get circuit breaker state by service name."""
        with self._lock:
            return self._storage.get(service_name)

    def _get_or_create_unlocked(self, service_name: str) -> CircuitBreakerStateData:
        """Get or create a circuit breaker state. Caller MUST hold self._lock.

        Extracted from get_or_create() to let lock-holding hot-path callers
        (record_success / record_failure) skip the redundant RLock reentry.
        Naming follows _get_or_create_window() precedent in this file.
        """
        # 490 D2/D5 + #436: extracted to avoid redundant RLock reentry on the hot path.
        if service_name not in self._storage:
            state = CircuitBreakerStateData(
                id=self._next_id,
                service_name=service_name,
                state=CircuitBreakerStateEnum.CLOSED.value,
                created_at=_now(),
                updated_at=_now(),
            )
            self._storage[service_name] = state
            self._next_id += 1
        return self._storage[service_name]

    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        """Get or create a circuit breaker state.

        Double-checked locking — steady-state callers (entry exists) bypass
        the RLock entirely and pay only a GIL-atomic dict.get. Only the
        first-call create needs the lock. Mirrors the precedent in
        ``protect.py`` and the project-wide policy in ``factory/base.py``
        ("read-only dict operations rely on CPython GIL atomicity").

        Removes the surviving read-path acquire that was the contention
        floor under high concurrency.
        """
        # #490 / plan §436 (Cat 7A.6): drop the read-path acquire that left the
        # N=100 contention floor.
        cached = self._storage.get(service_name)
        if cached is not None:
            return cached
        with self._lock:
            return self._get_or_create_unlocked(service_name)

    def update_state(
        self,
        service_name: str,
        state: str,
        failure_count: int | None = None,
        success_count: int | None = None,
        opened_at: datetime | None = None,
        last_failure_at: datetime | None = None,
        half_open_request_count: int | None = None,
        reset_half_open_count: bool = False,
    ) -> bool:
        """Update circuit breaker state."""
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return False

            if reset_half_open_count:
                resolved_half_open_count = 0
                resolved_window_started_at: datetime | None = None
            elif half_open_request_count is not None:
                resolved_half_open_count = half_open_request_count
                resolved_window_started_at = entry.half_open_window_started_at
            else:
                resolved_half_open_count = entry.half_open_request_count
                resolved_window_started_at = entry.half_open_window_started_at

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=state,
                failure_count=(
                    failure_count if failure_count is not None else entry.failure_count
                ),
                success_count=(
                    success_count if success_count is not None else entry.success_count
                ),
                last_failure_at=(
                    last_failure_at
                    if last_failure_at is not None
                    else entry.last_failure_at
                ),
                opened_at=opened_at if opened_at is not None else entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=resolved_half_open_count,
                half_open_window_started_at=resolved_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return True

    def increment_failure_count(
        self,
        service_name: str,
        last_failure_at: datetime | None = None,
    ) -> int:
        """Increment failure count."""
        with self._lock:
            entry = self.get_or_create(service_name)
            new_count = entry.failure_count + 1

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=new_count,
                success_count=entry.success_count,
                last_failure_at=last_failure_at or _now(),
                opened_at=entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return new_count

    def reset_counts(self, service_name: str) -> bool:
        """Reset failure/success counts and clear the OPEN-era timestamp.

        ``opened_at`` is cleared alongside the counters so a CLOSED
        DTO does not carry a stale OPEN-era timestamp. The L2-authoritative
        close path in ``LayeredCircuitBreakerStateRepository`` invokes this
        before transitioning L1 to CLOSED.
        """
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return False

            self._clear_window(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=0,
                success_count=0,
                last_failure_at=entry.last_failure_at,
                opened_at=None,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return True

    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        """Set manual control override."""
        with self._lock:
            entry = self.get_or_create(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=state,
                failure_count=entry.failure_count,
                success_count=entry.success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=(
                    _now()
                    if state == CircuitBreakerStateEnum.OPEN.value
                    else entry.opened_at
                ),
                manually_controlled=True,
                controlled_by_id=controlled_by_id,
                control_reason=reason,
                manual_override_expires_at=expires_at,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return True

    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        """Clear manual control override.

        Only the manual-control flag is cleared. state and the counters
        (failure_count, success_count) are not modified. If a state transition is
        needed, the caller must invoke update_state first.
        """
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return False

            self._clear_window(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=entry.failure_count,
                success_count=entry.success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=entry.opened_at,
                manually_controlled=False,
                controlled_by_id=None,
                control_reason=entry.control_reason if preserve_reason else "",
                manual_override_expires_at=None,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return True

    def _get_or_create_window(self, service_name: str) -> deque[bool]:
        """Get or create the per-service Sliding Window ring buffer."""
        if service_name not in self._call_windows:
            self._call_windows[service_name] = deque(
                maxlen=self._sliding_window_size,
            )
        return self._call_windows[service_name]

    def _clear_window(self, service_name: str) -> None:
        """Reset the per-service Sliding Window and incremental counters.

        Reset symmetry: zeroing _failure_cnt / _success_cnt alongside
        the deque clear maintains the invariant
        ``_success_cnt[n] + _failure_cnt[n] == len(_call_windows[n])`` at every
        reset site that retains the CB entry. Terminal sites (delete / clear)
        pop from the dicts entirely.
        """
        # 490 D6: keep counters in sync with the window on reset.
        if service_name in self._call_windows:
            self._call_windows[service_name].clear()
            self._failure_cnt[service_name] = 0
            self._success_cnt[service_name] = 0

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        """Record a failure and return updated state.

        Records a failure into the Sliding Window ring buffer, then updates
        state using the in-window failure/success counts.
        """
        with self._lock:
            entry = self._get_or_create_unlocked(service_name)

            # 490 D1/D3: eviction-aware incremental update — peek the oldest
            # slot before append() (deque(maxlen=W) silently evicts on
            # overflow), decrement that slot's contribution, then append and
            # increment the new value. O(1) replaces the prior O(W) sum().
            window = self._get_or_create_window(service_name)
            if service_name not in self._failure_cnt:
                self._failure_cnt[service_name] = 0
                self._success_cnt[service_name] = 0
            if len(window) == window.maxlen:
                evicted = window[0]
                if evicted:
                    self._success_cnt[service_name] -= 1
                else:
                    self._failure_cnt[service_name] -= 1
            window.append(False)  # False = failure
            self._failure_cnt[service_name] += 1

            failure_count = self._failure_cnt[service_name]
            success_count = self._success_cnt[service_name]

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=failure_count,
                success_count=success_count,
                last_failure_at=_now(),
                opened_at=entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return updated

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        """Record a success and return updated state.

        Records a success into the Sliding Window ring buffer, then updates
        state using the in-window failure/success counts.
        """
        with self._lock:
            entry = self._get_or_create_unlocked(service_name)

            # 490 D1/D3: see record_failure() for eviction-aware update rationale.
            window = self._get_or_create_window(service_name)
            if service_name not in self._failure_cnt:
                self._failure_cnt[service_name] = 0
                self._success_cnt[service_name] = 0
            if len(window) == window.maxlen:
                evicted = window[0]
                if evicted:
                    self._success_cnt[service_name] -= 1
                else:
                    self._failure_cnt[service_name] -= 1
            window.append(True)  # True = success
            self._success_cnt[service_name] += 1

            failure_count = self._failure_cnt[service_name]
            success_count = self._success_cnt[service_name]

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=failure_count,
                success_count=success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return updated

    def record_success_with_close_check(
        self,
        service_name: str,
        success_threshold: int,
    ) -> CircuitBreakerCloseAttempt:
        """Atomic record-success + threshold-check + close transition.

        Whole sequence executes under `self._lock`: eviction-aware window
        increment, threshold check, and (if crossed from HALF_OPEN) the
        close transition with `_clear_window` are one critical section.
        Closes the TOCTOU race where multiple stale-view callers each pass
        the threshold check and emit duplicate `CIRCUIT_BREAKER_CLOSED`
        events for the same logical recovery.
        """
        half_open_state = CircuitBreakerStateEnum.HALF_OPEN.value
        closed_state = CircuitBreakerStateEnum.CLOSED.value
        with self._lock:
            entry = self._get_or_create_unlocked(service_name)

            # 490 D1/D3: eviction-aware incremental update — see record_failure().
            window = self._get_or_create_window(service_name)
            if service_name not in self._failure_cnt:
                self._failure_cnt[service_name] = 0
                self._success_cnt[service_name] = 0
            if len(window) == window.maxlen:
                evicted = window[0]
                if evicted:
                    self._success_cnt[service_name] -= 1
                else:
                    self._failure_cnt[service_name] -= 1
            window.append(True)
            self._success_cnt[service_name] += 1

            failure_count = self._failure_cnt[service_name]
            success_count = self._success_cnt[service_name]

            should_close = (
                entry.state == half_open_state and success_count >= success_threshold
            )

            if should_close:
                # 497 D1 + G1 + G2: close transition and window clear must commit
                # in the SAME lock acquire as the success-increment so the
                # window-derived invariant `_success_cnt[n] + _failure_cnt[n]
                # == len(_call_windows[n])` (490 D6) holds across CLOSED, and
                # no subsequent record_success caller observes a stale
                # half_open + above-threshold success_count.
                self._clear_window(service_name)
                updated = CircuitBreakerStateData(
                    id=entry.id,
                    service_name=service_name,
                    state=closed_state,
                    failure_count=0,
                    success_count=0,
                    last_failure_at=entry.last_failure_at,
                    opened_at=None,
                    manually_controlled=entry.manually_controlled,
                    controlled_by_id=entry.controlled_by_id,
                    control_reason=entry.control_reason,
                    manual_override_expires_at=entry.manual_override_expires_at,
                    half_open_request_count=0,
                    half_open_window_started_at=None,
                    created_at=entry.created_at,
                    updated_at=_now(),
                )
                self._storage[service_name] = updated
                return CircuitBreakerCloseAttempt(state=updated, did_close=True)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=entry.state,
                failure_count=failure_count,
                success_count=success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=entry.half_open_request_count,
                half_open_window_started_at=entry.half_open_window_started_at,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return CircuitBreakerCloseAttempt(state=updated, did_close=False)

    def record_failure_with_open_check(
        self,
        service_name: str,
    ) -> CircuitBreakerOpenAttempt:
        """Atomic record-failure + HALF_OPEN -> OPEN re-open transition.

        Whole sequence executes under `self._lock`: state read, and (if the
        state is HALF_OPEN) the re-open transition with `_clear_window` are one
        critical section. Closes the TOCTOU race where multiple stale-view
        callers each read HALF_OPEN and emit duplicate `CIRCUIT_BREAKER_OPENED`
        events for the same logical re-open. Symmetric mirror of
        `record_success_with_close_check`; a single HALF_OPEN failure re-opens
        unconditionally (no threshold).
        """
        half_open_state = CircuitBreakerStateEnum.HALF_OPEN.value
        open_state = CircuitBreakerStateEnum.OPEN.value
        with self._lock:
            entry = self._get_or_create_unlocked(service_name)

            if entry.state != half_open_state:
                return CircuitBreakerOpenAttempt(state=entry, did_open=False)

            # HALF_OPEN failure: re-open and clear the window/counters in the
            # SAME lock acquire as the state read so no concurrent caller
            # observes a stale half_open and emits a duplicate OPEN.
            self._clear_window(service_name)
            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=open_state,
                failure_count=0,
                success_count=0,
                last_failure_at=entry.last_failure_at,
                opened_at=_now(),
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return CircuitBreakerOpenAttempt(state=updated, did_open=True)

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        """Get all circuit breaker states."""
        with self._lock:
            return list(self._storage.values())

    def reset(self, service_name: str) -> bool:
        """Reset circuit breaker to initial closed state."""
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return False

            self._clear_window(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=CircuitBreakerStateEnum.CLOSED.value,
                failure_count=0,
                success_count=0,
                last_failure_at=None,
                opened_at=None,
                manually_controlled=False,
                controlled_by_id=None,
                control_reason="",
                manual_override_expires_at=None,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return True

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int = 90,
    ) -> tuple[bool, str, str]:
        """Atomically force open a circuit breaker."""
        with self._lock:
            entry = self.get_or_create(service_name)
            previous_state = entry.state

            expires_at = (
                _now() + timedelta(minutes=ttl_minutes) if ttl_minutes > 0 else None
            )

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=CircuitBreakerStateEnum.OPEN.value,
                failure_count=entry.failure_count,
                success_count=entry.success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=_now(),
                manually_controlled=True,
                controlled_by_id=controlled_by_id,
                control_reason=reason,
                manual_override_expires_at=expires_at,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return (True, previous_state, CircuitBreakerStateEnum.OPEN.value)

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """Atomically force close a circuit breaker."""
        with self._lock:
            entry = self.get_or_create(service_name)
            previous_state = entry.state

            # 490 D6: DTO counters reset to 0 — sync the sliding window and
            # incremental counters to keep the window-derived invariant.
            self._clear_window(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=CircuitBreakerStateEnum.CLOSED.value,
                failure_count=0,
                success_count=0,
                last_failure_at=entry.last_failure_at,
                opened_at=None,
                manually_controlled=True,
                controlled_by_id=controlled_by_id,
                control_reason=reason,
                manual_override_expires_at=None,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return (True, previous_state, CircuitBreakerStateEnum.CLOSED.value)

    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple[bool, str, str]:
        """Atomically reset a circuit breaker to initial state."""
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return (False, "", "")

            previous_state = entry.state

            # 490 D6: DTO counters reset to 0 — sync the sliding window and
            # incremental counters to keep the window-derived invariant.
            self._clear_window(service_name)

            updated = CircuitBreakerStateData(
                id=entry.id,
                service_name=service_name,
                state=CircuitBreakerStateEnum.CLOSED.value,
                failure_count=0,
                success_count=0,
                last_failure_at=None,
                opened_at=None,
                manually_controlled=False,
                controlled_by_id=None,
                control_reason=reason,
                manual_override_expires_at=None,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )
            self._storage[service_name] = updated
            return (True, previous_state, CircuitBreakerStateEnum.CLOSED.value)

    def try_acquire_half_open_slot(
        self,
        service_name: str,
        limit: int,
        stuck_timeout_seconds: int,
    ) -> tuple[bool, str, str]:
        """Atomic HALF_OPEN slot acquisition under RLock."""
        half_open_state = CircuitBreakerStateEnum.HALF_OPEN.value
        open_state = CircuitBreakerStateEnum.OPEN.value

        with self._lock:
            entry = self.get_or_create(service_name)
            current_state = entry.state
            current_count = entry.half_open_request_count
            window_started_at = entry.half_open_window_started_at
            now_ts = _now()

            if current_state == half_open_state and current_count >= limit:
                window_age = (
                    (now_ts - window_started_at).total_seconds()
                    if window_started_at is not None
                    else float("inf")
                )
                if window_age > stuck_timeout_seconds:
                    # 490 D6: DTO success_count reset to 0 — clear the
                    # window so window-derived invariant matches the DTO.
                    self._clear_window(service_name)
                    self._storage[service_name] = CircuitBreakerStateData(
                        id=entry.id,
                        service_name=service_name,
                        state=half_open_state,
                        failure_count=entry.failure_count,
                        success_count=0,
                        last_failure_at=entry.last_failure_at,
                        opened_at=entry.opened_at,
                        manually_controlled=entry.manually_controlled,
                        controlled_by_id=entry.controlled_by_id,
                        control_reason=entry.control_reason,
                        manual_override_expires_at=entry.manual_override_expires_at,
                        half_open_request_count=1,
                        half_open_window_started_at=now_ts,
                        created_at=entry.created_at,
                        updated_at=now_ts,
                    )
                    self._last_acquire_marker = "stuck_recovery"
                    return (True, half_open_state, half_open_state)

                self._last_acquire_marker = "rejected"
                return (False, half_open_state, half_open_state)

            if current_state == open_state:
                # 490 D6: DTO success_count reset to 0 on OPEN→HALF_OPEN
                # transition — clear the window so the next record_*'s
                # incremental counters start from a clean slate.
                self._clear_window(service_name)
                self._storage[service_name] = CircuitBreakerStateData(
                    id=entry.id,
                    service_name=service_name,
                    state=half_open_state,
                    failure_count=entry.failure_count,
                    success_count=0,
                    last_failure_at=entry.last_failure_at,
                    opened_at=entry.opened_at,
                    manually_controlled=entry.manually_controlled,
                    controlled_by_id=entry.controlled_by_id,
                    control_reason=entry.control_reason,
                    manual_override_expires_at=entry.manual_override_expires_at,
                    half_open_request_count=1,
                    half_open_window_started_at=now_ts,
                    created_at=entry.created_at,
                    updated_at=now_ts,
                )
                self._last_acquire_marker = "transition"
                return (True, open_state, half_open_state)

            if current_state == half_open_state and current_count < limit:
                self._storage[service_name] = CircuitBreakerStateData(
                    id=entry.id,
                    service_name=service_name,
                    state=half_open_state,
                    failure_count=entry.failure_count,
                    success_count=entry.success_count,
                    last_failure_at=entry.last_failure_at,
                    opened_at=entry.opened_at,
                    manually_controlled=entry.manually_controlled,
                    controlled_by_id=entry.controlled_by_id,
                    control_reason=entry.control_reason,
                    manual_override_expires_at=entry.manual_override_expires_at,
                    half_open_request_count=current_count + 1,
                    half_open_window_started_at=window_started_at,
                    created_at=entry.created_at,
                    updated_at=now_ts,
                )
                self._last_acquire_marker = "increment"
                return (True, half_open_state, half_open_state)

            self._last_acquire_marker = "no_op"
            return (False, current_state, current_state)

    def reset_half_open_count(self, service_name: str) -> None:
        """Reset HALF_OPEN counter and clear window watermark."""
        # 476 G8: watermark reset paired with the HALF_OPEN counter.
        with self._lock:
            entry = self._storage.get(service_name)
            if entry is None:
                return
            self._storage[service_name] = CircuitBreakerStateData(
                id=entry.id,
                service_name=entry.service_name,
                state=entry.state,
                failure_count=entry.failure_count,
                success_count=entry.success_count,
                last_failure_at=entry.last_failure_at,
                opened_at=entry.opened_at,
                manually_controlled=entry.manually_controlled,
                controlled_by_id=entry.controlled_by_id,
                control_reason=entry.control_reason,
                manual_override_expires_at=entry.manual_override_expires_at,
                half_open_request_count=0,
                half_open_window_started_at=None,
                created_at=entry.created_at,
                updated_at=_now(),
            )

    def get_open_states(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Get OPEN circuit breaker states, oldest-first."""
        with self._lock:
            open_states = [
                entry
                for entry in self._storage.values()
                if entry.state == CircuitBreakerStateEnum.OPEN.value
            ]
            open_states.sort(
                key=lambda s: s.opened_at or datetime.min.replace(tzinfo=_now().tzinfo)
            )
            if limit is not None:
                return open_states[:limit]
            return open_states

    def get_all_open(self) -> list[CircuitBreakerStateData]:
        """Get all open circuit breakers."""
        with self._lock:
            return [
                entry
                for entry in self._storage.values()
                if entry.state == CircuitBreakerStateEnum.OPEN.value
            ]

    def delete(self, service_name: str) -> bool:
        """Delete a circuit breaker state."""
        with self._lock:
            if service_name in self._storage:
                del self._storage[service_name]
                # 490 D6 (terminal): pop from parallel dicts entirely.
                # The pre-existing `_clear_window(name)` call only emptied
                # the deque, leaving an empty deque jammed in
                # `_call_windows[name]` — fixed here as a drive-by.
                self._call_windows.pop(service_name, None)
                self._failure_cnt.pop(service_name, None)
                self._success_cnt.pop(service_name, None)
                return True
            return False

    def delete_state(self, service_name: str) -> bool:
        """Delete circuit breaker state (alias for delete)."""
        return self.delete(service_name)

    def clear(self) -> None:
        """Clear all entries (for testing)."""
        with self._lock:
            self._storage.clear()
            self._call_windows.clear()
            # 490 D6 (terminal): clear parallel counter dicts.
            self._failure_cnt.clear()
            self._success_cnt.clear()
            self._next_id = 1


# =============================================================================
# Backward Compatibility Exports
# =============================================================================

__all__ = [
    # Main repository
    "InMemoryCircuitBreakerStateRepository",
    "LayeredCircuitBreakerStateRepository",
    # Drift reconciliation (re-exported from drift_reconciliation.py)
    "DriftReconciliationResult",
    "DriftReconciliationRecord",
    "DriftReconciler",
    "get_drift_reconciler",
    # Shadow logger (re-exported from shadow_logger.py)
    "L2SyncFailureRecord",
    "ShadowLogger",
    "get_shadow_logger",
]
