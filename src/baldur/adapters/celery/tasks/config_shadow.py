"""
Config Shadow Celery Tasks.

Shadow Evaluation 비동기 실행을 Celery 태스크로 래핑한다.
"""

from __future__ import annotations

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.tasks.config_shadow.run_shadow_evaluation",
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
)
def run_shadow_evaluation(
    self,
    evaluation_id: str,
    config_type: str = "",
    baseline_config: dict | None = None,
    candidate_config: dict | None = None,
    service_name: str = "",
    time_window_hours: int = 336,
    region: str = "",
    rollout_id: str | None = None,
) -> dict:
    """Shadow Evaluation을 비동기로 실행한다."""
    try:
        from baldur.services.config_shadow import (
            get_shadow_evaluator_service,
        )

        service = get_shadow_evaluator_service()
        evaluation = service.execute_from_params(
            evaluation_id=evaluation_id,
            config_type=config_type,
            baseline_config=baseline_config or {},
            candidate_config=candidate_config or {},
            service_name=service_name,
            time_window_hours=time_window_hours,
            region=region,
            rollout_id=rollout_id,
        )
        return {
            "evaluation_id": evaluation.evaluation_id,
            "status": evaluation.status.value,
            "passed": evaluation.report.passed if evaluation.report else None,
        }
    except self.MaxRetriesExceededError:
        logger.exception(
            "config_shadow.task_max_retries_exceeded",
            evaluation_id=evaluation_id,
        )
        raise
    except Exception as exc:
        logger.exception(
            "config_shadow.task_failed",
            evaluation_id=evaluation_id,
            error=str(exc),
        )
        raise self.retry(exc=exc) from exc
