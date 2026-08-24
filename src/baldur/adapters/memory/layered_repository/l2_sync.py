"""
L2 Sync Operations Mixin.

Provides methods for syncing data to/from L2 storage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import CircuitBreakerStateData

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.adapters.memory.shadow_logger import ShadowLogger
    from baldur.interfaces.repositories import CircuitBreakerStateRepository

logger = structlog.get_logger()


class L2SyncMixin:
    """Mixin providing L2 sync operations."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # LayeredRepositoryBase and sibling mixins
        # (ErrorHandlingMixin, L2LoadMixin). See
        # LayeredCircuitBreakerStateRepository for the assembled class.
        _l1: InMemoryCircuitBreakerStateRepository
        _l2: CircuitBreakerStateRepository | None
        _l2_healthy: bool
        _shadow_logger: ShadowLogger

        def _get_timeout_seconds(self) -> float: ...
        def _get_executor(self) -> ThreadPoolExecutor: ...
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
        def _load_from_l2_with_timeout(self) -> None: ...

    def _submit_l2_write(self, write: Callable[[], Any]) -> None:
        """Run one L2 write on the shared executor, bounded by the adapter timeout.

        Every synchronous L2 write routes through here instead of calling the
        adapter inline. An inline call is bounded only by the Redis socket
        timeout with retry-on-timeout — seconds, on whichever thread issued the
        operation, which for the manual-control ops is the operator's own
        request thread.

        Raises ``TimeoutError`` (concurrent.futures) or whatever the write
        raised; each caller reports it with its own literal event name.
        """
        future = self._get_executor().submit(write)
        future.result(timeout=self._get_timeout_seconds())

    def _sync_pin_to_l2(
        self,
        service_name: str,
        write: Callable[[], Any],
        intended_state: str = "",
    ) -> bool:
        """Write an operator's manual-control decision through to L2, bounded.

        The generic state mirror carries only the four state fields, so a pin
        placed here would otherwise never reach the durable row and would be
        lost to every process that hydrates from it. Deliberately synchronous:
        the operator's response is read back after this returns, so the stored
        expiry and the reported one must be the same instant.

        Fail-open on the durability side-effect: a failed L2 write is logged
        and counted through the quarantine handlers, and the operation still
        succeeds — enforcement is the L1 row, which is already written.
        """
        if not self._l2:
            return False

        start_time = time.perf_counter()
        try:
            self._submit_l2_write(write)
            self._handle_l2_success((time.perf_counter() - start_time) * 1000)
            return True
        except FuturesTimeoutError:
            self._handle_l2_timeout("manual_control_sync", service_name)
            logger.warning(
                "layered_repo.manual_control_sync_timeout_ms",
                service_name=service_name,
                timeout_ms=self._get_timeout_seconds() * 1000,
            )
            return False
        except Exception as e:
            self._handle_l2_error(
                "manual_control_sync", service_name, e, intended_state
            )
            return False

    def _sync_to_l2_with_timeout(
        self,
        service_name: str,
        state: CircuitBreakerStateData,
    ) -> bool:
        """Synchronize to L2 (timeout applied)."""
        if not self._l2:
            return False

        timeout = self._get_timeout_seconds()
        start_time = time.perf_counter()

        def _do_sync():
            self._l2.get_or_create(service_name)
            self._l2.update_state(
                service_name=service_name,
                state=state.state,
                failure_count=state.failure_count,
                success_count=state.success_count,
                opened_at=state.opened_at,
            )

        try:
            executor = self._get_executor()
            future = executor.submit(_do_sync)
            future.result(timeout=timeout)

            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._handle_l2_success(elapsed_ms)
            return True

        except FuturesTimeoutError:
            self._handle_l2_timeout("sync", service_name)
            logger.warning(
                "layered_repo.sync_timeout_ms_isolated",
                service_name=service_name,
                timeout_ms=timeout * 1000,
            )
            return False

        except Exception as e:
            self._handle_l2_error("sync", service_name, e, state.state)
            return False

    def _sync_to_l2_inline(
        self,
        service_name: str,
        state: CircuitBreakerStateData,
    ) -> bool:
        """Mirror one state snapshot to L2 on the calling thread.

        The write body shared by every caller that already owns an executor
        thread: the fire-and-forget mirror task and the convergence lane's
        repair. Bounded by the adapter's own socket/statement timeout rather
        than by ``future.result()``, because a task that submits its own work
        and then waits for it occupies two pool slots — and on a pool sized 1
        or 2 the inner task can never start, so the wait always times out and
        three of them quarantine a perfectly healthy L2.

        Every failure routes to ``_handle_l2_error`` so quarantine accounting
        stays correct; nothing propagates to the caller.
        """
        if not self._l2:
            return False

        start_time = time.perf_counter()
        try:
            self._l2.get_or_create(service_name)
            self._l2.update_state(
                service_name=service_name,
                state=state.state,
                failure_count=state.failure_count,
                success_count=state.success_count,
                opened_at=state.opened_at,
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._handle_l2_success(elapsed_ms)
            return True
        except Exception as e:
            self._handle_l2_error("sync", service_name, e, state.state)
            return False

    def _sync_to_l2_async(
        self, service_name: str, state: CircuitBreakerStateData
    ) -> None:
        """Asynchronously mirror L1 state to L2 (fire-and-forget).

        Skipped entirely while L2 is quarantined (``_l2_healthy`` False) so a
        degraded L2 stops accumulating doomed sync tasks on the shared
        executor queue — every other L2-touching path already gates on
        ``_l2_healthy``; this is the mirror path that did not. Skipped writes
        are repaired by drift reconciliation once L2 recovers.

        Submits a single task that performs the L2 write inline (one worker
        thread, not the submit-within-submit of ``_sync_to_l2_with_timeout``
        that occupied two). The task's whole body is wrapped so every failure
        routes to ``_handle_l2_error`` — without ``future.result()`` to
        re-raise, an uncaught exception would be swallowed by the discarded
        ``Future`` and would never advance ``_l2_consecutive_failures``, so
        the quarantine the guard above relies on would never trip.
        """
        if not self._l2 or not self._l2_healthy:
            return

        try:
            executor = self._get_executor()
            executor.submit(self._sync_to_l2_inline, service_name, state)
        except Exception as e:
            logger.warning(
                "layered_repo.submit_sync_task_failed",
                error=e,
            )

    def _resolve_repair_row(self, service_name: str) -> CircuitBreakerStateData | None:
        """Fresh-read the L1 row a repair would mirror, or ``None`` to skip it.

        The two properties every whole-row L1→L2 repair lane shares, decided in
        one place so the timeout-bounded and inline variants cannot drift:

        - **Freshness.** The row is read here, never taken from a snapshot the
          caller took at the start of its pass. A snapshot predating an
          operator's Block would otherwise be written back over it, leaving a
          CLOSED row that still reports itself manually controlled — a shape
          whose admission short-circuit admits everything.
        - **Pin neutrality.** A pinned row is skipped outright. The mirror
          opens with L2 ``get_or_create``, which on a missing key writes the
          default payload — including "not manually controlled" — so repairing
          a pinned service after the durable row was lost would erase the
          operator's decision from the shared store.

        Skipping is safe rather than merely conservative: the manual-control
        ops already write the pinned row through to L2 synchronously, so what a
        skipped repair leaves behind is correct, not stale. Reconciliation here
        is pin-*neutral* — it never creates, erases, or contradicts a pin; it
        does not deliver one either.
        """
        row = self._l1.get_by_service_name(service_name)
        if row is None:
            logger.debug(
                "layered_repo.repair_skipped_row_absent",
                service_name=service_name,
            )
            return None
        if row.manually_controlled:
            logger.debug(
                "layered_repo.repair_skipped_manually_controlled",
                service_name=service_name,
            )
            return None
        return row

    def _repair_row_to_l2_inline(self, service_name: str) -> bool | None:
        """``_repair_row_to_l2`` for a caller that already owns a pool thread.

        Identical freshness, pin-skip and tri-state contract; the mirror runs
        on the calling thread instead of a nested submit. Used by the
        convergence lane, whose task is already resident in the shared
        executor.
        """
        row = self._resolve_repair_row(service_name)
        if row is None:
            return None
        return self._sync_to_l2_inline(service_name, row)

    def _repair_row_to_l2(self, service_name: str) -> bool | None:
        """Mirror one L1 row to L2 for repair — unless the row is pinned.

        The timeout-bounded variant, for callers on a request or scheduler
        thread: the mirror is submitted to the shared executor and capped by
        the adapter timeout. Freshness and pin neutrality come from
        ``_resolve_repair_row``.

        Returns True when the mirror ran and succeeded, False when it ran and
        failed, and ``None`` when nothing was attempted (row gone or pinned) —
        a skip is not a failure and must not be reported as one.
        """
        row = self._resolve_repair_row(service_name)
        if row is None:
            return None
        return self._sync_to_l2_with_timeout(service_name, row)

    def force_sync_from_l2(self) -> bool:
        """Force synchronization from L2 (administrative purpose)."""
        if not self._l2:
            return False

        try:
            self._load_from_l2_with_timeout()
            return True
        except Exception as e:
            logger.exception(
                "layered_repo.force_sync_failed",
                error=e,
            )
            return False

    def force_sync_to_l2(self) -> dict[str, Any]:
        """Force-synchronize all L1 state to L2."""
        if not self._l2:
            return {"success": False, "reason": "L2 not configured"}

        success_count = 0
        failure_count = 0
        skipped_count = 0

        # Enumerate names, not rows: the repair helper re-reads each row, so a
        # pin taken during the pass is honoured rather than overwritten.
        for state in self._l1.get_all_states():
            outcome = self._repair_row_to_l2(state.service_name)
            if outcome is None:
                skipped_count += 1
            elif outcome:
                success_count += 1
            else:
                failure_count += 1

        if success_count > 0:
            self._shadow_logger.mark_all_as_synced()

        return {
            "success": failure_count == 0,
            "total": success_count + failure_count + skipped_count,
            "synced": success_count,
            "failed": failure_count,
            "skipped": skipped_count,
        }
