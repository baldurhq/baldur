"""
Drift Reconciliation Operations Mixin.

Provides methods for drift detection and reconciliation.
"""

from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.memory.drift_reconciliation import DriftReconciliationResult

if TYPE_CHECKING:
    from concurrent.futures import ThreadPoolExecutor

    from baldur.adapters.memory.circuit_breaker import (
        InMemoryCircuitBreakerStateRepository,
    )
    from baldur.adapters.memory.drift_reconciliation import DriftReconciler
    from baldur.adapters.memory.shadow_logger import ShadowLogger
    from baldur.interfaces.repositories import (
        CircuitBreakerStateData,
        CircuitBreakerStateRepository,
    )

logger = structlog.get_logger()


class DriftOperationsMixin:
    """Mixin providing drift reconciliation operations."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # LayeredRepositoryBase and sibling mixins.
        _l1: InMemoryCircuitBreakerStateRepository
        _l2: CircuitBreakerStateRepository | None
        _drift_reconciler: DriftReconciler
        _metrics: dict[str, Any]
        _shadow_logger: ShadowLogger

        def _get_timeout_seconds(self) -> float: ...
        def _get_executor(self) -> ThreadPoolExecutor: ...
        def _incr_metrics(self, **deltas: float) -> None: ...
        def _sync_to_l2_with_timeout(
            self, service_name: str, state: CircuitBreakerStateData
        ) -> bool: ...
        def _log_drift_reconciliation_audit(
            self,
            total_checked: int,
            reconciled: int,
            l1_wins: int,
            l2_wins: int,
            errors: list[dict[str, Any]],
        ) -> None: ...

    def _schedule_drift_reconciliation(self) -> None:
        """Schedule drift reconciliation in the background."""

        def _run_reconciliation():
            try:
                jitter = self._drift_reconciler.get_jitter()
                if jitter > 0:
                    logger.debug(
                        "layered_repo.drift_reconciliation_scheduled_jitter",
                        jitter=jitter,
                    )
                    time.sleep(jitter)

                self._reconcile_all_drift()
            except Exception as e:
                logger.exception(
                    "layered_repo.drift_reconciliation_error",
                    error=e,
                )

        try:
            executor = self._get_executor()
            executor.submit(_run_reconciliation)
        except Exception as e:
            logger.warning(
                "layered_repo.schedule_drift_reconciliation_failed",
                error=e,
            )

    def _reconcile_all_drift(self) -> dict[str, Any]:
        """Reconcile L1/L2 drift for every service."""
        if not self._l2:
            return {"success": False, "reason": "L2 not configured"}

        reconciled_count = 0
        l1_wins_count = 0
        l2_wins_count = 0
        errors = []

        l1_states = self._l1.get_all_states()

        for l1_state in l1_states:
            try:
                timeout = self._get_timeout_seconds()
                executor = self._get_executor()
                future = executor.submit(
                    self._l2.get_by_service_name, l1_state.service_name
                )

                try:
                    l2_state = future.result(timeout=timeout)
                except FuturesTimeoutError:
                    logger.warning(
                        "layered_repo.drift_reconciliation_timeout_skipping",
                        l1_state=l1_state.service_name,
                    )
                    continue

                if l2_state is None:
                    self._sync_to_l2_with_timeout(l1_state.service_name, l1_state)
                    l1_wins_count += 1
                    reconciled_count += 1
                    continue

                winner_state, result = self._drift_reconciler.reconcile(
                    service_name=l1_state.service_name,
                    l1_state=l1_state.state,
                    l2_state=l2_state.state,
                    l1_updated_at=l1_state.updated_at,
                    l2_updated_at=l2_state.updated_at,
                )

                if result == DriftReconciliationResult.NO_DRIFT:
                    continue

                reconciled_count += 1

                if result in (
                    DriftReconciliationResult.L1_WINS,
                    DriftReconciliationResult.TIMESTAMP_L1,
                ):
                    self._sync_to_l2_with_timeout(l1_state.service_name, l1_state)
                    l1_wins_count += 1
                else:
                    self._l1.update_state(
                        service_name=l2_state.service_name,
                        state=l2_state.state,
                        failure_count=l2_state.failure_count,
                        success_count=l2_state.success_count,
                        opened_at=l2_state.opened_at,
                    )
                    l2_wins_count += 1

            except Exception as e:
                errors.append(
                    {
                        "service": l1_state.service_name,
                        "error": str(e),
                    }
                )
                logger.warning(
                    "layered_repo.drift_reconciliation_failed",
                    l1_state=l1_state.service_name,
                    error=e,
                )

        self._incr_metrics(drift_reconciliation_count=reconciled_count)

        if reconciled_count > 0:
            self._shadow_logger.mark_all_as_synced()

            # Audit record: drift reconciliation completed
            self._log_drift_reconciliation_audit(
                total_checked=len(l1_states),
                reconciled=reconciled_count,
                l1_wins=l1_wins_count,
                l2_wins=l2_wins_count,
                errors=errors,
            )

        result_dict = {
            "success": len(errors) == 0,
            "total_checked": len(l1_states),
            "reconciled": reconciled_count,
            "l1_wins": l1_wins_count,
            "l2_wins": l2_wins_count,
            "errors": errors,
        }

        logger.info(
            "layered_repo.drift_reconciliation_completed_reconciled",
            reconciled_count=reconciled_count,
            l1_wins_count=l1_wins_count,
            l2_wins_count=l2_wins_count,
        )

        return result_dict

    def force_drift_reconciliation(self) -> dict[str, Any]:
        """Manually trigger drift reconciliation."""
        if not self._l2:
            return {"success": False, "reason": "L2 not configured"}

        logger.info("layered_repo.manual_drift_reconciliation_triggered")
        return self._reconcile_all_drift()

    def get_drift_reconciler_stats(self) -> dict[str, Any]:
        """Return drift reconciliation statistics."""
        return self._drift_reconciler.get_stats()

    def get_drift_reconciliation_history(self) -> list[dict[str, Any]]:
        """Return drift reconciliation history."""
        history = self._drift_reconciler.get_history()
        return [
            {
                "service_name": r.service_name,
                "l1_state": r.l1_state,
                "l2_state": r.l2_state,
                "l1_updated_at": (
                    r.l1_updated_at.isoformat() if r.l1_updated_at else None
                ),
                "l2_updated_at": (
                    r.l2_updated_at.isoformat() if r.l2_updated_at else None
                ),
                "winner": r.winner,
                "result": r.result.value,
                "reconciled_at": r.reconciled_at.isoformat(),
                "jitter_seconds": r.jitter_seconds,
            }
            for r in history
        ]

    def reconcile_single_service(self, service_name: str) -> dict[str, Any]:
        """Reconcile drift for a single service."""
        if not self._l2:
            return {"success": False, "reason": "L2 not configured"}

        l1_state = self._l1.get_by_service_name(service_name)
        if l1_state is None:
            return {"success": False, "reason": "Service not found in L1"}

        try:
            timeout = self._get_timeout_seconds()
            executor = self._get_executor()
            future = executor.submit(self._l2.get_by_service_name, service_name)
            l2_state = future.result(timeout=timeout)
        except FuturesTimeoutError:
            return {"success": False, "reason": "L2 timeout"}
        except Exception as e:
            return {"success": False, "reason": str(e)}

        if l2_state is None:
            self._sync_to_l2_with_timeout(service_name, l1_state)
            return {
                "success": True,
                "action": "l1_to_l2",
                "reason": "L2 had no state, synced from L1",
            }

        winner_state, result = self._drift_reconciler.reconcile(
            service_name=service_name,
            l1_state=l1_state.state,
            l2_state=l2_state.state,
            l1_updated_at=l1_state.updated_at,
            l2_updated_at=l2_state.updated_at,
        )

        if result == DriftReconciliationResult.NO_DRIFT:
            return {
                "success": True,
                "action": "none",
                "reason": "No drift detected",
            }

        self._incr_metrics(drift_reconciliation_count=1)

        if result in (
            DriftReconciliationResult.L1_WINS,
            DriftReconciliationResult.TIMESTAMP_L1,
        ):
            self._sync_to_l2_with_timeout(service_name, l1_state)
            return {
                "success": True,
                "action": "l1_to_l2",
                "winner": "l1",
                "result": result.value,
                "winner_state": winner_state,
            }
        self._l1.update_state(
            service_name=l2_state.service_name,
            state=l2_state.state,
            failure_count=l2_state.failure_count,
            success_count=l2_state.success_count,
            opened_at=l2_state.opened_at,
        )
        return {
            "success": True,
            "action": "l2_to_l1",
            "winner": "l2",
            "result": result.value,
            "winner_state": winner_state,
        }
