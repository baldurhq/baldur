"""
Celery Causation Context 자동 전파.

API 요청에서 시작된 인과관계 정보를 Celery Task로 자동 전파합니다.

Features:
    - before_task_publish: Task 발행 시 causation 헤더 자동 주입
    - task_prerun: Task 시작 시 causation 복원 (미설정 시 시스템 cascade 자동 생성)
    - task_postrun: Task 종료 시 causation 정리

Usage:
    # Celery 앱에서 초기화
    from baldur.context.celery_propagation import setup_celery_causation_propagation
    setup_celery_causation_propagation()

    # 또는 signal_hooks.py의 setup_baldur_signals()에서 자동 호출됨

데이터 흐름:
    API Request → ExceptionHandler → CausationContext.start_cascade()
           ↓
    before_task_publish → headers에 causation 정보 자동 주입
           ↓
    Celery Task → task_prerun → CausationContext 복원 (깊이 증가)
           ↓
    task_postrun → CausationContext 정리
"""

from __future__ import annotations

from typing import Any

import structlog
from celery.signals import before_task_publish

from baldur.context.causation_context import (
    CELERY_HEADER_CASCADE_ID,
    CausationContext,
    CausationInfo,
    _current_causation,
    get_causation_for_celery,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# before_task_publish 시그널 핸들러
# =============================================================================


_before_task_publish_connected = False


@before_task_publish.connect
def on_before_task_publish(
    sender: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    **kwargs,
) -> None:
    """
    Celery Task 발행 전 causation 헤더 자동 주입.

    현재 CausationContext가 설정되어 있으면 헤더에 자동으로 포함합니다.
    개발자가 수동으로 headers=get_causation_for_celery()를 호출할 필요 없습니다.

    Args:
        sender: Task 이름
        body: Task 메시지 본문
        headers: Task 메시지 헤더 (수정 가능)
    """
    if headers is None:
        return

    # 이미 causation 헤더가 있으면 덮어쓰지 않음 (명시적 설정 우선)
    if headers.get(CELERY_HEADER_CASCADE_ID):
        logger.debug(
            "causation_propagation.causation_headers_already_set",
            sender=sender,
        )
        return

    # 현재 CausationContext에서 헤더 생성
    causation_headers = get_causation_for_celery()

    if causation_headers:
        headers.update(causation_headers)
        logger.debug(
            "causation_propagation.injected_causation_headers_task",
            sender=sender,
            causation_headers=causation_headers.get(CELERY_HEADER_CASCADE_ID),
        )


# =============================================================================
# System-initiated Cascade 자동 생성 (task_prerun용 헬퍼)
# =============================================================================


def ensure_causation_context_for_task(
    task_name: str,
    task_id: str,
) -> Any | None:
    """
    Celery Task 시작 시 CausationContext 보장.

    호출 체인에서 전파된 causation이 없으면 시스템 cascade를 자동 생성합니다.
    X-Test-Mode에서는 XTC- 프리픽스가 자동 추가됩니다.

    Args:
        task_name: Task 이름
        task_id: Task ID

    Returns:
        설정된 causation ContextVar token (정리용)
    """
    # 이미 설정되어 있으면 건드리지 않음
    if CausationContext.is_set():
        return None

    # 시스템 cascade 생성 (Celery Beat, 독립 실행 등)
    import uuid

    from baldur.context.causation_context import _get_xtest_id_prefix

    # source 결정: 스케줄러 여부 확인
    source = _detect_task_source(task_name)

    # X-Test-Mode 시 XTC- 프리픽스 적용
    prefix = _get_xtest_id_prefix()
    system_event_id = f"{prefix}SYSTEM_ROOT_{source}_{uuid.uuid4().hex[:8]}"
    cascade_id = f"{prefix}cascade-{uuid.uuid4().hex[:12]}"

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=system_event_id,
        chain_depth=0,
        namespace="global",
        metadata={
            "system_source": source,
            "auto_generated": True,
            "task_name": task_name,
            "task_id": task_id,
            "created_at": utc_now().isoformat(),
        },
    )

    token = _current_causation.set(info)

    logger.debug(
        "causation_propagation.auto_created_system_cascade",
        source=source,
        cascade_id=cascade_id,
        task_name=task_name,
    )

    return token


def _detect_task_source(task_name: str) -> str:
    """
    Task 이름에서 source 유형 추론.

    Args:
        task_name: Celery Task 이름

    Returns:
        source 문자열 (celery_beat, management_cmd, worker 등)
    """
    task_name_lower = task_name.lower()

    # 스케줄러 관련 패턴
    if any(pattern in task_name_lower for pattern in ["beat", "schedule", "periodic"]):
        return "celery_beat"

    # 관리 명령 관련 패턴
    if any(pattern in task_name_lower for pattern in ["manage", "command", "admin"]):
        return "management_cmd"

    # 크론/스케줄러 패턴
    if any(pattern in task_name_lower for pattern in ["cron", "cleanup", "expire"]):
        return "scheduler"

    # 기본값
    return "worker"


# =============================================================================
# Setup 함수
# =============================================================================


def setup_celery_causation_propagation() -> None:
    """
    Celery causation 자동 전파 설정.

    before_task_publish 시그널을 연결하여 모든 Task 발행 시
    현재 CausationContext를 자동으로 헤더에 포함합니다.

    Usage:
        from baldur.context.celery_propagation import setup_celery_causation_propagation
        setup_celery_causation_propagation()

    Note:
        signal_hooks.py의 setup_baldur_signals()에서 자동으로 호출됩니다.
    """
    global _before_task_publish_connected

    if _before_task_publish_connected:
        logger.debug("causation_propagation.already_connected")
        return

    # before_task_publish는 @before_task_publish.connect 데코레이터로 이미 연결됨
    # 여기서는 연결 상태만 표시
    _before_task_publish_connected = True

    logger.info("causation_propagation.celery_causation_propagation_enabled")


__all__ = [
    "setup_celery_causation_propagation",
    "ensure_causation_context_for_task",
    "on_before_task_publish",
]
