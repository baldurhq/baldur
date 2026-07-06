"""
Repository Operations Mixin.

Provides CircuitBreakerStateRepository interface implementation with L1 priority.
"""

from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.repositories import (
    CircuitBreakerCloseAttempt,
    CircuitBreakerOpenAttempt,
    CircuitBreakerStateData,
)

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

logger = structlog.get_logger()


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

        def _get_timeout_seconds(self) -> float: ...
        def _get_executor(self) -> ThreadPoolExecutor: ...
        def _sync_to_l2_async(
            self, service_name: str, state: CircuitBreakerStateData
        ) -> None: ...
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
                    self._l1.get_or_create(service_name)
                    self._l1.update_state(
                        service_name=service_name,
                        state=l2_result.state,
                        failure_count=l2_result.failure_count,
                        success_count=l2_result.success_count,
                        opened_at=l2_result.opened_at,
                    )
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
    ) -> bool:
        """Update L1, then asynchronously synchronize to L2 (476 D9 reset flag forwarded)."""
        result = self._l1.update_state(
            service_name=service_name,
            state=state,
            failure_count=failure_count,
            success_count=success_count,
            opened_at=opened_at,
            last_failure_at=last_failure_at,
            half_open_request_count=half_open_request_count,
            reset_half_open_count=reset_half_open_count,
        )

        if result:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

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
        self._sync_to_l2_async(service_name, result)
        return result

    def record_success(self, service_name: str) -> CircuitBreakerStateData:
        """Record a success in L1, then synchronize to L2."""
        result = self._l1.record_success(service_name)
        self._sync_to_l2_async(service_name, result)
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
        self._sync_to_l2_async(service_name, attempt.state)
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
        self._sync_to_l2_async(service_name, attempt.state)
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
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

        return result

    def atomic_force_open(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
        ttl_minutes: int = 90,
    ) -> tuple:
        """Force open in L1, then synchronize to L2."""
        result = self._l1.atomic_force_open(
            service_name, reason, controlled_by_id, ttl_minutes
        )

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

        return result

    def atomic_force_close(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple:
        """Force close in L1, then synchronize to L2."""
        result = self._l1.atomic_force_close(service_name, reason, controlled_by_id)

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

        return result

    def atomic_reset(
        self,
        service_name: str,
        reason: str = "",
        controlled_by_id: int | None = None,
    ) -> tuple:
        """Reset in L1, then synchronize to L2."""
        result = self._l1.atomic_reset(service_name, reason, controlled_by_id)

        if result[0]:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

        return result

    def set_manual_control(
        self,
        service_name: str,
        state: str,
        controlled_by_id: int | None = None,
        reason: str = "",
        expires_at: datetime | None = None,
    ) -> bool:
        """Set manual control in L1, then synchronize to L2."""
        result = self._l1.set_manual_control(
            service_name, state, controlled_by_id, reason, expires_at
        )

        if result:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

        return result

    def clear_manual_control(
        self, service_name: str, preserve_reason: bool = False
    ) -> bool:
        """Clear manual control in L1, then synchronize to L2."""
        result = self._l1.clear_manual_control(service_name, preserve_reason)

        if result:
            updated = self._l1.get_by_service_name(service_name)
            if updated:
                self._sync_to_l2_async(service_name, updated)

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
