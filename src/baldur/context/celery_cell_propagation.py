"""
Celery 태스크에 cell_id 태깅 — 하이브리드 3단계 전파.

전파 우선순위:
1. HTTP 컨텍스트의 cell_id 상속 (ContextVar)
2. kwargs에서 service_name/namespace/domain 추출
3. task_name Fallback (최후의 수단)

기존 전파 패턴 참조:
- context/celery_propagation.py: CausationContext before_task_publish 자동 주입
- adapters/celery/signal_hooks.py L381: on_before_task_publish 핸들러
"""

from __future__ import annotations

from typing import Any

import structlog
from celery.signals import before_task_publish

logger = structlog.get_logger()

# kwargs에서 추출할 라우팅 키 (우선순위 순)
CELERY_ROUTING_KEYS = [
    "service_name",  # CB, Postmortem 태스크
    "namespace",  # Recovery, Incident 태스크
    "domain",  # DLQ Replay 태스크
    "user_id",  # CB Force Open/Close 태스크
]


def _extract_routing_key(kwargs: dict[str, Any]) -> tuple[str, str] | None:
    """
    Celery kwargs에서 라우팅 키 추출.

    Args:
        kwargs: 태스크 kwargs

    Returns:
        (key_name, value) 또는 None

    Note:
        현재 코드베이스의 모든 Celery 태스크는 플랫한 1-depth kwargs를
        사용하므로 단순 dict.get()으로 충분하다.
        향후 중첩 구조 도입 시 이 함수 내부만 수정하면 된다.
    """
    for key in CELERY_ROUTING_KEYS:
        value = kwargs.get(key)
        if value is not None:
            return (key, str(value))
    return None


@before_task_publish.connect
def add_cell_id_to_task(
    sender: str | None = None,
    headers: dict | None = None,
    body: Any = None,
    **kwargs,
) -> None:
    """
    태스크 발행 시 cell_id 삽입 — 하이브리드 3단계.

    기존 on_before_task_publish (celery_propagation.py)와 동일한
    시그널 패턴. 이미 cell_id가 있으면 덮어쓰지 않음.
    """
    if headers is None:
        return

    # 이미 cell_id가 명시적으로 설정된 경우 skip
    if headers.get("cell_id"):
        return

    try:
        from baldur.settings.cell_topology import get_cell_topology_settings

        settings = get_cell_topology_settings()
        if not settings.enabled or not settings.tagging_enabled:
            return

        # ── 1순위: HTTP 컨텍스트의 cell_id 상속 (ContextVar) ──
        from baldur.context.cell_context import get_current_cell_id

        current_cell = get_current_cell_id()
        if current_cell:
            headers["cell_id"] = current_cell
            return

        # ── 2순위: kwargs에서 라우팅 키 추출 ──
        from baldur.services.cell_topology import get_cell_registry

        registry = get_cell_registry()

        # body structure: [args, kwargs, embed] (Celery protocol v2)
        task_kwargs: dict[str, Any] = {}
        if (
            body
            and isinstance(body, (list, tuple))
            and len(body) > 1
            and isinstance(body[1], dict)
        ):
            task_kwargs = body[1]

        routing = _extract_routing_key(task_kwargs)
        if routing:
            key_name, value = routing
            headers["cell_id"] = registry.get_cell_for_key(f"{key_name}:{value}")
            return

        # ── 3순위: task_name Fallback ──
        task_name = headers.get("task", "unknown")
        headers["cell_id"] = registry.get_cell_for_key(f"task:{task_name}")

    except Exception:
        pass  # 태깅 실패 시 무시 — 기존 동작 유지 (Fail-Open)


# extract_cell_id_on_prerun / clear_cell_id_on_postrun은
# context/celery_context_utils.py의 restore_all_task_context / cleanup_all_task_context로 통합되었다.
# 하위 호환을 위해 래퍼 함수 유지.


def extract_cell_id_on_prerun(task: Any = None, **kwargs) -> None:
    """task_prerun 시 cell_id를 ContextVar에 설정 (하위 호환 래퍼)."""
    if task is None:
        return
    try:
        from baldur.context.cell_context import _current_cell_id

        request = getattr(task, "request", None)
        if request is None:
            return
        cell_id = request.get("cell_id") if hasattr(request, "get") else None
        if cell_id:
            token = _current_cell_id.set(cell_id)
            task._cell_id_token = token
    except Exception:
        pass


def clear_cell_id_on_postrun(task: Any = None, **kwargs) -> None:
    """task_postrun 시 cell_id ContextVar 복원 (하위 호환 래퍼)."""
    if task is None:
        return
    try:
        from baldur.context.cell_context import _current_cell_id

        token = getattr(task, "_cell_id_token", None)
        if token is not None:
            _current_cell_id.reset(token)
    except Exception:
        pass
