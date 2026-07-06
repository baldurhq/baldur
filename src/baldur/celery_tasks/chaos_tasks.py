"""
Chaos Engineering Celery Tasks

Tasks for chaos engineering safety mechanisms including Recovery Monitoring.
Zombie Hunter task is registered via tasks/chaos_scheduler.py:register_celery_tasks().
"""

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.celery_tasks.check_recovery_monitoring",
    queue="chaos_monitoring",
    max_retries=0,
    time_limit=60,
    soft_time_limit=55,
)
def check_recovery_monitoring_experiments(self) -> dict:  # noqa: C901
    """
    Check RECOVERY_MONITORING state experiments for Canary recovery completion.

    This task should be scheduled via Celery Beat every 30 seconds.
    It polls experiments in RECOVERY_MONITORING state and:
    1. Checks if Canary recovery is complete → marks COMPLETED
    2. Checks if Hard TTL expired → force completes

    Returns:
        Dictionary with check results
    """
    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    bound_logger.info("chaos_monitor.recovery_monitoring_checked")

    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.models.experiment import ExperimentStatus

        scheduler = ProviderRegistry.chaos_scheduler.safe_get()
        if scheduler is None:
            raise RuntimeError("baldur_pro ChaosScheduler not registered")
        monitoring_experiments = scheduler.get_experiments_by_status(
            ExperimentStatus.RECOVERY_MONITORING.value
        )

        checked = 0
        completed = 0
        force_completed = 0
        errors = []

        for experiment in monitoring_experiments:
            try:
                checked += 1
                exp_id = getattr(experiment, "experiment_id", "unknown")

                # Check Canary recovery
                canary_status = {}
                if hasattr(experiment, "_verify_canary_recovery"):
                    canary_status = experiment._verify_canary_recovery()

                # If not in canary anymore, recovery complete
                if not canary_status.get("in_canary", True):
                    if hasattr(experiment, "complete_recovery_monitoring"):
                        experiment.complete_recovery_monitoring()
                        completed += 1
                        bound_logger.info(
                            "chaos_recovery_monitor.experiment_recovery_completed",
                            exp_id=exp_id,
                        )

                        # Unregister from scheduler
                        scheduler.unregister_experiment_instance(exp_id)
                    continue

                # Check Hard TTL
                if (
                    hasattr(experiment, "is_hard_ttl_expired")
                    and experiment.is_hard_ttl_expired()
                ) and hasattr(experiment, "force_complete"):
                    experiment.force_complete(reason="hard_ttl_expired")
                    force_completed += 1
                    bound_logger.warning(
                        "chaos_recovery_monitor.experiment_force_completed_hard",
                        exp_id=exp_id,
                    )

                    # Unregister from scheduler
                    scheduler.unregister_experiment_instance(exp_id)

            except Exception as e:
                exp_id = getattr(experiment, "experiment_id", "unknown")
                bound_logger.warning(
                    "chaos_recovery_monitor.error_checking",
                    exp_id=exp_id,
                    error=e,
                )
                errors.append({"experiment_id": exp_id, "error": str(e)})

        result = {
            "success": True,
            "checked": checked,
            "completed": completed,
            "force_completed": force_completed,
            "errors": errors,
        }

        if checked > 0:
            bound_logger.info(
                "chaos_recovery_monitor.checked_experiments_completed_force",
                checked=checked,
                completed=completed,
                force_completed=force_completed,
            )

        return result

    except Exception as e:
        bound_logger.exception(
            "chaos_recovery_monitor.task_failed",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }
