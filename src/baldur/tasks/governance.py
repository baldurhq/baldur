"""
Governance Celery Tasks - Emergency Mode Auto-Recovery + Approval Visibility.

긴급 모드 자동 복귀 및 알림 발송을 위한 Celery 태스크입니다.

Thin Task, Fat Service Architecture:
    - 이 파일의 Celery Task들은 단순 위임자 역할만 수행
    - 모든 비즈니스 로직은 GovernanceService에서 처리
    - 거버넌스 체크도 서비스 레이어에서 수행

Celery Beat 스케줄:
    - check_emergency_mode_expiry: 15분 주기
    - refresh_governance_approval_metrics: 5분 주기 (484 D11)

Usage:
    # settings.py 또는 celery.py에 추가
    CELERY_BEAT_SCHEDULE = {
        "check-emergency-mode-expiry": {
            "task": "baldur.tasks.governance.check_emergency_mode_expiry",
            "schedule": 900.0,  # 15분
        },
    }

"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.audit.helpers import log_governance_task_audit
from baldur.utils.time import utc_now

logger = structlog.get_logger()


def refresh_governance_approval_metrics() -> dict[str, Any]:  # noqa: C901
    """Refresh PENDING approval gauges from RuntimeConfigManager state (484 D11/D3).

    Reads ``ApprovalMixin.get_approval_requests(status="PENDING")`` and
    publishes:
    - ``baldur_governance_pending_approval_requests`` — count of pending
    - ``baldur_governance_oldest_pending_approval_age_seconds`` — age (sec) of
      the oldest PENDING request, or 0 if none.

    Best-effort: gauge update failures are logged and swallowed so this
    observability refresh never breaks the governance hot path.

    Returns:
        dict: {"success": bool, "pending_count": int, "oldest_age_seconds": float}
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        manager = ProviderRegistry.runtime_config_manager.safe_get()
        if manager is None:
            raise RuntimeError("baldur_pro RuntimeConfigManager not registered")
        pending = manager.get_approval_requests(status="PENDING")
        count = len(pending)

        oldest_age_seconds: float = 0.0
        if pending:
            from datetime import datetime

            now = utc_now()
            oldest_at: datetime | None = None
            for req in pending:
                requested_at_str = req.get("requested_at", "")
                if not requested_at_str:
                    continue
                try:
                    requested_at = datetime.fromisoformat(requested_at_str)
                except (ValueError, TypeError):
                    continue
                if oldest_at is None or requested_at < oldest_at:
                    oldest_at = requested_at
            if oldest_at is not None:
                oldest_age_seconds = max(0.0, (now - oldest_at).total_seconds())

        try:
            from baldur.metrics.prometheus import get_metrics

            recorder = getattr(get_metrics(), "governance", None)
            if recorder is not None:
                recorder.set_pending_approval_count(count)
                recorder.set_oldest_pending_approval_age(oldest_age_seconds)
        except Exception as metric_error:
            logger.warning(
                "governance.approval_metrics_publish_failed",
                error=metric_error,
            )

        return {
            "success": True,
            "pending_count": count,
            "oldest_age_seconds": oldest_age_seconds,
        }

    except Exception as e:
        logger.exception(
            "governance.refresh_approval_metrics_failed",
            error=e,
        )
        raise


