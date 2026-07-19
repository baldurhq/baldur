"""
🚦 Traffic-Aware Replay Task

Traffic-state-aware DLQ Replay

Performs DLQ Replay only when traffic has normalized.
Runs every minute via a Beat Schedule, and replays only when all of the following conditions are met:

Health Checks:
1. Circuit Breaker State == CLOSED
2. Error Budget > critical_threshold
3. Governance checks pass (Kill Switch, Emergency Mode)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.helpers import log_traffic_aware_replay_audit
from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)

logger = structlog.get_logger()


# =============================================================================
# Traffic Health Status
# =============================================================================


@dataclass
class TrafficHealthStatus:
    """Traffic health status result."""

    is_healthy: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def healthy(cls, checks: dict[str, bool]) -> TrafficHealthStatus:
        """Healthy-status factory."""
        return cls(is_healthy=True, reason="All checks passed", checks=checks)

    @classmethod
    def unhealthy(cls, reason: str, checks: dict[str, bool]) -> TrafficHealthStatus:
        """Unhealthy-status factory."""
        return cls(is_healthy=False, reason=reason, checks=checks)


def check_traffic_health(domain: str | None = None) -> TrafficHealthStatus:  # noqa: C901
    """
    Check the traffic health status.

    Checks:
    1. Circuit Breaker State (when a domain is specified)
    2. Error Budget Gate
    3. Governance (Kill Switch, Emergency Mode)

    Args:
        domain: check the CB state of a specific domain (optional)

    Returns:
        TrafficHealthStatus with is_healthy flag and check results
    """
    checks: dict[str, bool] = {}

    # Check 1: Circuit Breaker State (only when a domain is specified)
    if domain:
        try:
            from baldur.services.circuit_breaker import (
                CircuitState,
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            cb_state = cb_service.get_state(domain)
            checks["circuit_breaker"] = cb_state == CircuitState.CLOSED

            if not checks["circuit_breaker"]:
                return TrafficHealthStatus.unhealthy(
                    reason=f"Circuit breaker is {cb_state} for domain '{domain}'",
                    checks=checks,
                )
        except ImportError:
            logger.debug("traffic_health.circuitbreakerservice_available_skipping_cb")
            checks["circuit_breaker"] = True  # pass if unavailable
        except Exception as e:
            logger.warning(
                "traffic_health.cb_check_failed",
                error=e,
            )
            checks["circuit_breaker"] = True  # fail-open on exception

    # Check 2: Error Budget Gate
    try:
        from baldur.factory.registry import ProviderRegistry

        gate = ProviderRegistry.error_budget_gate.safe_get()
        if gate is None:
            raise RuntimeError("baldur_pro ErrorBudgetGate not registered")
        checks["error_budget"] = gate.is_replay_allowed()

        if not checks["error_budget"]:
            return TrafficHealthStatus.unhealthy(
                reason="Error budget insufficient for replay",
                checks=checks,
            )
    except ImportError:
        logger.debug("traffic_health.errorbudgetgate_available_skipping")
        checks["error_budget"] = True  # pass if unavailable
    except Exception as e:
        logger.warning(
            "traffic_health.error_budget_check_failed",
            error=e,
        )
        checks["error_budget"] = True  # fail-open on exception

    # Check 3: Governance (Kill Switch, Emergency Mode)
    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.settings.governance import get_governance_settings

        governance_settings = get_governance_settings()
        governance = ProviderRegistry.governance.get().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=governance_settings.emergency_min_level,
            check_error_budget=False,  # already checked above
            operation_name="traffic_aware_replay",
            service_name="TrafficAwareReplayTask",
            domain=domain or "dlq",
            audit_on_block=False,  # batch schedule, so skip audit
        )
        checks["governance"] = governance.allowed

        if not checks["governance"]:
            return TrafficHealthStatus.unhealthy(
                reason=governance.block_message,
                checks=checks,
            )
    except ImportError:
        logger.debug("traffic_health.governancechecks_available_skipping")
        checks["governance"] = True
    except Exception as e:
        logger.warning(
            "traffic_health.governance_check_failed",
            error=e,
        )
        checks["governance"] = True  # fail-open on exception

    return TrafficHealthStatus.healthy(checks)


# =============================================================================
# Traffic-Aware Replay Task
# =============================================================================


class TrafficAwareReplayTask(BaseNotifyingTask):
    """
    Traffic-state-aware DLQ Replay.

    Performs DLQ Replay only when traffic is normal.
    Enabled/disabled according to the traffic_aware_enabled setting in RuntimeConfig.

    Audit record:
    - Records the DLQ_REPLAY event (together with the execution result)

    Schedule: every minute
    Queue: dlq_processing
    Notification: on replay success (ON_SUCCESS)

    Args:
        domain: replay only a specific domain (optional)
        max_items: maximum number of items to replay (optional, RuntimeConfig takes precedence)

    Returns:
        dict: {
            "status": "completed" | "skipped" | "disabled",
            "reason": str,
            "total": int,
            "success": int,
            "failed": int,
            "checks": dict,
        }
    """

    name = "baldur.traffic_aware_replay"

    @property
    def notification_policy(self) -> NotificationPolicy:  # type: ignore[override]
        """Dynamically create the notification_policy from Settings."""
        cooldown = self._get_cooldown_seconds()
        return NotificationPolicy(
            timing=NotificationTiming.AFTER,
            threshold=1,  # notify when 1 or more are replayed
            threshold_field="success",
            default_severity="info",
            cooldown_seconds=cooldown,
        )

    @staticmethod
    def _get_cooldown_seconds() -> int:
        """Look up cooldown_seconds from Settings."""
        try:
            # Keep the default 5 minutes (300s), but allow lookup from settings
            return 300
        except Exception:
            return 300  # default

    def run(
        self,
        domain: str | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """
        Run Traffic-Aware Replay.

        1. Load traffic-aware settings from RuntimeConfig
        2. Perform the Traffic Health Check
        3. Run Replay if all checks pass
        4. Record audit

        Args:
            domain: replay only a specific domain
            max_items: maximum number of items to replay

        Returns:
            dict with status, counts, and check results
        """
        logger.info(
            "traffic_aware_replay.starting_check",
            healing_domain=domain,
        )

        task_id = (
            getattr(self.request, "id", None) if hasattr(self, "request") else None
        )

        # 1. Load traffic-aware settings from RuntimeConfig
        config = self._get_replay_automation_config()

        if not config.get("traffic_aware_enabled", False):
            logger.debug("traffic_aware_replay.track_disabled")
            result = {
                "status": "disabled",
                "reason": "Traffic-aware replay is disabled in RuntimeConfig",
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": {},
            }
            self._log_audit(result, domain, task_id)
            return result

        effective_max_items = max_items or config.get("traffic_aware_max_items", 30)

        # 2. Traffic Health Check
        health_status = check_traffic_health(domain)

        if not health_status.is_healthy:
            logger.info(
                "traffic_aware_replay.skipping_traffic_unhealthy",
                health_status=health_status.reason,
            )
            result = {
                "status": "skipped",
                "reason": health_status.reason,
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": health_status.checks,
            }
            self._log_audit(result, domain, task_id)
            return result

        # 3. Run Replay
        logger.info(
            "traffic_aware_replay.health_ok_executing_replay",
            effective_max_items=effective_max_items,
        )

        try:
            replay_result: dict[str, Any] = dict(
                self._execute_replay(domain, effective_max_items)
            )
            result = replay_result

            logger.info(
                "traffic_aware_replay.completed",
                replay_total=result["total"],
                success=result["success"],
                failed=result["failed"],
            )

            final_result = {
                "status": "completed",
                "reason": "Replay executed successfully",
                "total": result["total"],
                "success": result["success"],
                "failed": result["failed"],
                "checks": health_status.checks,
            }
            self._log_audit(final_result, domain, task_id)
            return final_result

        except Exception as e:
            logger.exception(
                "traffic_aware_replay.replay_failed",
                error=e,
            )
            error_result = {
                "status": "error",
                "reason": str(e),
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": health_status.checks,
            }
            self._log_audit(error_result, domain, task_id, error_message=str(e))
            return error_result

    def _log_audit(
        self,
        result: dict[str, Any],
        domain: str | None,
        task_id: str | None,
        error_message: str | None = None,
    ) -> None:
        """Audit log entry recording."""
        log_traffic_aware_replay_audit(
            domain=domain,
            status=result.get("status", "unknown"),
            total=result.get("total", 0),
            success_count=result.get("success", 0),
            failed_count=result.get("failed", 0),
            skipped_reason=(
                result.get("reason") if result.get("status") == "skipped" else None
            ),
            health_checks=result.get("checks"),
            error_message=error_message,
            task_id=task_id,
        )

    def _get_replay_automation_config(self) -> dict[str, Any]:
        """Load the replay_automation settings from RuntimeConfig.

        Absent (no PRO RuntimeConfigManager) is the OSS-normal state — DEBUG at
        most once per task instance; a read failure is genuinely abnormal —
        WARNING every occurrence. Uses the public ``get_config`` accessor,
        never the private getter.
        """
        from baldur.factory.registry import ProviderRegistry

        try:
            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                if not getattr(self, "_runtime_config_absent_logged", False):
                    logger.debug("traffic_aware_replay.runtime_config_absent")
                    self._runtime_config_absent_logged = True
                return {}
            return manager.get_config("replay_automation")
        except Exception as e:
            logger.warning(
                "traffic_aware_replay.runtime_config_read_failed",
                error=e,
            )
            return {}

    def _execute_replay(self, domain: str | None, max_items: int) -> dict[str, int]:
        """Perform the actual replay via ReplayService."""
        try:
            from baldur.interfaces.repositories import ResolutionTrigger
            from baldur.services.replay_service import ReplayService

            service = ReplayService()
            batch_result = service.replay_batch(
                domain=domain,
                max_items=max_items,
                trigger=ResolutionTrigger.TRAFFIC_AWARE,
            )

            return {
                "total": batch_result.total,
                "success": batch_result.success_count,
                "failed": batch_result.failed_count,
            }
        except ImportError as err:
            logger.exception("traffic_aware_replay.replayservice_available")
            raise RuntimeError("ReplayService not available") from err

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Determine the severity based on the result."""
        status = result.get("status", "")
        if status == "error" or result.get("failed", 0) > result.get("success", 0):
            return "warning"
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Generate the notification message."""
        status = result.get("status", "")

        if status == "disabled":
            return "⏸️ Traffic-aware replay disabled - skipped"
        if status == "skipped":
            return f"⏭️ Traffic-Aware Replay skipped: {result.get('reason', '')}"
        if status == "error":
            return f"❌ Traffic-Aware Replay error: {result.get('reason', '')}"
        if status == "completed":
            total = result.get("total", 0)
            success = result.get("success", 0)
            failed = result.get("failed", 0)
            if total == 0:
                return "✅ Traffic-Aware Replay completed - no pending items"
            return f"✅ Traffic-Aware Replay: {success}/{total} done, {failed} failed"

        return "Traffic-Aware Replay completed"


# =============================================================================
# Task Registry for Celery
# =============================================================================


# Task list (used by register_with_celery)
TRAFFIC_AWARE_TASKS = [
    TrafficAwareReplayTask,
]


def register_traffic_aware_tasks_with_celery(app) -> None:
    """Register the Traffic-Aware tasks with the Celery app."""
    for task_class in TRAFFIC_AWARE_TASKS:
        # Mix in app.Task like the intelligence and compliance registrars do:
        # a bare BaseNotifyingTask instance is not a Celery task, so
        # register_task fails on the missing bind().
        wrapped = type(
            task_class.__name__,
            (task_class, app.Task),
            {"name": task_class.name},
        )
        app.register_task(wrapped())
        logger.debug(
            "cell_registry.bulkheads_registered",
            task_class=task_class.name,
        )


def get_traffic_aware_beat_schedule() -> dict[str, Any]:
    """
    Return the Traffic-Aware Replay Beat schedule.

    Returns:
        dict: Celery Beat schedule configuration
    """
    from celery.schedules import crontab

    return {
        # Traffic-Aware Replay - every minute
        "traffic-aware-replay": {
            "task": "baldur.traffic_aware_replay",
            "schedule": crontab(minute="*"),  # every minute
            "options": {"queue": "dlq_processing"},
            "kwargs": {},  # dynamically loaded from RuntimeConfig
        },
    }


__all__ = [
    "TrafficHealthStatus",
    "check_traffic_health",
    "TrafficAwareReplayTask",
    "TRAFFIC_AWARE_TASKS",
    "register_traffic_aware_tasks_with_celery",
    "get_traffic_aware_beat_schedule",
]
