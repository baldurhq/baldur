"""
Audit and Notification Helpers Mixin.

Provides methods for audit logging and notifications with Fail-Open principle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.helpers import (
    log_drift_reconciliation_audit,
    log_storage_failure_audit,
    log_storage_recovery_audit,
)

logger = structlog.get_logger()


class AuditHelpersMixin:
    """Mixin providing audit logging and notification helpers."""

    if TYPE_CHECKING:
        # Host contract — attributes provided via MRO by
        # LayeredRepositoryBase / sibling mixins.
        _adapter_type: str
        _metrics: dict[str, Any]

    def _log_l2_failure_audit(
        self,
        operation: str,
        service_name: str | None,
        error_type: str,
        error_message: str,
        consecutive_failures: int,
    ) -> None:
        """Record an Audit log on an L2 failure.

        ``consecutive_failures`` is the edge value captured under the state
        lock by the caller, not a re-read of ``self._l2_consecutive_failures``
        — so the audit record agrees with the WARNING and notification of the
        same transition even when later failures land before this fires.
        """
        log_storage_failure_audit(
            storage_type="l2",
            adapter_type=self._adapter_type,
            operation=operation,
            service_name=service_name,
            error_type=error_type,
            error_message=error_message,
            consecutive_failures=consecutive_failures,
        )

    def _log_l2_recovery_audit(self) -> None:
        """Record an Audit log on L2 recovery."""
        log_storage_recovery_audit(
            storage_type="l2",
            adapter_type=self._adapter_type,
            total_failures=self._metrics.get("l2_sync_failure_count", 0),
        )

    def _log_drift_reconciliation_audit(
        self,
        total_checked: int,
        reconciled: int,
        l1_wins: int,
        l2_wins: int,
        errors: list[dict[str, Any]],
    ) -> None:
        """Record an Audit log when drift reconciliation completes."""
        log_drift_reconciliation_audit(
            adapter_type=self._adapter_type,
            total_checked=total_checked,
            reconciled=reconciled,
            l1_wins=l1_wins,
            l2_wins=l2_wins,
            error_count=len(errors),
        )

    def _send_l2_failure_notification(
        self,
        failure_type: str,
        consecutive_failures: int,
        error_message: str = "",
    ) -> None:
        """Send a notification on consecutive L2 failures. Applies the Fail-Open principle."""
        try:
            from baldur.models.notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
            )
            from baldur_pro.services.unified_notification import (
                get_unified_notification_manager,
            )

            notification_service = get_unified_notification_manager()
            notification_service.notify(
                NotificationPayload(
                    title=f"L2 Storage Failure ({self._adapter_type})",
                    message=(
                        f"L2 storage has failed {consecutive_failures} consecutive times. "
                        f"Type: {failure_type}. System operating in L1-only mode. "
                        f"{error_message}"
                    ),
                    priority=NotificationPriority.HIGH,
                    category=NotificationCategory.OPERATIONS,
                    source="layered_repository",
                    metadata={
                        "adapter_type": self._adapter_type,
                        "failure_type": failure_type,
                        "consecutive_failures": consecutive_failures,
                    },
                )
            )
        except Exception as e:
            logger.debug(
                "layered_repo.notification_failed_ignored",
                error=e,
            )

    def _send_l2_recovery_notification(self) -> None:
        """Send a notification when L2 recovery completes. Applies the Fail-Open principle."""
        try:
            from baldur.models.notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
            )
            from baldur_pro.services.unified_notification import (
                get_unified_notification_manager,
            )

            notification_service = get_unified_notification_manager()
            notification_service.notify(
                NotificationPayload(
                    title=f"L2 Storage Recovered ({self._adapter_type})",
                    message=(
                        f"L2 storage has recovered after "
                        f"{self._metrics.get('l2_sync_failure_count', 0)} failures. "
                        f"Drift reconciliation initiated."
                    ),
                    priority=NotificationPriority.INFO,
                    category=NotificationCategory.OPERATIONS,
                    source="layered_repository",
                    metadata={
                        "adapter_type": self._adapter_type,
                        "total_failures": self._metrics.get("l2_sync_failure_count", 0),
                    },
                )
            )
        except Exception as e:
            logger.debug(
                "layered_repo.notification_failed_ignored",
                error=e,
            )
