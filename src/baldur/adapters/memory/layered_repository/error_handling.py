"""
L2 Error Handling Mixin.

Provides methods for handling L2 errors and success states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.utils.time import utc_now

if TYPE_CHECKING:
    import threading
    from datetime import datetime

    from baldur.adapters.memory.shadow_logger import ShadowLogger

logger = structlog.get_logger()


class ErrorHandlingMixin:
    """Mixin providing L2 error handling operations."""

    if TYPE_CHECKING:
        # Host contract — attributes/methods provided via MRO by
        # LayeredRepositoryBase and sibling mixins
        # (AuditHelpersMixin, DriftOperationsMixin).
        _metrics: dict[str, Any]
        _adapter_type: str
        _shadow_logger: ShadowLogger
        _lock: threading.Lock
        _l2_consecutive_failures: int
        _l2_healthy: bool
        _l2_last_error_time: datetime | None
        _l2_was_unhealthy: bool

        def _log_l2_failure_audit(
            self,
            operation: str,
            service_name: str | None,
            error_type: str,
            error_message: str,
            consecutive_failures: int,
        ) -> None: ...
        def _send_l2_failure_notification(
            self,
            failure_type: str,
            consecutive_failures: int,
            error_message: str = "",
        ) -> None: ...
        def _log_l2_recovery_audit(self) -> None: ...
        def _send_l2_recovery_notification(self) -> None: ...
        def _schedule_drift_reconciliation(self) -> None: ...
        def _incr_metrics(self, **deltas: float) -> None: ...

    def _handle_l2_timeout(self, operation: str, service_name: str | None) -> None:
        """Handle an L2 timeout."""
        self._incr_metrics(l2_timeout_count=1)

        # State critical section (D2/D3): atomize the failure-count
        # read-modify-write and the healthy->quarantined transition decision,
        # then release before firing any I/O side-effect. `should_fire`
        # captures the one-shot edge under the lock — a concurrent failing
        # thread observes the already-flipped `_l2_healthy` and does not
        # double-fire — and `consecutive` captures the edge value so the
        # WARNING / audit / notification trio all report the same number.
        with self._lock:
            self._l2_consecutive_failures += 1
            self._l2_last_error_time = utc_now()
            consecutive = self._l2_consecutive_failures
            should_fire = False
            if consecutive >= 3:
                should_fire = self._l2_healthy
                self._l2_healthy = False
                self._l2_was_unhealthy = True

        if should_fire:
            # One-shot on the healthy->quarantined edge: WARNING + audit +
            # notification fire exactly once (D4), all using the captured
            # `consecutive` so they agree on the edge value even under
            # concurrency.
            logger.warning(
                "layered_repo.l2_quarantined",
                adapter_type=self._adapter_type,
                consecutive_failures=consecutive,
            )
            self._log_l2_failure_audit(
                operation=operation,
                service_name=service_name,
                error_type="timeout",
                error_message=f"L2 timeout after {consecutive} consecutive failures",
                consecutive_failures=consecutive,
            )
            self._send_l2_failure_notification(
                failure_type="timeout",
                consecutive_failures=consecutive,
            )

        try:
            from baldur.services.metrics.recorders import record_l2_timeout

            record_l2_timeout(self._adapter_type, operation)
        except ImportError:
            pass

    def _handle_l2_error(
        self,
        operation: str,
        service_name: str | None,
        error: Exception,
        intended_state: str = "",
    ) -> None:
        """Handle an L2 error and record a Shadow Log."""
        self._incr_metrics(l2_sync_failure_count=1)

        # State critical section (D2/D3): see _handle_l2_timeout — atomize the
        # failure-count RMW and the transition decision, capture the one-shot
        # `should_fire` + edge value `consecutive`, fire side-effects after
        # release.
        with self._lock:
            self._l2_consecutive_failures += 1
            self._l2_last_error_time = utc_now()
            consecutive = self._l2_consecutive_failures
            should_fire = False
            if consecutive >= 3:
                should_fire = self._l2_healthy
                self._l2_healthy = False
                self._l2_was_unhealthy = True

        if should_fire:
            # One-shot on the healthy->quarantined edge: WARNING + audit +
            # notification fire exactly once (D4), all using the captured
            # `consecutive`.
            logger.warning(
                "layered_repo.l2_quarantined",
                adapter_type=self._adapter_type,
                consecutive_failures=consecutive,
            )
            self._log_l2_failure_audit(
                operation=operation,
                service_name=service_name,
                error_type=type(error).__name__,
                error_message=str(error)[:500],
                consecutive_failures=consecutive,
            )
            self._send_l2_failure_notification(
                failure_type="error",
                consecutive_failures=consecutive,
                error_message=str(error)[:200],
            )

        if service_name and intended_state:
            self._shadow_logger.record_sync_failure(
                service_name=service_name,
                intended_state=intended_state,
                error=error,
                adapter_type=self._adapter_type,
                operation=operation,
            )

        try:
            from baldur.services.metrics.recorders import record_l2_sync_failure

            record_l2_sync_failure(self._adapter_type, operation)
        except ImportError:
            pass

    def _handle_l2_success(self, elapsed_ms: float) -> None:
        """Handle an L2 success and detect recovery."""
        self._incr_metrics(
            l2_sync_success_count=1,
            l2_latency_total_ms=elapsed_ms,
            l2_latency_count=1,
        )

        # State critical section (D2/D3): atomize the recovery transition
        # decision and the count/healthy reset, then fire side-effects after
        # release. `was_unhealthy` is captured under the lock and clears
        # `_l2_was_unhealthy` in the same section so only the thread that saw
        # the quarantined->healthy edge fires the one-shot; a concurrent
        # success reads the already-cleared flag and stays silent.
        with self._lock:
            was_unhealthy = not self._l2_healthy or self._l2_was_unhealthy
            self._l2_consecutive_failures = 0
            self._l2_healthy = True
            if was_unhealthy:
                self._l2_was_unhealthy = False

        if was_unhealthy:
            logger.info(
                "layered_repo.recovery_detected_after_failures",
                l2_sync_failure_count=self._metrics.get("l2_sync_failure_count", 0),
            )

            # Audit record: L2 recovery
            self._log_l2_recovery_audit()

            # Send notification: L2 recovery complete
            self._send_l2_recovery_notification()

            self._schedule_drift_reconciliation()

        try:
            from baldur.services.metrics.recorders import record_l2_latency

            record_l2_latency(self._adapter_type, elapsed_ms / 1000.0)
        except ImportError:
            pass
