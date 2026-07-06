"""
Cell Context ContextVar 테스트.

ContextVar 설정/해제, cell_scope 컨텍스트 매니저 동작 검증.

참조 소스:
- context/cell_context.py (_current_cell_id, get_current_cell_id, set_cell_id, cell_scope)
"""

from __future__ import annotations

import pytest

from baldur.context.cell_context import (
    _current_cell_id,
    cell_scope,
    get_current_cell_id,
    set_cell_id,
)


class TestCellContextContract:
    """Cell Context 계약 검증."""

    def test_contextvar_name(self):
        """ContextVar 이름이 'baldur_cell_id'이어야 한다."""
        assert _current_cell_id.name == "baldur_cell_id"

    def test_default_value_is_none(self):
        """초기값이 None이어야 한다."""
        assert get_current_cell_id() is None


class TestCellContextBehavior:
    """Cell Context 동작 검증."""

    def test_set_cell_id_returns_token(self):
        """set_cell_id는 복원용 Token을 반환해야 한다."""
        token = set_cell_id("cell-1")
        assert get_current_cell_id() == "cell-1"

        _current_cell_id.reset(token)
        assert get_current_cell_id() is None

    def test_set_and_get(self):
        """set_cell_id 후 get_current_cell_id로 조회 가능해야 한다."""
        set_cell_id("cell-5")
        assert get_current_cell_id() == "cell-5"

    def test_cell_scope_sets_and_restores(self):
        """cell_scope 내부에서 cell_id가 설정되고, 종료 시 복원되어야 한다."""
        assert get_current_cell_id() is None

        with cell_scope("cell-3") as cid:
            assert cid == "cell-3"
            assert get_current_cell_id() == "cell-3"

        assert get_current_cell_id() is None

    def test_cell_scope_restores_on_exception(self):
        """cell_scope 내부에서 예외 발생 시에도 복원되어야 한다."""
        set_cell_id("cell-original")

        with pytest.raises(ValueError, match="test error"):
            with cell_scope("cell-temp"):
                assert get_current_cell_id() == "cell-temp"
                raise ValueError("test error")

        assert get_current_cell_id() == "cell-original"

    def test_nested_cell_scope(self):
        """중첩된 cell_scope가 올바르게 복원되어야 한다."""
        with cell_scope("cell-outer"):
            assert get_current_cell_id() == "cell-outer"

            with cell_scope("cell-inner"):
                assert get_current_cell_id() == "cell-inner"

            assert get_current_cell_id() == "cell-outer"

        assert get_current_cell_id() is None
