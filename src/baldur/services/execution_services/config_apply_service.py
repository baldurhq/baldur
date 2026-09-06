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


def _config_apply_refused_for_entitlement(operation: str) -> bool:
    """Whether applying a config change must be refused for lack of a licence.

    Applying a scheduled or graceful change is PRO behaviour: the manager that
    performs it is a PRO service, and the change *creation* surface is already
    unavailable without an ACTIVE verdict because it resolves through the
    provider registry. This restores the same boundary on the applier, which
    reaches its manager by direct import and so never passed through it.

    Presence is answered first and is not folded into the refusal: an OSS-only
    install has no licence to be the problem, and must keep receiving the
    "manager unavailable" answer it receives today.

    Fails closed, matching the beat lane that composes this service's task.
    """
    from baldur.core.entitlement import is_entitlement_active
    from baldur.utils.tier import is_pro_installed

    if not is_pro_installed():
        return False
    if is_entitlement_active():
        return False

    logger.debug(
        "config_apply_service.skipped_not_entitled",
        operation=operation,
    )
    return True


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
        # Entitlement resolves ahead of governance: without an ACTIVE verdict
        # the PRO governance provider never registers, so the check below would
        # run against the permissive OSS no-op default and report nothing
        # useful. The refusal status is "skipped", not "blocked" — "blocked" is
        # the governance vocabulary, and the beat task raises a WARNING on it.
        if _config_apply_refused_for_entitlement("apply_pending_changes"):
            return {
                "status": "skipped",
                "reason": "not_entitled",
                "message": "Config apply requires an active PRO entitlement",
            }

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
        # Same boundary as the scheduled applier, on the lane the beat gate
        # never covered: this entry has no gated lane in front of it at all.
        if _config_apply_refused_for_entitlement("apply_graceful_change"):
            return {
                "status": "skipped",
                "reason": "not_entitled",
            }

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
