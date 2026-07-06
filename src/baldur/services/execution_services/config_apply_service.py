"""
Config Apply Service

Service layer for deferred config changes and graceful config application.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.factory.registry import ProviderRegistry

logger = structlog.get_logger()


# =============================================================================
# ConfigApplyService
# =============================================================================


class ConfigApplyService:
    """
    Config application service.

    Handles deferred config changes and graceful config application.

    Usage:
        service = get_config_apply_service()

        # Apply pending changes
        result = service.apply_pending_changes()
    """

    _instance: ConfigApplyService | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

    def apply_pending_changes(self) -> dict[str, Any]:
        """
        Apply pending config changes.

        Called by Celery Beat every 5 seconds.

        Note:
            Config Apply only checks Emergency. Kill Switch is intentionally
            skipped to preserve a recovery path.

        Returns:
            Application result dictionary
        """
        # Emergency Mode check: block config apply at LEVEL_2+. Kill Switch
        # intentionally skipped to preserve a recovery path.
        governance = ProviderRegistry.governance.get()
        governance_result = governance.check_all_governance(
            check_kill_switch=False,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=False,
            operation_name="apply_pending_changes",
            service_name="ConfigApplyService",
            domain="config",
        )

        if not governance_result.allowed:
            logger.warning(
                "config_apply_service.config_changes_blocked",
                governance_result=governance_result.block_message,
            )
            return {
                "status": "blocked",
                "reason": governance_result.block_message,
                "message": "Config changes blocked during emergency mode",
            }

        try:
            from baldur.services.pending_config import get_pending_config_service

            try:
                from baldur_pro.services.runtime_config import (
                    get_runtime_config_manager,
                )
            except ImportError:
                get_runtime_config_manager = None  # type: ignore[assignment,misc]

            if get_runtime_config_manager is None:
                logger.debug("config_apply_service.pro_modules_unavailable")
                return {
                    "status": "blocked",
                    "reason": "runtime_config_manager_unavailable",
                    "message": "baldur_pro.services.runtime_config not installed",
                }

            pending_service = get_pending_config_service()
            config_manager = get_runtime_config_manager()

            due_changes = pending_service.get_due_changes()

            if not due_changes:
                return {
                    "status": "success",
                    "applied": 0,
                    "message": "No pending changes due",
                }

            applied_count = 0
            failed_count = 0
            results = []

            for change in due_changes:
                try:
                    result = config_manager.apply_pending_change(change.id)

                    if result.get("status") == "applied":
                        applied_count += 1
                        logger.info(
                            "config_apply_service.applied_pending_change",
                            change=change.id,
                        )
                    else:
                        failed_count += 1
                        logger.error(
                            "config_apply_service.apply_failed",
                            change=change.id,
                            error=result.get("error"),
                        )

                    results.append(
                        {
                            "id": change.id,
                            "config_type": change.config_type,
                            "status": result.get("status"),
                        }
                    )

                except Exception as e:
                    failed_count += 1
                    pending_service.mark_failed(change.id, str(e))
                    logger.exception(
                        "config_apply_service.exception_applying",
                        change=change.id,
                        error=e,
                    )
                    results.append(
                        {
                            "id": change.id,
                            "config_type": change.config_type,
                            "status": "error",
                            "error": str(e),
                        }
                    )

            return {
                "status": "success",
                "applied": applied_count,
                "failed": failed_count,
                "results": results,
            }

        except Exception as e:
            logger.exception(
                "config_apply_service.error",
                error=e,
            )
            raise

    def apply_graceful_change(
        self,
        pending_id: str,
        max_wait_seconds: int = 60,
    ) -> dict[str, Any]:
        """
        Apply a graceful config change.

        Waits for in-flight operations to complete before applying.

        Args:
            pending_id: Pending config ID
            max_wait_seconds: Maximum wait time (seconds)

        Returns:
            Application result dictionary
        """
        governance = ProviderRegistry.governance.get()
        governance_result = governance.check_all_governance(
            check_kill_switch=False,
            check_emergency=True,
            emergency_min_level=2,
            check_error_budget=False,
            operation_name="apply_graceful_change",
            service_name="ConfigApplyService",
            domain="config",
        )

        if not governance_result.allowed:
            return {
                "status": "blocked",
                "reason": governance_result.block_message,
            }

        try:
            from baldur.services.in_progress_tracker import get_in_progress_tracker
            from baldur.services.pending_config import get_pending_config_service

            try:
                from baldur_pro.services.runtime_config import (
                    get_runtime_config_manager,
                )
            except ImportError:
                get_runtime_config_manager = None  # type: ignore[assignment,misc]

            if get_runtime_config_manager is None:
                return {
                    "status": "blocked",
                    "reason": "runtime_config_manager_unavailable",
                }

            pending_service = get_pending_config_service()
            config_manager = get_runtime_config_manager()
            tracker = get_in_progress_tracker()

            # PendingConfigService exposes get_due_changes() only; look up by id.
            change = next(
                (c for c in pending_service.get_due_changes() if c.id == pending_id),
                None,
            )
            if not change:
                return {
                    "status": "error",
                    "error": f"Change not found: {pending_id}",
                }

            in_progress = tracker.count_in_progress(change.config_type)

            if in_progress > 0:
                return {
                    "status": "retry",
                    "in_progress_count": in_progress,
                    "message": f"{in_progress} operations in progress",
                }

            return config_manager.apply_pending_change(pending_id)

        except Exception as e:
            logger.exception(
                "config_apply_service.graceful_apply_error",
                error=e,
            )
            return {
                "status": "error",
                "error": str(e),
            }


# =============================================================================
# Factory Functions
# =============================================================================


_config_apply_service_instance: ConfigApplyService | None = None
_config_apply_service_instance_lock = threading.Lock()


def get_config_apply_service() -> ConfigApplyService:
    """Return ConfigApplyService singleton instance."""
    global _config_apply_service_instance
    if _config_apply_service_instance is None:
        with _config_apply_service_instance_lock:
            if _config_apply_service_instance is None:
                _config_apply_service_instance = ConfigApplyService()
    return _config_apply_service_instance


def reset_config_apply_service() -> None:
    """Reset singleton instance for test isolation."""
    global _config_apply_service_instance
    _config_apply_service_instance = None
    ConfigApplyService._instance = None
