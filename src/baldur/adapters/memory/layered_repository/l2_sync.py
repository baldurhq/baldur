"""
L2 Sync Operations Mixin.

Provides methods for syncing data to/from L2 storage.
"""

from __future__ import annotations

import time
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

        def _sync():
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
            except Exception as e:
                self._handle_l2_error("sync", service_name, e, state.state)

        try:
            executor = self._get_executor()
            executor.submit(_sync)
        except Exception as e:
            logger.warning(
                "layered_repo.submit_sync_task_failed",
                error=e,
            )

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

        all_states = self._l1.get_all_states()
        success_count = 0
        failure_count = 0

        for state in all_states:
            if self._sync_to_l2_with_timeout(state.service_name, state):
                success_count += 1
            else:
                failure_count += 1

        if success_count > 0:
            self._shadow_logger.mark_all_as_synced()

        return {
            "success": failure_count == 0,
            "total": len(all_states),
            "synced": success_count,
            "failed": failure_count,
        }
