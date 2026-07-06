"""
미들웨어 단위 테스트용 conftest.

Django 미들웨어 단위 테스트에서 JsonResponse 등이 동작하도록
최소한의 Django 설정만 구성합니다.
"""

from django.conf import settings

if not settings.configured:
    settings.configure(
        DEFAULT_CHARSET="utf-8",
    )

import pytest

from baldur.context.cell_context import _current_cell_id


@pytest.fixture(autouse=True)
def _reset_cell_context():
    """테스트 간 ContextVar 상태 초기화 (2+ 파일 공유)."""
    token = _current_cell_id.set(None)
    yield
    _current_cell_id.reset(token)
