"""
Chaos Execution Service

Chaos experiment scheduling and execution service layer.

Thin Task, Fat Service principle:
    - Celery Task (chaos_scheduler.py) simply calls this service
    - All business logic, safety checks, state management happen here

Features:
    - Scheduled experiment execution
    - Pre-flight safety checks (SafetyGuard integration)
    - Daily resilience report generation
    - Pending approval notifications

Reference:
- docs/baldur/CHAOS_ENGINEERING.md
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.factory.registry import ProviderRegistry

from .models import (
    ApprovalCleanupResult,
    DailyReportResult,
    ExperimentExecutionResult,
    PendingApprovalCheckResult,
)

logger = structlog.get_logger()


# =============================================================================
# Chaos Execution Service
# =============================================================================


class ChaosExecutionService:
    """
    Chaos experiment execution service.

    Integrates with SafetyGuard to perform all safety checks and runs
    scheduled Chaos experiments.

    Usage:
        service = get_chaos_execution_service()

        # Run scheduled experiments
        result = service.run_scheduled_experiments()

        # Generate daily report
        report = service.generate_daily_report()
    """

    _instance: ChaosExecutionService | None = None

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

    # =========================================================================
    # Main Entry Points
    # =========================================================================

    def run_scheduled_experiments(self) -> ExperimentExecutionResult:
        """
        Run scheduled chaos experiments.

        Called by Celery Beat every 5 minutes.

        Workflow:
        1. Governance check (Kill Switch, ErrorBudget)
        2. Query scheduled experiments due for execution
        3. SafetyGuard pre-flight check per experiment
        4. Execute approved experiments
        5. Record results

        Returns:
            ExperimentExecutionResult
        """
        result = ExperimentExecutionResult()

        # 1. Governance check - Kill Switch only (Chaos skips Emergency).
        governance = ProviderRegistry.governance.get()
        governance_result = governance.check_all_governance(
            check_kill_switch=True,
            check_emergency=False,
            check_error_budget=True,
            operation_name="run_scheduled_experiments",
            service_name="ChaosExecutionService",
            domain="chaos",
        )

        if not governance_result.allowed:
            result.governance_blocked = True
            result.governance_block_reason = governance_result.block_message
            logger.warning(
                "chaos_execution_service.experiments_blocked_governance",
                governance_result=governance_result.block_message,
            )
            return result

        try:
            scheduler = ProviderRegistry.chaos_scheduler.safe_get()
            safety_guard = ProviderRegistry.safety_guard.safe_get()

            if scheduler is None or safety_guard is None:
                logger.debug("chaos_execution_service.pro_modules_unavailable")
                return result

            # 2. Query experiments due for execution
            due_experiments = scheduler.get_due_experiments()
            result.checked = len(due_experiments)

            if not due_experiments:
                logger.debug("chaos_execution_service.no_experiments_due_execution")
                return result

            logger.info(
                "chaos_execution_service.found_due_experiments",
                due_experiments_count=len(due_experiments),
            )

            # 3. Process each experiment
            for experiment in due_experiments:
                exp_result = self._process_single_experiment(
                    experiment=experiment,
                    scheduler=scheduler,
                    safety_guard=safety_guard,
                )

                if exp_result["status"] == "executed":
                    result.executed += 1
                elif exp_result["status"] == "skipped":
                    result.skipped += 1
                elif exp_result["status"] in ("blocked", "pending_approval"):
                    result.blocked += 1
                elif exp_result["status"] == "error":
                    result.errors.append(exp_result)

                result.experiments.append(exp_result)

            logger.info(
                "chaos_execution_service.completed_executed_skipped_blocked",
                executed_count=result.executed,
                skipped=result.skipped,
                blocked=result.blocked,
            )

        except Exception as e:
            logger.exception("chaos_service.run_scheduled_error")
            result.errors.append({"error": str(e)})

        return result

    def _process_single_experiment(
        self,
        experiment,
        scheduler,
        safety_guard,
    ) -> dict[str, Any]:
        """Process a single experiment."""
        try:
            exp_id = experiment.id

            # Kill Switch check
            if scheduler.is_kill_switch_active():
                logger.warning(
                    "chaos_execution_service.kill_switch_active_skipping",
                    exp_id=exp_id,
                )
                return {
                    "id": exp_id,
                    "status": "blocked",
                    "reason": "kill_switch_active",
                }

            # SafetyGuard pre-flight check
            safety_result = safety_guard.pre_flight_check(
                experiment_type=experiment.experiment_type,
                blast_radius=experiment.blast_radius,
                target_service=experiment.target_service,
            )

            if not safety_result.is_safe:
                logger.warning(
                    "chaos_execution_service.safety_check_failed",
                    exp_id=exp_id,
                    safety_result=safety_result.block_reasons,
                )
                scheduler.skip_experiment(
                    exp_id,
                    reason=f"Safety check failed: {safety_result.block_reasons}",
                )
                return {
                    "id": exp_id,
                    "status": "skipped",
                    "reason": str(safety_result.block_reasons),
                }

            # Approval required check
            if experiment.requires_approval and not experiment.is_approved:
                logger.info(
                    "chaos_execution_service.experiment_awaiting_approval",
                    exp_id=exp_id,
                )
                return {
                    "id": exp_id,
                    "status": "pending_approval",
                }

            # Execute experiment
            exec_result = scheduler.execute_experiment(exp_id)

            if exec_result.success:
                logger.info(
                    "chaos_execution_service.executed_experiment",
                    exp_id=exp_id,
                )
                return {
                    "id": exp_id,
                    "status": "executed",
                    "result": (
                        exec_result.to_dict()
                        if hasattr(exec_result, "to_dict")
                        else str(exec_result)
                    ),
                }
            error_msg = (
                str(exec_result.error)
                if hasattr(exec_result, "error")
                else "Unknown error"
            )
            logger.error(
                "chaos_execution_service.execute_failed",
                exp_id=exp_id,
            )
            return {
                "id": exp_id,
                "status": "error",
                "error": error_msg,
            }

        except Exception as e:
            logger.exception(
                "chaos_execution_service.error_executing",
                experiment=experiment.id,
            )
            return {
                "id": experiment.id,
                "status": "error",
                "error": str(e),
            }

    def generate_daily_report(self) -> DailyReportResult:
        """
        Generate daily resilience report.

        Called daily at 6 AM UTC.

        Returns:
            DailyReportResult
        """
        try:
            from baldur.factory.registry import ProviderRegistry

            generator = ProviderRegistry.report_generator.safe_get()
            if generator is None:
                raise RuntimeError("baldur_pro ReportGenerator not registered")
            report = generator.generate_daily_report()

            logger.info(
                "chaos_execution_service.daily_report_generated",
                chaos_report_id=report.report_id,
                grade=report.grade,
            )

            return DailyReportResult(
                success=True,
                report_id=report.report_id,
                grade=report.grade,
                summary={
                    "total_experiments": report.total_experiments,
                    "passed": report.passed_count,
                    "failed": report.failed_count,
                    "sla_compliance": report.sla_compliance_percent,
                },
            )

        except Exception as e:
            logger.exception("chaos_execution_service.error_generating_daily_report")
            return DailyReportResult(success=False, error=str(e))

    def cleanup_expired_approvals(self) -> ApprovalCleanupResult:
        """
        Clean up expired approval requests.

        Called hourly.

        Returns:
            ApprovalCleanupResult
        """
        result = ApprovalCleanupResult()

        try:
            from baldur.factory.registry import ProviderRegistry

            scheduler = ProviderRegistry.chaos_scheduler.safe_get()
            manager = ProviderRegistry.blast_radius_manager.safe_get()

            if scheduler is None or manager is None:
                logger.debug("chaos_execution_service.pro_modules_unavailable")
                return result

            # Expire pending schedule approvals
            result.schedule_expired = scheduler.expire_pending_approvals()

            # Expire pending blast-radius approvals
            result.blast_radius_expired = manager.expire_pending_approvals()

            total = result.schedule_expired + result.blast_radius_expired
            if total > 0:
                logger.info(
                    "chaos_execution_service.expired_pending_approvals",
                    total_expired_approvals_count=total,
                )

        except Exception as e:
            logger.exception("chaos_execution_service.error_cleaning_up_approvals")
            result.errors.append(str(e))

        return result

    def check_pending_approvals(self) -> PendingApprovalCheckResult:
        """
        Check pending approvals and emit notification.

        Called every 30 minutes.

        Returns:
            PendingApprovalCheckResult
        """
        result = PendingApprovalCheckResult()

        try:
            from baldur.factory.registry import ProviderRegistry

            scheduler = ProviderRegistry.chaos_scheduler.safe_get()
            manager = ProviderRegistry.blast_radius_manager.safe_get()

            if scheduler is None or manager is None:
                logger.debug("chaos_execution_service.pro_modules_unavailable")
                return result

            pending_schedules = scheduler.list_schedules(pending_approval_only=True)
            pending_blast = manager.get_pending_approvals()

            result.pending_schedules = len(pending_schedules)
            result.pending_blast_radius = len(pending_blast)

            total_pending = result.pending_schedules + result.pending_blast_radius

            if total_pending > 0:
                logger.info(
                    "chaos_execution_service.experiments_pending_approval",
                    total_pending=total_pending,
                )

                # Try to send notification
                try:
                    from baldur.interfaces.notification import (
                        send_pending_approval_alert,
                    )

                    send_pending_approval_alert(
                        pending_count=total_pending,
                        schedules=pending_schedules,
                        blast_radius=pending_blast,
                    )
                    result.alerts_sent = 1
                except ImportError:
                    result.alerts_sent = 0
                    result.notification_status = "not_configured"

        except Exception as e:
            logger.exception("chaos_execution_service.error_checking_pending_approvals")
            result.error = str(e)

        return result


# =============================================================================
# Factory Functions
# =============================================================================


_chaos_execution_service_instance: ChaosExecutionService | None = None
_chaos_execution_service_instance_lock = threading.Lock()


def get_chaos_execution_service() -> ChaosExecutionService:
    """Return ChaosExecutionService singleton instance."""
    global _chaos_execution_service_instance
    if _chaos_execution_service_instance is None:
        with _chaos_execution_service_instance_lock:
            if _chaos_execution_service_instance is None:
                _chaos_execution_service_instance = ChaosExecutionService()
    return _chaos_execution_service_instance


def reset_chaos_execution_service() -> None:
    """Reset singleton instance for test isolation."""
    global _chaos_execution_service_instance
    _chaos_execution_service_instance = None
    ChaosExecutionService._instance = None
