"""
Cell Context — cell_id ContextVar 전역 전파.

CellTaggingMiddleware에서 설정하며,
미들웨어 체인 외부(서비스 레이어, Celery 발행 시점 등)에서도
cell_id에 접근 가능하게 한다.

기존 패턴 참조:
- context/actor_context.py L53: _current_actor ContextVar
- context/causation_context.py L226: _current_causation ContextVar
- scaling/deadline_context.py L51: _request_deadline ContextVar
"""

from __future__ import annotations

import contextvars
from collections.abc import Generator
from contextlib import contextmanager

_current_cell_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "baldur_cell_id", default=None
)


def get_current_cell_id() -> str | None:
    """현재 컨텍스트의 cell_id 반환."""
    return _current_cell_id.get()


def set_cell_id(cell_id: str) -> contextvars.Token[str | None]:
    """cell_id 설정. 반환된 Token으로 복원 필요."""
    return _current_cell_id.set(cell_id)


@contextmanager
def cell_scope(cell_id: str) -> Generator[str, None, None]:
    """
    cell_id Context Manager.

    사용 예:
        with cell_scope("cell-3"):
            # 이 블록 내에서 get_current_cell_id() == "cell-3"
            task.delay(...)  # before_task_publish에서 cell_id 자동 전파
    """
    token = _current_cell_id.set(cell_id)
    try:
        yield cell_id
    finally:
        _current_cell_id.reset(token)
