"""
SLA 알림 비동기 전송 Celery 태스크.

SLA 위반 이벤트(Warning/Critical/Recovered)를 비동기로 처리합니다.
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
    name="baldur.adapters.celery.tasks.send_sla_notification",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def send_sla_notification(
    self,
    event_data: dict,
    notification_type: str,
) -> dict[str, Any]:
    """
    SLA 알림 비동기 전송 태스크.

    Args:
        event_data: SLA 이벤트 데이터 (rtt_ms, threshold_ms, service_name 등)
        notification_type: 알림 유형 ("warning", "critical", "recovered")

    Returns:
        전송 결과 딕셔너리
    """
    try:
        from baldur_pro.services.throttle.sla_notification import (
            _send_limit_recovered_sync,
            _send_sla_critical_sync,
            _send_sla_warning_sync,
        )
    except ImportError:
        _send_limit_recovered_sync = None  # type: ignore[assignment,misc]
        _send_sla_critical_sync = None  # type: ignore[assignment,misc]
        _send_sla_warning_sync = None  # type: ignore[assignment,misc]

    dispatch = {
        "warning": _send_sla_warning_sync,
        "critical": _send_sla_critical_sync,
        "recovered": _send_limit_recovered_sync,
    }

    handler = dispatch.get(notification_type)
    if handler:
        handler(event_data)
        logger.info(
            "send_sla_notification.sent_notification_attempt",
            notification_type=notification_type,
            retry_attempt=self.request.retries + 1,
        )
    else:
        logger.warning(
            "send_sla_notification.unknown_notification_type",
            notification_type=notification_type,
        )

    return {"status": "sent", "type": notification_type}
