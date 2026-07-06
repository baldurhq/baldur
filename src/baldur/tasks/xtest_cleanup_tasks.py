"""
X-Test Artifact Cleanup Celery Tasks

X-Test 세션 종료 후 테스트 아티팩트 자동 정리를 위한 Celery 태스크.

Thin Task, Fat Service 원칙:
- 이 파일의 함수들은 단순 위임자 역할만 수행
- 모든 비즈니스 로직은 XTestCleanupService에서 처리

Tasks:
1. cleanup_xtest_artifacts - 만료된 X-Test 세션 및 아티팩트 정리

Schedule:
- 30분마다 실행 (설정 가능)
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Thin Task Wrappers
# =============================================================================


def cleanup_xtest_artifacts() -> dict[str, Any]:
    """
    만료된 X-Test 세션 및 관련 아티팩트 정리.

    Thin wrapper로 XTestCleanupService에 위임합니다.

    정리 대상:
    - 만료된 X-Test 세션 메타데이터
    - Circuit Breaker xtest_mode 상태 원복
    - DLQ x-test-mode 항목 삭제
    - Idempotency xtest 키 삭제
    - Rate Limit xtest 카운터 초기화
    - 시나리오 결과 정리

    Returns:
        dict: {
            "success": bool,
            "sessions_cleaned": int,
            "cb_states_restored": int,
            "dlq_entries_purged": int,
            "idempotency_keys_cleared": int,
            "rate_limit_counters_reset": int,
            "scenario_results_cleared": int,
            "errors": list,
        }
    """
    from baldur.services.xtest_cleanup_service import get_xtest_cleanup_service

    try:
        service = get_xtest_cleanup_service()
        result = service.cleanup_expired_sessions()

        logger.info(
            "x_test_cleanup_task.completed",
            sessions_cleaned=result.sessions_cleaned,
            cb_states_restored=result.cb_states_restored,
            dlq_entries_purged=result.dlq_entries_purged,
            idempotency_keys_cleared=result.idempotency_keys_cleared,
        )

        return result.to_dict()

    except Exception as e:
        logger.exception(
            "x_test_cleanup_task.failed",
            error=e,
        )
        raise


def get_xtest_cleanup_stats() -> dict[str, Any]:
    """
    X-Test 정리 대상 통계 조회.

    Returns:
        dict: 정리 대상 통계
    """
    from baldur.services.xtest_cleanup_service import get_xtest_cleanup_service

    try:
        service = get_xtest_cleanup_service()
        return service.get_cleanup_stats()

    except Exception as e:
        logger.exception(
            "x_test_cleanup_task.failed",
            error=e,
        )
        return {"error": str(e)}


# =============================================================================
# Celery Task Registration
# =============================================================================

try:
    from celery import shared_task

    from baldur.settings.xtest_cleanup import get_xtest_cleanup_settings

    # 모듈 로드 시점에 설정값 캐싱
    _xtest_cleanup_settings = get_xtest_cleanup_settings()

    @shared_task(
        name="baldur.cleanup_xtest_artifacts",
        bind=True,
        max_retries=_xtest_cleanup_settings.max_retries,
        default_retry_delay=_xtest_cleanup_settings.retry_delay,
    )
    def cleanup_xtest_artifacts_task(self):
        """Celery task wrapper for cleanup_xtest_artifacts."""
        return cleanup_xtest_artifacts()

    @shared_task(
        name="baldur.get_xtest_cleanup_stats",
        bind=True,
    )
    def get_xtest_cleanup_stats_task(self):
        """Celery task wrapper for get_xtest_cleanup_stats."""
        return get_xtest_cleanup_stats()

    CELERY_TASKS_AVAILABLE = True

except ImportError:
    logger.debug("x_test_cleanup_tasks.celery_available_skipping_task")
    CELERY_TASKS_AVAILABLE = False


# =============================================================================
# Beat Schedule 정의
# =============================================================================


def get_xtest_cleanup_beat_schedule() -> dict[str, Any]:
    """
    X-Test Cleanup Beat Schedule 반환.

    Returns:
        dict: Celery Beat Schedule 설정
    """
    try:
        from celery.schedules import crontab

        from baldur.settings.xtest_cleanup import get_xtest_cleanup_settings

        settings = get_xtest_cleanup_settings()
        interval_minutes = settings.cleanup_interval_minutes

        return {
            # 30분마다 (기본값) - X-Test 아티팩트 정리
            "cleanup-xtest-artifacts": {
                "task": "baldur.cleanup_xtest_artifacts",
                "schedule": crontab(minute=f"*/{interval_minutes}"),
                "options": {"queue": "maintenance"},
            },
        }

    except ImportError:
        logger.debug("x_test_cleanup_tasks.celery_available_beat_schedule")
        return {}


__all__ = [
    # Thin wrapper functions
    "cleanup_xtest_artifacts",
    "get_xtest_cleanup_stats",
    # Beat schedule
    "get_xtest_cleanup_beat_schedule",
    # Celery availability flag
    "CELERY_TASKS_AVAILABLE",
]
