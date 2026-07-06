"""
core 단위 테스트 공통 fixture.

cell_context ContextVar 초기화 fixture 등,
core 디렉토리 내 2+ 파일에서 공유하는 fixture를 관리합니다.
"""

from __future__ import annotations

import pytest

from baldur.context.cell_context import _current_cell_id


@pytest.fixture(autouse=True)
def _reset_cell_context():
    """테스트 간 ContextVar 상태 초기화."""
    token = _current_cell_id.set(None)
    yield
    _current_cell_id.reset(token)
