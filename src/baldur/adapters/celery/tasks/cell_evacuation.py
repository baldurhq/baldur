"""
Cell 대피 관련 비동기 Celery 태스크.

Cell 격리/복구 시 감사 로그 및 이벤트 발행을 비동기로 처리합니다.
CellEvacuationPolicy의 Fire-and-forget 통보에서 호출됩니다.

autoretry로 전송 실패 시 자동 재시도(max 3회, 30초 간격)하며,
acks_late로 Worker 종료 시 미완료 태스크를 브로커에 반환합니다.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_isolation",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_isolation(
    self,
    cell_id: str,
    reason: str,
    duration_seconds: int = 3600,
) -> dict[str, Any]:
    """
    Cell 격리 감사 로그 비동기 발행 태스크.

    RegionalIsolationGate.isolate_region()을 호출하여
    감사 로그 및 전역 이벤트를 기록합니다.

    Args:
        cell_id: 격리 대상 Cell 식별자
        reason: 격리 사유
        duration_seconds: 격리 지속 시간 (초)

    Returns:
        처리 결과 딕셔너리
    """
    from baldur.services.isolation.regional_gate import (
        get_regional_isolation_gate,
    )

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    gate = get_regional_isolation_gate()
    result = gate.isolate_region(
        region=cell_id,
        reason=reason,
        duration_seconds=duration_seconds,
    )

    bound_logger.info(
        "cell_evacuation.isolation_notified",
        cell_id=cell_id,
        reason=reason,
        result=result,
        attempt=self.request.retries + 1,
    )
    return {"status": "notified", "cell_id": cell_id, "isolated": result}


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_blast_radius",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_blast_radius(
    self,
    cell_id: str,
    affected_services: list[str] | None = None,
) -> dict[str, Any]:
    """
    Cell Blast Radius 정책 설정 비동기 태스크.

    BlastRadiusService.set_policy()를 호출하여
    감사 로그를 기록합니다.

    Args:
        cell_id: 대상 Cell 식별자
        affected_services: 영향받는 서비스 목록

    Returns:
        처리 결과 딕셔너리
    """
    from baldur.services.blast_radius.models import BlastRadiusLevel
    from baldur.services.blast_radius.service import BlastRadiusService

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    blast_service = BlastRadiusService()
    blast_service.set_policy(
        service_name=cell_id,
        level=BlastRadiusLevel.CRITICAL,
        affected_services=affected_services or [],
        max_affected_percentage=0.0,
        auto_isolate=True,
    )

    bound_logger.info(
        "cell_evacuation.blast_radius_notified",
        cell_id=cell_id,
        affected_services_count=len(affected_services or []),
        attempt=self.request.retries + 1,
    )
    return {
        "status": "notified",
        "cell_id": cell_id,
        "affected_services_count": len(affected_services or []),
    }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.notify_cell_restoration",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def notify_cell_restoration(
    self,
    cell_id: str,
) -> dict[str, Any]:
    """
    Cell 복구 감사 로그 비동기 발행 태스크.

    RegionalIsolationGate.restore_region()을 호출하여
    복구 감사 로그를 기록합니다.

    Args:
        cell_id: 복구 대상 Cell 식별자

    Returns:
        처리 결과 딕셔너리
    """
    from baldur.services.isolation.regional_gate import (
        get_regional_isolation_gate,
    )

    task_id = self.request.id or "unknown"
    bound_logger = logger.bind(task_id=task_id)

    gate = get_regional_isolation_gate()
    result = gate.restore_region(cell_id)

    bound_logger.info(
        "cell_evacuation.restoration_notified",
        cell_id=cell_id,
        result=result,
        attempt=self.request.retries + 1,
    )
    return {"status": "notified", "cell_id": cell_id, "restored": result}