def check_emergency_mode_expiry(task_id: str | None = None) -> dict[str, Any]:
    """
    Check emergency mode expiry and perform auto-recovery if needed.

    This function is a thin wrapper that delegates to GovernanceService.
    All business logic is implemented in the service layer.

    This should be scheduled via Celery Beat (every 15 minutes).

    Audit 기록:
    - EMERGENCY_MODE_ACTIVATED/DEACTIVATED 이벤트 기록

    Actions (handled by GovernanceService):
    1. Check if emergency mode is active
    2. If 4 hours elapsed: Send warning to Admin
    3. If 6 hours elapsed: Send final warning ("2 hours until auto-restore")
    4. If 8 hours elapsed: Auto-restore to NORMAL mode

    Args:
        task_id: Celery task ID (for audit tracking)

    Returns:
        dict: 실행 결과
    """
    try:
        from baldur_pro.services.governance.service import get_governance_service
    except ImportError:
        get_governance_service = None  # type: ignore[assignment,misc]

    try:
        service = get_governance_service()
        result = service.check_emergency_mode_expiry()
        result_dict = result.to_dict()

        # === Audit ===
        status = result_dict.get("status", "completed")
        auto_recovered = result_dict.get("auto_recovered", False)
        notification_sent = result_dict.get("notification_sent", False)

        log_governance_task_audit(
            action="expiry_check",
            emergency_level=result_dict.get("emergency_level"),
            previous_level=result_dict.get("previous_level"),
            status=status,
            notification_sent=notification_sent,
            auto_recovered=auto_recovered,
            hours_elapsed=result_dict.get("hours_elapsed"),
            task_id=task_id,
        )

        return result_dict

    except Exception as e:
        # === Audit (failure) ===
        log_governance_task_audit(
            action="expiry_check",
            status="failed",
            error_message=str(e),
            task_id=task_id,
        )
        raise


# =============================================================================
# Celery Beat Schedule Configuration
# =============================================================================


def get_governance_beat_schedule() -> dict[str, dict[str, Any]]:
    """
    Get Celery Beat schedule for governance tasks.

    Returns:
        dict: Celery Beat schedule configuration

    Usage:
        # In your celery.py or settings.py:
        from baldur.tasks.governance import get_governance_beat_schedule

        CELERY_BEAT_SCHEDULE.update(get_governance_beat_schedule())
    """
    return {
        "check-emergency-mode-expiry": {
            "task": "baldur.tasks.governance.check_emergency_mode_expiry",
            "schedule": 900.0,  # 15 min (900s)
            "options": {
                # Route to the shared maintenance queue (sibling: expire-manual-overrides).
                # No dedicated "governance" worker is declared, so a bespoke queue name
                # would leave this task scheduled-but-unconsumed.
                "queue": "maintenance",
                "priority": 3,  # maintenance queue max-priority is 3
            },
        },
        # 484 D11 - refresh the PENDING 4-eyes approval-request gauge every 5 min
        "refresh-governance-approval-metrics": {
            "task": "baldur.tasks.governance.refresh_governance_approval_metrics",
            "schedule": 300.0,  # 5 min (300s)
            "options": {
                # Metrics-refresh task -> the metrics queue (sibling: collect-baldur-metrics).
                "queue": "metrics",
                "priority": 1,  # metrics queue max-priority is 1
            },
        },
    }


# =============================================================================
# Celery Task Registration (if using Celery)
# =============================================================================

try:
    from celery import shared_task

    from baldur.settings.governance import get_governance_settings

    # 모듈 로드 시점에 설정값 캐싱
    _governance_settings = get_governance_settings()

    @shared_task(
        name="baldur.tasks.governance.check_emergency_mode_expiry",
        bind=True,
        max_retries=_governance_settings.expiry_check_max_retries,
        default_retry_delay=_governance_settings.expiry_check_retry_delay,
        autoretry_for=(Exception,),
        retry_backoff=True,
    )
    def check_emergency_mode_expiry_task(self) -> dict[str, Any]:
        """
        Celery task wrapper for check_emergency_mode_expiry.

        This is the actual Celery task that should be scheduled.
        """
        return check_emergency_mode_expiry(task_id=self.request.id)

    @shared_task(
        name="baldur.tasks.governance.refresh_governance_approval_metrics",
        bind=True,
        max_retries=1,
        default_retry_delay=600,
    )
    def refresh_governance_approval_metrics_task(self) -> dict[str, Any]:
        """Celery task wrapper for refresh_governance_approval_metrics."""
        return refresh_governance_approval_metrics()

except ImportError:
    # Celery not installed, skip task registration
    logger.debug("governance.celery_installed_skipping_task")
    pass
