"""
Configuration Apply Tasks.

Celery tasks for applying scheduled/delayed configuration changes.

Thin Task, Fat Service Architecture:
    - 이 파일의 Celery Task들은 단순 위임자 역할만 수행
    - 모든 비즈니스 로직은 ConfigApplyService에서 처리
    - 거버넌스 체크 (Emergency Mode)도 서비스 레이어에서 수행

Tasks:
- apply_pending_config_changes: Apply all due pending changes
- apply_graceful_config_change: Wait for in-progress ops, then apply
"""

from typing import Any

import structlog
from celery import shared_task

from baldur.audit.helpers import log_config_apply_audit
from baldur.settings.apply_strategy import get_apply_strategy_settings

logger = structlog.get_logger()

# 모듈 로드 시점에 설정값 캐싱
_apply_settings = get_apply_strategy_settings()


@shared_task(
    name="baldur.apply_pending_config_changes",
    bind=True,
    max_retries=_apply_settings.pending_max_retries,
    default_retry_delay=_apply_settings.pending_retry_delay,
)
def apply_pending_config_changes(self):
    """
    Apply all pending configuration changes that are due.

    This task is a thin wrapper that delegates to ConfigApplyService.
    All governance checks (Emergency Mode) are performed in the service layer.

    Audit 기록:
    - 설정 적용 성공/실패/차단 시 CONFIG_CHANGE 이벤트 기록

    Note:
        - Kill Switch는 체크하지 않음 (복구 퇴로 확보)
        - Emergency Mode LEVEL_2+ 시 차단

    This task should be scheduled to run periodically (e.g., every 5 seconds)
    via Celery Beat.
    """
    from baldur.services.execution_services import get_config_apply_service

    task_id = self.request.id

    try:
        service = get_config_apply_service()
        result = service.apply_pending_changes()

        status = result.get("status", "unknown")
        if status == "blocked":
            logger.warning(
                "config_task.blocked",
                reason=result.get("reason"),
            )

        # === Audit ===
        # ConfigApplyService.apply_pending_changes() returns the count under
        # key "applied" (not "applied_count"); read the correct key so the
        # audit reflects the real number of applied changes once this path is
        # live (was always recording 0).
        log_config_apply_audit(
            config_key="pending_changes",
            status=status,
            task_id=task_id,
            details={
                "applied_count": result.get("applied", 0),
                "blocked_reason": result.get("reason"),
            },
        )

        return result

    except Exception as e:
        logger.exception(
            "config_task.error",
            error=e,
        )

        # === Audit (failure) ===
        log_config_apply_audit(
            config_key="pending_changes",
            status="failed",
            error_message=str(e),
            task_id=task_id,
        )

        raise self.retry(exc=e) from e


@shared_task(
    name="baldur.apply_graceful_config_change",
    bind=True,
    max_retries=_apply_settings.graceful_max_retries,
    default_retry_delay=_apply_settings.graceful_retry_delay,
)
def apply_graceful_config_change(self, pending_id: str, max_wait_seconds: int = 60):
    """
    Apply a configuration change gracefully.

    This task is a thin wrapper that delegates to ConfigApplyService.
    Waits for in-progress operations to complete before applying.

    Audit 기록:
    - 설정 적용 성공/실패/차단 시 CONFIG_CHANGE 이벤트 기록

    Args:
        pending_id: ID of the pending configuration change
        max_wait_seconds: Maximum time to wait for in-progress ops
    """
    from baldur.services.execution_services import get_config_apply_service

    task_id = self.request.id

    try:
        service = get_config_apply_service()
        result = service.apply_graceful_change(pending_id, max_wait_seconds)

        status = result.get("status", "unknown")

        if status == "blocked":
            # 비상 모드에서는 재시도하여 비상 모드 해제 후 적용
            if self.request.retries < self.max_retries:
                logger.info("config_task.retry_after_emergency_mode")
                raise self.retry(countdown=30)

            # === Audit (blocked) ===
            log_config_apply_audit(
                pending_id=pending_id,
                status="blocked",
                task_id=task_id,
                details={"reason": result.get("reason")},
            )

            return result

        if status == "retry":
            # 진행 중인 작업이 있으면 재시도
            logger.info(
                "config_task.waiting_progress_ops_retry",
                pending_id=pending_id,
                retry_attempt=self.request.retries + 1,
            )
            raise self.retry(countdown=min(5 * (self.request.retries + 1), 30))

        # === Audit (success) ===
        log_config_apply_audit(
            pending_id=pending_id,
            config_key=result.get("config_key"),
            old_value=result.get("old_value"),
            new_value=result.get("new_value"),
            status=status,
            task_id=task_id,
        )

        return result

    except Exception as e:
        if self.request.retries >= self.max_retries:
            # Max retries reached, apply anyway
            logger.warning(
                "config_task.max_retries_reached_applying",
                pending_id=pending_id,
            )
            try:
                from baldur_pro.services.runtime_config import (
                    get_runtime_config_manager,
                )

                config_manager = get_runtime_config_manager()
                apply_result = config_manager.apply_pending_change(pending_id)

                # === Audit (force-applied) ===
                log_config_apply_audit(
                    pending_id=pending_id,
                    status="force_applied",
                    task_id=task_id,
                    details={"reason": "max_retries_reached"},
                )

                return apply_result
            except Exception as apply_error:
                from baldur.services.pending_config import (
                    get_pending_config_service,
                )

                pending_service = get_pending_config_service()
                pending_service.mark_failed(pending_id, str(apply_error))

                # === Audit (failure) ===
                log_config_apply_audit(
                    pending_id=pending_id,
                    status="failed",
                    error_message=str(apply_error),
                    task_id=task_id,
                )

                raise

        raise self.retry(exc=e) from e


@shared_task(name="baldur.cleanup_expired_config_changes")
def cleanup_expired_config_changes(max_age_hours: int | None = None):
    """
    Cleanup old pending changes that were never applied.

    Should be scheduled to run periodically (e.g., daily).

    Args:
        max_age_hours: 만료 기준 시간 (기본값: 설정에서 로드)
    """
    from baldur.services.pending_config import get_pending_config_service

    # 설정값 사용 (인자가 None이면 설정에서 가져옴)
    if max_age_hours is None:
        max_age_hours = _apply_settings.cleanup_max_age_hours

    try:
        pending_service = get_pending_config_service()
        count = pending_service.cleanup_expired(max_age_hours)

        return {
            "status": "success",
            "expired_count": count,
        }
    except Exception as e:
        logger.exception(
            "config_task.error_cleaning_up_expired",
            error=e,
        )
        return {
            "status": "error",
            "error": str(e),
        }


def get_config_apply_beat_schedule() -> dict[str, dict[str, Any]]:
    """Get Celery Beat schedule for applying pending config changes.

    Drives ``apply_pending_config_changes`` on a 30s cadence so DELAYED /
    GRACEFUL config changes are actually applied. This is the canonical
    multi-host single-execution path: one beat process schedules; any worker
    runs the task. Queue ``maintenance`` is used over ``realtime`` because the
    latter carries a 30s ``x-message-ttl`` that would race a 30s-period task.

    Usage:
        from baldur.tasks.config_apply import get_config_apply_beat_schedule

        CELERY_BEAT_SCHEDULE.update(get_config_apply_beat_schedule())
    """
    return {
        "apply-pending-config-changes": {
            "task": "baldur.apply_pending_config_changes",
            "schedule": 30.0,
            "options": {"queue": "maintenance"},
        },
    }
