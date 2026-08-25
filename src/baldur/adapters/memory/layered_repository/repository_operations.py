"""
Repository Operations Mixin.

Provides CircuitBreakerStateRepository interface implementation with L1 priority.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import (
    CIRCUIT_BREAKER_PINNED_TOKEN,
    CircuitBreakerCloseAttempt,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
    pinned_trip_attempt,
)

if TYPE_CHECKING:
    from concurrent.futures import Future, ThreadPoolExecutor

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.core.rate_limiting import CooldownGate
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

logger = structlog.get_logger()

# 771 D8: how long the reject-path convergence lane waits before re-attempting
# the same service. The first detection schedules immediately; this paces
# retries only, with the L2-recovery reconciliation pass as the backstop.
_REJECT_CONVERGENCE_COOLDOWN_SECONDS = 30.0

# 771 D10: process-wide bound on convergence tasks resident in the shared L2
# executor. Each task performs its I/O inline, so this is exactly how many
# pool slots the lane can occupy no matter how many services contradict at
# once; convergence of a backlog serialises instead of crowding out the
# request-path L2 calls that share the pool.
_REJECT_CONVERGENCE_MAX_IN_FLIGHT = 2

_reject_convergence_slots = threading.BoundedSemaphore(
    _REJECT_CONVERGENCE_MAX_IN_FLIGHT
)


def _release_reject_convergence_slot(_future: Future) -> None:
    """Return one in-flight permit — the lane's single release point.

    Registered as the task's done-callback, which fires on normal completion,
    on a task exception, and on cancellation (executor shutdown cancels queued
    futures). The task body must not release: a permit leaked twice would
    disable the lane for the rest of the process with no metric, log, or retry
    to show for it.
    """
    try:
        _reject_convergence_slots.release()
    except ValueError:
        logger.warning("layered_repo.reject_path_convergence_permit_over_released")


class RepositoryOperationsMixin:
    """Mixin providing repository interface operations."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # LayeredRepositoryBase and sibling mixins
        # (L2SyncMixin, ErrorHandlingMixin). See
        # LayeredCircuitBreakerStateRepository for the assembled class.
        _l1: InMemoryCircuitBreakerStateRepository
        _l2: CircuitBreakerStateRepository | None
        _l2_healthy: bool
        _reject_convergence_cooldown: CooldownGate

        def _get_timeout_seconds(self) -> float: ...
        def _get_executor(self) -> ThreadPoolExecutor: ...
        def _repair_row_to_l2_inline(self, service_name: str) -> bool | None: ...
        def _sync_to_l2_async(self, service_name: str) -> None: ...
        def _sync_to_l2_with_timeout(
            self,
            service_name: str,
            state: CircuitBreakerStateData,
            skip_if_pinned: bool = False,
        ) -> bool: ...
        def _sync_pin_to_l2(
            self,
            service_name: str,
            write: Callable[[], Any],
            intended_state: str = "",
        ) -> bool: ...
        def _handle_l2_success(self, elapsed_ms: float) -> None: ...
        def _handle_l2_timeout(
            self, operation: str, service_name: str | None
        ) -> None: ...
        def _handle_l2_error(
            self,
            operation: str,
            service_name: str | None,
            error: Exception,
            intended_state: str = "",
        ) -> None: ...

    def get_by_service_name(self, service_name: str) -> CircuitBreakerStateData | None:
        """Look up in L1. If missing in L1, check L2 and cache into L1."""
        result = self._l1.get_by_service_name(service_name)

        if result is None and self._l2 and self._l2_healthy:
            timeout = self._get_timeout_seconds()
            start_time = time.perf_counter()

            try:
                executor = self._get_executor()
                future = executor.submit(self._l2.get_by_service_name, service_name)
                l2_result = future.result(timeout=timeout)

                if l2_result:
                    # Wholesale hydration of an absent L1 row: nothing local
                    # can be erased, so this lane carries the manual-control
                    # fields too — a Block placed elsewhere is enforced here
                    # from the first request onward.
                    self._l1.hydrate_snapshot(l2_result)
                    elapsed_ms = (time.perf_counter() - start_time) * 1000
                    self._handle_l2_success(elapsed_ms)
                    return self._l1.get_by_service_name(service_name)

            except FuturesTimeoutError:
                self._handle_l2_timeout("get", service_name)
            except Exception as e:
                self._handle_l2_error("get", service_name, e)

        return result

    def get_or_create(self, service_name: str) -> CircuitBreakerStateData:
        """L1 read-or-init. No L2 mirror (478 D2); flag-gated cold-start L2 read (656 D4).

        Default path (flag off): L1 read-or-init, no L2 touch — the admission
        hot path stays L1-only (#227 §7.4) and lock-free on an L1 hit (InMemory
        double-checked locking). read-or-init must not ``_sync_to_l2_async`` —
        it would clobber the Lua-atomic L2 state set by
        ``try_acquire_half_open_slot``. State mirroring belongs to the explicit
        write callers (update_state, record_*, set_*, atomic_*).

        656 D4 (flag on, ``cluster_state_propagation_enabled``): on an L1 miss,
        perform a bounded one-shot authoritative L2 read (reusing
        ``get_by_service_name``'s timeout-bounded executor fallback) so a freshly
        booted / never-hydrated worker rejects traffic the cluster already cut
        off — closing the #478 hydration-failure staleness window 479 left open.
        Reading (not writing) L2 does not clobber the Lua-atomic L2 state, so the
        478 D2 no-mirror invariant is preserved. This gate read is also the OSS
        behavioral consumer of the flag (G32 claim-wiring proof).
        """
        from baldur.settings.circuit_breaker import get_circuit_breaker_settings

        if get_circuit_breaker_settings().cluster_state_propagation_enabled:
            from_l2 = self.get_by_service_name(service_name)
            if from_l2 is not None:
                return from_l2
        return self._l1.get_or_create(service_name)

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
        clear_opened_at: bool = False,
        skip_if_pinned: bool = False,
    ) -> bool:
        """Update L1, then asynchronously synchronize to L2 (476 D9 reset flag forwarded).

        Both write directives are forwarded to L1; the mirror derives its own
        ``clear_opened_at`` from the row it reads and always passes the pin
        guard, so neither needs to be threaded through the async hand-off.
        """
        result = self._l1.update_state(
            service_name=service_name,
            state=state,
            failure_count=failure_count,
            success_count=success_count,
            opened_at=opened_at,
            last_failure_at=last_failure_at,
            half_open_request_count=half_open_request_count,
            reset_half_open_count=reset_half_open_count,
            clear_opened_at=clear_opened_at,
            skip_if_pinned=skip_if_pinned,
        )

        if result:
            self._sync_to_l2_async(service_name)

            # 476 D9: forward the counter-reset directive to L2 explicitly so
            # the cluster-wide HALF_OPEN counter clears in the same transition
            # round-trip — _sync_to_l2_async only mirrors the L1 snapshot and
            # does not invoke the L2 reset_half_open_count primitive.
            if reset_half_open_count and self._l2:
                try:
                    self._l2.reset_half_open_count(service_name)
                except Exception as e:
                    logger.warning(
                        "layered_repo.l2_reset_half_open_count_failed",
                        service_name=service_name,
                        error=str(e),
                    )

        return result

    def try_acquire_half_open_slot(
        self,
        service_name: str,
        limit: int,
        stuck_timeout_seconds: int,
    ) -> tuple[bool, str, str]:
        """L2-first synchronous HALF_OPEN slot acquisition (476 D1/D6/C1).

        Synchronous (NOT ``_sync_to_l2_async``) because §392 requires
        cluster-wide exact accounting at the CAS layer. On L2 timeout /
        unhealthy / exception, fall back to L1 (per-process best-effort) and
        emit ``baldur_circuit_breaker_half_open_degraded_mode_total`` so the
        relaxed contract is observable.

        After L2 succeeds with ``allowed=True``, synchronously writeback
        the L2-decided post-state to L1 (D6) so subsequent ``record_*``
        calls don't read stale L1=open while L2 says half_open. Writeback
        failures are logged (``circuit_breaker.l1_writeback_failed``) but
        never roll back the L2-authoritative decision.

        When L2 succeeds with ``allowed=False`` and answers ``closed`` while
        L1 still holds a non-closed state, the two layers contradict each
        other and every further request on this worker would be rejected on an
        answer it keeps discarding. That case hands off to the convergence
        lane (771 D1); the returned decision stays L2's, untouched.
        """
        if self._l2 and self._l2_healthy:
            timeout = self._get_timeout_seconds()
            start_time = time.perf_counter()

            try:
                executor = self._get_executor()
                future = executor.submit(
                    self._l2.try_acquire_half_open_slot,
                    service_name,
                    limit,
                    stuck_timeout_seconds,
                )
                allowed, prev_state, new_state = future.result(timeout=timeout)
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._handle_l2_success(elapsed_ms)

                marker = getattr(self._l2, "_last_acquire_marker", "")
                if marker == "stuck_recovery":
                    self._record_half_open_stuck_recovery(service_name)

                if allowed:
                    self._writeback_l2_state_to_l1(service_name, prev_state, new_state)
                self._maybe_schedule_reject_convergence(
                    service_name, allowed, new_state
                )

                return (allowed, prev_state, new_state)

            except FuturesTimeoutError:
                self._handle_l2_timeout("try_acquire_half_open_slot", service_name)
            except Exception as e:
                self._handle_l2_error("try_acquire_half_open_slot", service_name, e)

        # L2 unavailable / failed — fail-open to L1 (C1).
        self._record_half_open_degraded_mode(service_name)
        return self._l1.try_acquire_half_open_slot(
            service_name, limit, stuck_timeout_seconds
        )

    def reset_half_open_count(self, service_name: str) -> None:
        """Reset HALF_OPEN counter on L2 (cluster-wide source of truth)."""
        if self._l2:
            try:
                self._l2.reset_half_open_count(service_name)
            except Exception as e:
                logger.warning(
                    "layered_repo.l2_reset_half_open_count_failed",
                    service_name=service_name,
                    error=str(e),
                )
        # L1 is best-effort — bypass for symmetry with try_acquire
        # (L2-authoritative). L1 will catch up via the next L2 read or via
        # drift reconciliation if it diverges meaningfully.
        try:
            self._l1.reset_half_open_count(service_name)
        except Exception as e:
            logger.debug(
                "layered_repo.l1_reset_half_open_count_failed",
                service_name=service_name,
                error=str(e),
            )

    def _writeback_l2_state_to_l1(
        self, service_name: str, prev_state: str, new_state: str
    ) -> None:
        """Sync the L2-decided post-state back to L1 (476 D6 / G11)."""
        try:
            success_count_arg = (
                0 if (prev_state == "open" and new_state == "half_open") else None
            )
            # Ensure L1 entry exists before updating.
            self._l1.get_or_create(service_name)
            self._l1.update_state(
                service_name=service_name,
                state=new_state,
                success_count=success_count_arg,
            )
        except Exception as e:
            logger.warning(
                "circuit_breaker.l1_writeback_failed",
                service_name=service_name,
                prev_state=prev_state,
                new_state=new_state,
                error=str(e),
            )

    # =========================================================================
    # Reject-path convergence lane (771)
    #
    # A healthy L2 that rejects with ``closed`` against a non-closed L1 row is
    # answering a contradiction: the acquire keeps asking, keeps being told the
    # cluster is closed, and — before this lane — kept discarding the answer,
    # so one service stayed rejected on that worker until the breaker next
    # tripped cluster-wide, the worker restarted, or an operator resynced.
    # Detection is a tuple compare on the request path; the resolution runs on
    # the shared L2 executor.
    # =========================================================================

    def _maybe_schedule_reject_convergence(
        self, service_name: str, allowed: bool, new_state: str
    ) -> None:
        """Hand a reject-path contradiction to the convergence lane, if it is one.

        Ordered so the request path pays as little as possible: the tuple shape
        first, then the per-service cooldown, and only then the store-touching
        work, which continues inside ``_submit_reject_convergence`` behind the
        in-flight permit. A rejected service at several hundred requests per
        second would otherwise pay an in-memory-store lock acquisition on every
        single request — the exact read-path contention the store was reworked
        to remove — instead of once per cooldown window; and because a dropped
        detection deliberately leaves the cooldown unconsumed, the same holds
        while the lane is at its in-flight bound, so the permit gate too must
        come before the L1 read.

        The whole body is isolated: nothing here may raise into the acquire.
        Placed bare inside the acquire's ``try``, a scheduling failure (an
        executor shut down under the caller, say) would be caught by the L2
        error handler, fall through to the L1 fallback, turn an
        L2-authoritative rejection into a local admission, and tick the
        consecutive-failure count toward a quarantine L2 never earned.
        """
        try:
            if allowed or new_state != "closed":
                return
            if not self._should_schedule_reject_convergence(service_name):
                return
            self._submit_reject_convergence(service_name)
        except Exception as e:
            logger.debug(
                "layered_repo.reject_path_convergence_schedule_failed",
                service_name=service_name,
                error=str(e),
            )

    def _should_schedule_reject_convergence(self, service_name: str) -> bool:
        """Has this service's convergence cooldown elapsed?

        The lock-free read on the shared cooldown gate — the cheap check that
        keeps the L1 row read off the per-request path. The binding reservation
        is taken later, when the lane is about to submit.
        """
        return not self._reject_convergence_cooldown.is_suppressed(
            service_name, _REJECT_CONVERGENCE_COOLDOWN_SECONDS
        )

    def _submit_reject_convergence(self, service_name: str) -> None:
        """Take a permit, confirm the contradiction, then submit the task.

        The permit is taken first, without blocking, and ahead of the
        lock-taking L1 row read: a dropped detection deliberately leaves the
        cooldown unconsumed so the next rejected request retries, which means
        a cap-full window would otherwise re-pay that lock acquisition on
        every rejected request for every further stuck service. At the bound
        the detection is dropped before touching the store or any
        reservation. The reservation is what makes concurrent detections of
        the same service resolve to a single task.
        """
        if not _reject_convergence_slots.acquire(blocking=False):
            logger.debug(
                "layered_repo.reject_path_convergence_deferred",
                service_name=service_name,
                reason="max_in_flight",
            )
            return

        submitted = False
        try:
            l1_row = self._l1.get_by_service_name(service_name)
            if l1_row is None or l1_row.state == "closed":
                return
            reserved, _token = self._reject_convergence_cooldown.try_reserve(
                service_name, _REJECT_CONVERGENCE_COOLDOWN_SECONDS
            )
            if not reserved:
                return
            future = self._get_executor().submit(
                self._run_reject_path_convergence, service_name
            )
            future.add_done_callback(_release_reject_convergence_slot)
            submitted = True
        finally:
            # Every path that did not hand the permit to a done-callback gives
            # it back here — a confirm that found no contradiction, a lost
            # reservation race, or a submit that raised. The reservation itself
            # is deliberately not rolled back on a submit failure: an executor
            # that rejects a submit keeps rejecting them, and the cooldown is
            # what stops a rejected service from retrying once per request.
            if not submitted:
                _reject_convergence_slots.release()

    def _run_reject_path_convergence(self, service_name: str) -> str:
        """Resolve one reject-path contradiction. Returns the outcome name.

        Runs on the shared L2 executor and performs all of its L2 I/O inline on
        that thread — never a nested submit, which on a pool of one or two
        workers waits for a task that cannot start until this one returns.

        Outcomes: ``converged`` (the local row was moved to the store's closed
        state), ``repaired`` / ``repair_failed`` (the store had lost the row
        and this worker's state was mirrored back), ``skipped_pinned`` (a
        manual override in either layer), ``skipped`` (the store is
        quarantined, degraded, or unreadable) and ``noop`` (the two layers no
        longer disagree).
        """
        outcome = self._resolve_reject_path_convergence(service_name)
        self._record_reject_path_convergence(service_name, outcome)

        if outcome in ("converged", "repaired"):
            logger.info(
                "layered_repo.reject_path_convergence_applied",
                service_name=service_name,
                outcome=outcome,
            )
        else:
            logger.debug(
                "layered_repo.reject_path_convergence_noop",
                service_name=service_name,
                outcome=outcome,
            )
        return outcome

    def _resolve_reject_path_convergence(self, service_name: str) -> str:
        """Decide and apply the convergence direction for one service.

        The direction comes from the task's own fresh L2 read, which is also
        what tells a lost row apart from a genuine cluster close — the atomic
        acquire folds both into the same ``closed`` answer:

        - **row missing**: L2 is behind, so this worker's state is mirrored
          back to it. Protection is kept while the dependency may still be
          down, matching the direction the recovery-edge reconciliation
          already takes on a missing row.
        - **row present and closed**: L2 is authoritative, so the local row
          converges to closed — the same trust the record paths already extend
          to a healthy L2's ``closed`` answer. The reject path was the only
          one that did not.
        """
        l2 = self._l2
        if l2 is None or not self._l2_healthy or self._l2_backend_is_degraded():
            return "skipped"

        start_time = time.perf_counter()
        try:
            remote = l2.get_by_service_name(service_name)
        except Exception as e:
            self._handle_l2_error("reject_path_convergence", service_name, e)
            return "skipped"

        # A resilient backend answers a failed read from its process-local
        # fallback instead of raising, and switches itself to degraded before
        # returning. Anything it reported — "absent" included — is that
        # fallback's view rather than the store's, so the decision is dropped
        # instead of acted on: repairing against a false absence would write a
        # fabricated default row into the write-ahead log, and the replay could
        # erase a peer's pin once the store comes back.
        if self._l2_backend_is_degraded():
            return "skipped"

        self._handle_l2_success((time.perf_counter() - start_time) * 1000)

        try:
            if remote is None:
                repaired = self._repair_row_to_l2_inline(service_name)
                if repaired is None:
                    return "skipped_pinned"
                return "repaired" if repaired else "repair_failed"

            if remote.state != "closed":
                return "noop"

            if remote.manually_controlled:
                # A pinned remote row is delivered whole, pin fields included.
                # Copying its state alone would leave this worker unpinned and
                # free to record outcomes, re-trip, and mirror an OPEN over the
                # operator's still-active decision.
                applied = self._l1.hydrate_snapshot(
                    remote, skip_if_local_pin_active=True
                )
            else:
                applied = self._l1.converge_to_closed_unless_pinned(service_name)
            return "converged" if applied else "skipped_pinned"
        except Exception as e:
            logger.warning(
                "layered_repo.reject_path_convergence_failed",
                service_name=service_name,
                error=str(e),
            )
            return "skipped"

    def _l2_backend_is_degraded(self) -> bool:
        """Is the L2 store's resilient backend serving from its local fallback?

        Guards absence only, in both directions: an L2 without a backend
        attribute, or a backend that is some other object entirely, reads as
        not degraded and the caller proceeds as before. A real resilient
        backend answers without I/O.
        """
        l2_backend = getattr(self._l2, "_backend", None)
        return bool(
            l2_backend is not None and getattr(l2_backend, "is_degraded", False)
        )

    @staticmethod
    def _record_reject_path_convergence(service_name: str, outcome: str) -> None:
        """Increment the reject-path convergence counter (771 D9)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_reject_path_convergence,
            )

            record_reject_path_convergence(service_name, outcome)
        except ImportError:
            pass

    def apply_peer_cb_state(
        self,
        service_name: str,
        new_state: str,
        opened_at: datetime | None = None,
    ) -> bool:
        """Apply a peer worker's CB state transition to L1 ONLY (656 D2).

        The peer-side updater for cluster-wide OPEN/CLOSED propagation. Updates
        L1 only — never ``_sync_to_l2_async`` — because the emitting worker
        already owns the authoritative L2 write; mirroring back here would race
        that async write. Precedent: ``_writeback_l2_state_to_l1``.

        Idempotent by construction: applying a state L1 already holds is a
        no-op. Returns ``True`` iff L1 actually transitioned, so the listener
        can record the peer-propagation metric (``applied`` vs ``noop``).

        HALF_OPEN handling: if L1 is locally ``half_open`` (this worker holds a
        trial slot) and ``new_state == "open"``, L1 transitions to ``open`` —
        the local trial is abandoned (in-flight requests complete; new admission
        is cut), the safe response to a peer detecting failure. L2's half-open
        accounting is untouched (this is L1-only).

        Args:
            service_name: Circuit breaker identifier.
            new_state: ``"open"`` or ``"closed"`` (derived from the event type).
            opened_at: OPEN-era timestamp from the peer event (OPEN only;
                ignored / cleared for CLOSED).

        Returns:
            ``True`` iff L1 transitioned; ``False`` on an idempotent no-op.
        """
        current = self._l1.get_by_service_name(service_name)
        # An absent L1 entry resolves to the CLOSED default (get_or_create).
        current_state = current.state if current is not None else "closed"
        if current_state == new_state:
            return False

        self._l1.get_or_create(service_name)
        if new_state == "open":
            self._l1.update_state(
                service_name=service_name,
                state="open",
                failure_count=0,
                success_count=0,
                opened_at=opened_at,
                reset_half_open_count=True,
            )
        else:  # closed
            self._l1.reset_counts(service_name)
            self._l1.update_state(
                service_name=service_name,
                state="closed",
                reset_half_open_count=True,
            )
        return True

    @staticmethod
    def _record_half_open_degraded_mode(service_name: str) -> None:
        """Increment the degraded-mode counter (Stage 7 metric)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_half_open_degraded_mode,
            )

            record_half_open_degraded_mode(service_name)
        except ImportError:
            pass

    @staticmethod
    def _record_half_open_stuck_recovery(service_name: str) -> None:
        """Increment the stuck-recovery counter (Stage 7 metric)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_half_open_stuck_recovery,
            )

            record_half_open_stuck_recovery(service_name)
        except ImportError:
            pass

    def get_all_open(self) -> list[CircuitBreakerStateData]:
        """Look up open states in L1."""
        return self._l1.get_all_open()

    def delete(self, service_name: str) -> bool:
        """Delete from L1. Synchronize L2 as well."""
        result = self._l1.delete(service_name)

        if result and self._l2:
            try:
                self._l2.delete_state(service_name)
            except Exception:
                pass

        return result

    def clear(self) -> None:
        """Clear L1. Does not touch L2 (for tests)."""
        self._l1.clear()

    def record_failure(self, service_name: str) -> CircuitBreakerStateData:
        """Record a failure in L1, then synchronize to L2."""
        result = self._l1.record_failure(service_name)
        self._sync_to_l2_async(service_name)
        return result

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        """Record a success in L1, then synchronize to L2."""
        result = self._l1.record_success(service_name)
        self._sync_to_l2_async(service_name)
        return result

    def record_success_with_close_check(
        self,
        service_name: str,
        success_threshold: int,
    ) -> CircuitBreakerCloseAttempt:
        """L2-authoritative HALF_OPEN -> CLOSED close-check (498 D6).

        Routes the atomic close-decision to L2 (Redis Lua / SQL FOR UPDATE)
        so the cross-process exactly-one contract holds across gunicorn
        workers / K8s replicas. Mirrors ``try_acquire_half_open_slot``'s
        L2-authoritative pattern.

        Steps:
        1. If L2 healthy: submit ``L2.record_success_with_close_check`` via
           the timeout-bounded executor.
        2. Stale-L2 guard: if L2 returns state not in {half_open, closed},
           L2 is stale relative to the caller's HALF_OPEN expectation
           (prior ``try_acquire`` took the L1-fallback path; L2 never saw
           the OPEN->HALF_OPEN transition). Record degraded-mode metric
           and fall through to L1. Do NOT writeback the stale L2 state to
           L1 -- that would corrupt the local HALF_OPEN observation.
        3. On L2 success with state in {half_open, closed}: writeback to
           L1. For ``state=='closed'`` (both did_close=True winner AND
           did_close=False race-loser / post-crash convergence), call
           ``_l1.reset_counts(service_name)`` first to clear the sliding
           window before transitioning L1 to CLOSED -- the InMemory atomic
           override normally clears the window on close (490 D6 / 497 D1)
           and routing the decision to L2 bypasses that path. Then
           ``update_state(state='closed', reset_half_open_count=True)``.
           For ``state=='half_open'`` (non-close increment), writeback the
           new ``success_count`` to L1 without resetting counters.
        4. On L2 timeout / exception / unhealthy / None: record degraded-
           mode metric, delegate to ``_l1.record_success_with_close_check``
           and async-sync the resulting snapshot to L2 -- relaxed contract,
           identical to the prior single-process behavior.
        """
        if self._l2 and self._l2_healthy:
            timeout = self._get_timeout_seconds()
            start_time = time.perf_counter()

            try:
                executor = self._get_executor()
                future = executor.submit(
                    self._l2.record_success_with_close_check,
                    service_name,
                    success_threshold,
                )
                attempt = future.result(timeout=timeout)
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._handle_l2_success(elapsed_ms)

                returned_state = attempt.state.state
                if returned_state not in {"half_open", "closed"}:
                    # Stale-L2 guard: L2 disagrees with caller's HALF_OPEN
                    # expectation. Do NOT writeback to L1; fall back to L1's
                    # atomic close path.
                    self._record_close_check_degraded_mode(service_name)
                    return self._l1_fallback_close_check(
                        service_name, success_threshold
                    )

                self._writeback_close_check_to_l1(service_name, attempt)
                return attempt

            except FuturesTimeoutError:
                self._handle_l2_timeout("record_success_with_close_check", service_name)
            except Exception as e:
                self._handle_l2_error(
                    "record_success_with_close_check", service_name, e
                )

        # L2 unavailable / failed -- fall back to L1.
        self._record_close_check_degraded_mode(service_name)
        return self._l1_fallback_close_check(service_name, success_threshold)

    def _l1_fallback_close_check(
        self,
        service_name: str,
        success_threshold: int,
    ) -> CircuitBreakerCloseAttempt:
        """L1-authoritative fallback for record_success_with_close_check (498 D6 step 6)."""
        attempt = self._l1.record_success_with_close_check(
            service_name, success_threshold
        )
        self._sync_to_l2_async(service_name)
        return attempt

    def _writeback_close_check_to_l1(
        self,
        service_name: str,
        attempt: CircuitBreakerCloseAttempt,
    ) -> None:
        """Sync the L2-authoritative close-check decision to L1 (498 D6 step 3).

        - For ``state='closed'``: clear the L1 sliding window via
          ``reset_counts`` (covers both did_close=True winner and the
          did_close=False race-loser / post-crash convergence), then
          transition L1 to CLOSED with ``reset_half_open_count=True`` to
          clear the HALF_OPEN watermark. ``opened_at`` is cleared by
          ``reset_counts`` per D9.
        - For ``state='half_open'``: increment-only writeback; no window
          reset (the HALF_OPEN window is still active).

        Writeback failures are logged but do not roll back the
        L2-authoritative decision.
        """
        try:
            self._l1.get_or_create(service_name)
            if attempt.state.state == "closed":
                self._l1.reset_counts(service_name)
                self._l1.update_state(
                    service_name=service_name,
                    state="closed",
                    reset_half_open_count=True,
                )
            else:
                self._l1.update_state(
                    service_name=service_name,
                    state="half_open",
                    success_count=attempt.state.success_count,
                )
        except Exception as e:
            logger.warning(
                "circuit_breaker.l1_close_check_writeback_failed",
                service_name=service_name,
                returned_state=attempt.state.state,
                did_close=attempt.did_close,
                error=str(e),
            )

    @staticmethod
    def _record_close_check_degraded_mode(service_name: str) -> None:
        """Increment the close-check degraded-mode counter (498 D7)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_close_check_degraded_mode,
            )

            record_close_check_degraded_mode(service_name)
        except ImportError:
            pass

    def record_failure_with_open_check(
        self,
        service_name: str,
    ) -> CircuitBreakerOpenAttempt:
        """L2-authoritative HALF_OPEN -> OPEN re-open check (656 D7).

        Symmetric mirror of ``record_success_with_close_check``. Routes the
        atomic re-open decision to L2 (Redis Lua / SQL FOR UPDATE) so the
        cross-process exactly-one contract holds across gunicorn workers / K8s
        replicas, then branches on the L2-returned state:

        1. If L2 healthy: submit ``L2.record_failure_with_open_check`` via the
           timeout-bounded executor.
        2. ``state=='open'``: writeback L1 to OPEN carrying ``opened_at`` from
           the returned state (covers both the ``did_open=True`` winner and the
           ``did_open=False`` race-loser). Return the L2 attempt.
        3. ``state=='closed'``: trust L2 -- a concurrent quorum of HALF_OPEN
           successes closed the cluster while this worker's trial failed.
           Writeback L1 to CLOSED, ``did_open=False``, no re-open (a straggler
           failure never overrides the cluster's recovery).
        4. ``state in {missing, other}``: stale relative to the caller's
           HALF_OPEN view (a prior ``try_acquire`` took the L1-fallback path so
           L2 never saw the OPEN->HALF_OPEN transition). Record degraded-mode
           metric and fall back to L1's atomic re-open path.
        5. On L2 timeout / exception / unhealthy: record degraded-mode metric,
           delegate to ``_l1.record_failure_with_open_check`` and async-sync the
           resulting snapshot to L2.
        """
        if self._l2 and self._l2_healthy:
            timeout = self._get_timeout_seconds()
            start_time = time.perf_counter()

            try:
                executor = self._get_executor()
                future = executor.submit(
                    self._l2.record_failure_with_open_check,
                    service_name,
                )
                attempt = future.result(timeout=timeout)
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._handle_l2_success(elapsed_ms)

                returned_state = attempt.state.state
                if returned_state not in {"open", "closed"}:
                    # Stale-L2 guard: L2 disagrees with caller's HALF_OPEN
                    # expectation. Do NOT writeback; fall back to L1's atomic
                    # re-open path.
                    self._record_open_check_degraded_mode(service_name)
                    return self._l1_fallback_open_check(service_name)

                self._writeback_open_check_to_l1(service_name, attempt)
                return attempt

            except FuturesTimeoutError:
                self._handle_l2_timeout("record_failure_with_open_check", service_name)
            except Exception as e:
                self._handle_l2_error("record_failure_with_open_check", service_name, e)

        # L2 unavailable / failed -- fall back to L1.
        self._record_open_check_degraded_mode(service_name)
        return self._l1_fallback_open_check(service_name)

    def _l1_fallback_open_check(
        self,
        service_name: str,
    ) -> CircuitBreakerOpenAttempt:
        """L1-authoritative fallback for record_failure_with_open_check (656 D7)."""
        attempt = self._l1.record_failure_with_open_check(service_name)
        self._sync_to_l2_async(service_name)
        return attempt

    def _writeback_open_check_to_l1(
        self,
        service_name: str,
        attempt: CircuitBreakerOpenAttempt,
    ) -> None:
        """Sync the L2-authoritative open-check decision to L1 (656 D7).

        - For ``state='open'``: transition L1 to OPEN carrying ``opened_at``
          from the L2-returned state, with counters/watermarks reset (covers
          both the did_open=True winner and the did_open=False race-loser).
        - For ``state='closed'``: trust-L2 quorum-close convergence -- clear
          the L1 sliding window via ``reset_counts`` then transition L1 to
          CLOSED with ``reset_half_open_count=True``. No re-open.

        Writeback failures are logged but do not roll back the L2-authoritative
        decision.
        """
        try:
            self._l1.get_or_create(service_name)
            if attempt.state.state == "open":
                self._l1.update_state(
                    service_name=service_name,
                    state="open",
                    failure_count=0,
                    success_count=0,
                    opened_at=attempt.state.opened_at,
                    reset_half_open_count=True,
                )
            else:  # closed
                self._l1.reset_counts(service_name)
                self._l1.update_state(
                    service_name=service_name,
                    state="closed",
                    reset_half_open_count=True,
                )
        except Exception as e:
            logger.warning(
                "circuit_breaker.l1_open_check_writeback_failed",
                service_name=service_name,
                returned_state=attempt.state.state,
                did_open=attempt.did_open,
                error=str(e),
            )

    @staticmethod
    def _record_open_check_degraded_mode(service_name: str) -> None:
        """Increment the open-check degraded-mode counter (656 D7)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_open_check_degraded_mode,
            )

            record_open_check_degraded_mode(service_name)
        except ImportError:
            pass

    def trip_to_open(
        self,
        service_name: str,
        failure_count: int,
    ) -> CircuitBreakerOpenAttempt:
        """L2-authoritative CLOSED -> OPEN automatic trip (773 D1).

        The trip used to be an ordinary ``update_state``: an L1 write plus a
        fire-and-forget mirror racing the five failure records that produced it,
        so a genuine trip could be erased from the shared store by its own
        record path. Routing the state write to L2's atomic primitive (Redis
        Lua / SQL row lock) makes the durable row the outcome of one
        single-winner decision, and the local row a writeback of it.

        Branches:

        1. L2 healthy: submit ``L2.trip_to_open`` on the timeout-bounded
           executor.
        2. ``state=='open'``: writeback L1 to OPEN carrying the returned
           ``opened_at`` (covers the winner and the race-loser alike). The
           counters are left alone — L1 already holds the failure count the
           preceding ``record_failure`` wrote.
        3. ``state=='half_open'``: a peer tripped and the cluster already moved
           on to recovery testing. Join the trial regime rather than clobbering
           the store back to OPEN; the half-open counter and watermark stay
           L2-owned.
        4. ``state=='pinned'``: an override still in force declined the write.
           The remote row is delivered whole so this worker enforces the
           operator's decision from its next request, and the post-trip nudge is
           deliberately skipped — mirroring here is exactly what the override
           forbids.
        5. anything else (unknown stored state, L2 timeout / exception /
           unhealthy): record the degraded-mode metric and fall back to
           ``_l1.trip_to_open`` plus a mirror. The local row still transitions
           to OPEN, so protection is kept when the store cannot answer — but on
           this branch ``did_open`` is a *local* verdict with no synchronous
           store write, the same documented relaxation the close- and open-check
           chains carry.

        Every branch but ``pinned`` ends with a mirror nudge issued *after* its
        L1 writeback. Combined with the per-service coalescing in
        ``_sync_to_l2_async``, that is what makes the last same-service write
        this process performs reflect post-trip L1: a mirror already in flight
        re-runs against the written row, and one that already exited is
        replaced by a task starting after it.
        """
        if self._l2 and self._l2_healthy:
            timeout = self._get_timeout_seconds()
            start_time = time.perf_counter()

            try:
                executor = self._get_executor()
                future = executor.submit(
                    self._l2.trip_to_open,
                    service_name,
                    failure_count,
                )
                attempt = future.result(timeout=timeout)
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._handle_l2_success(elapsed_ms)

                returned_state = attempt.state.state
                if returned_state == CIRCUIT_BREAKER_PINNED_TOKEN:
                    return self._hydrate_pinned_trip(service_name, attempt)

                if returned_state not in {"open", "half_open"}:
                    self._record_trip_degraded_mode(service_name)
                    return self._l1_fallback_trip(service_name, failure_count)

                self._writeback_trip_to_l1(service_name, attempt)
                self._sync_to_l2_async(service_name)
                return attempt

            except FuturesTimeoutError:
                self._handle_l2_timeout("trip_to_open", service_name)
            except Exception as e:
                self._handle_l2_error("trip_to_open", service_name, e)

        # L2 unavailable / failed -- fall back to L1.
        self._record_trip_degraded_mode(service_name)
        return self._l1_fallback_trip(service_name, failure_count)

    def _l1_fallback_trip(
        self,
        service_name: str,
        failure_count: int,
    ) -> CircuitBreakerOpenAttempt:
        """L1-authoritative fallback for ``trip_to_open`` (773 D1 step 5)."""
        attempt = self._l1.trip_to_open(service_name, failure_count)
        self._sync_to_l2_async(service_name)
        return attempt

    def _writeback_trip_to_l1(
        self,
        service_name: str,
        attempt: CircuitBreakerOpenAttempt,
    ) -> None:
        """Sync the L2-authoritative trip decision to L1 (773 D1 steps 2-3).

        - ``state='open'``: transition L1 to OPEN carrying the store's
          ``opened_at``. Counters are untouched — the failure count this trip
          reports was written by the ``record_failure`` that preceded it.
        - ``state='half_open'``: join the cluster's trial regime with a cleared
          success count. The half-open counter and watermark belong to the
          atomic slot primitives and stay L2-owned.

        Writes go straight to L1 rather than through the layered
        ``update_state``, which would re-enter the mirror — the shipped
        writeback convention. Failures are logged and never roll back the
        store-authoritative decision.
        """
        try:
            self._l1.get_or_create(service_name)
            if attempt.state.state == "open":
                self._l1.update_state(
                    service_name=service_name,
                    state="open",
                    opened_at=attempt.state.opened_at,
                    reset_half_open_count=True,
                )
            else:  # half_open
                self._l1.update_state(
                    service_name=service_name,
                    state="half_open",
                    success_count=0,
                )
        except Exception as e:
            logger.warning(
                "circuit_breaker.l1_trip_writeback_failed",
                service_name=service_name,
                returned_state=attempt.state.state,
                did_open=attempt.did_open,
                error=str(e),
            )

    def _hydrate_pinned_trip(
        self,
        service_name: str,
        attempt: CircuitBreakerOpenAttempt,
    ) -> CircuitBreakerOpenAttempt:
        """Deliver the store's pinned row to L1 after a declined trip (773 D1 step 4).

        Copying the state alone would leave this worker unpinned and free to
        record outcomes, re-trip, and mirror an OPEN over an override still in
        force — so the remote row is delivered whole, pin fields included,
        exactly as the convergence lane delivers one.

        A failed delivery — the read raised, or the row vanished because the
        override was lifted concurrently — is logged and returns the declined
        verdict anyway. It never routes into the L1 fallback: a local OPEN
        written here would land over the operator's Allow, which is the
        inversion this branch exists to prevent.

        The sentinel token is preserved in the returned attempt. The hydration
        is what this worker enforces; the token is what the service reports, and
        collapsing it into the hydrated state would make a suppressed trip
        indistinguishable from an ordinary race-loser and silence the only log
        line that says an override swallowed a failure burst.
        """
        expires_at = attempt.state.manual_override_expires_at
        try:
            remote = self._l2.get_by_service_name(service_name) if self._l2 else None
            if remote is None:
                logger.warning(
                    "circuit_breaker.trip_pin_hydration_skipped",
                    service_name=service_name,
                    reason="remote_row_absent",
                )
            else:
                self._l1.hydrate_snapshot(remote, skip_if_local_pin_active=True)
                expires_at = remote.manual_override_expires_at
        except Exception as e:
            logger.warning(
                "circuit_breaker.trip_pin_hydration_failed",
                service_name=service_name,
                error=str(e),
            )

        return pinned_trip_attempt(service_name, expires_at)

    @staticmethod
    def _record_trip_degraded_mode(service_name: str) -> None:
        """Increment the trip degraded-mode counter (773 D1)."""
        try:
            from baldur.metrics.recorders.circuit_breaker import (
                record_trip_degraded_mode,
            )

            record_trip_degraded_mode(service_name)
        except ImportError:
            pass

    def get_all_states(self) -> list[CircuitBreakerStateData]:
        """Look up all states in L1."""
        return self._l1.get_all_states()

    def get_open_states(
        self, limit: int | None = None
    ) -> list[CircuitBreakerStateData]:
        """Look up OPEN states in L1."""
        return self._l1.get_open_states(limit)

    def reset(self, service_name: str) -> bool:
        """Reset in L1, then synchronize to L2."""
        result = self._l1.reset(service_name)

        if result:
            self._sync_to_l2_async(service_name)

        return result

    def _write_manual_control_through(
        self,
        service_name: str,
        row: CircuitBreakerStateData,
        pin_write: Callable[[CircuitBreakerStateRepository], Any],
    ) -> None:
        """Write an operator's manual-control result through to L2, synchronously.

        Two writes, in this order:

        1. the generic four-field state mirror, and
        2. the pin fields, via L2's own manual-control primitive.

        The generic mirror carries state only — a pin written through it alone
        would never reach the durable row. The order matters: a reader racing
        between the two writes sees the new state without the pin (an ordinary
        OPEN — the safe direction), where the reverse order would expose a pin
        attached to the state it replaced.

        Synchronous rather than fire-and-forget because the operator's response
        is read back once this returns; the expiry it reports and the expiry
        stored durably must be the same instant. Both writes are bounded by the
        adapter timeout on the shared executor, never issued inline.
        """
        l2 = self._l2
        if l2 is None:
            return

        self._sync_to_l2_with_timeout(service_name, row)
        self._sync_pin_to_l2(service_name, lambda: pin_write(l2), row.state)

    def _pin_fields_write(
        self, row: CircuitBreakerStateData
    ) -> Callable[[CircuitBreakerStateRepository], Any]:
        """Build the L2 pin write that reproduces ``row``'s manual-control fields.

        ``expires_at`` is passed explicitly from the row the L1 operation just
        produced — never recomputed from a TTL, which would resolve to a
        different instant than the one the operator's response reports.
        """

        def _write(l2: CircuitBreakerStateRepository) -> Any:
            return l2.set_manual_control(
                row.service_name,
                state=row.state,
                controlled_by_id=row.controlled_by_id,
                reason=row.control_reason,
                expires_at=row.manual_override_expires_at,
            )

        return _write

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int | None = None,
    ) -> tuple:
        """Force open in L1, then write state and pin fields through to L2."""
        result = self._l1.atomic_force_open(
            service_name, reason, controlled_by_id, ttl_minutes
        )

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._write_manual_control_through(
                    service_name, updated, self._pin_fields_write(updated)
                )

        return result

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int | None = None,
    ) -> tuple:
        """Force close in L1, then write state and pin fields through to L2."""
        result = self._l1.atomic_force_close(
            service_name, reason, controlled_by_id, ttl_minutes
        )

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._write_manual_control_through(
                    service_name, updated, self._pin_fields_write(updated)
                )

        return result

    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple:
        """Reset in L1, then write state and the pin clear through to L2."""
        result = self._l1.atomic_reset(service_name, reason, controlled_by_id)

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._write_manual_control_through(
                    service_name,
                    updated,
                    lambda l2: l2.atomic_reset(service_name, reason, controlled_by_id),
                )

        return result

    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        """Set manual control in L1, then write state and pin fields through to L2."""
        result = self._l1.set_manual_control(
            service_name, state, controlled_by_id, reason, expires_at
        )

        if result:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._write_manual_control_through(
                    service_name, updated, self._pin_fields_write(updated)
                )

        return result

    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        """Clear manual control in L1, then write the clear through to L2."""
        result = self._l1.clear_manual_control(service_name, preserve_reason)

        if result:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._write_manual_control_through(
                    service_name,
                    updated,
                    lambda l2: l2.clear_manual_control(service_name, preserve_reason),
                )

        return result

    def delete_state(self, service_name: str) -> bool:
        """Delete circuit breaker state for service. L1 primary, L2 sync."""
        result = self._l1.delete(service_name)
        if self._l2:
            try:
                self._l2.delete_state(service_name)
            except Exception:
                pass
        return result
