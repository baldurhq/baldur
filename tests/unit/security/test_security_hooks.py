"""
Security Hooks 단위 테스트.

세션 무효화 콜백 레지스트리(hooks.py)의 등록, 조회, 초기화 기능을 검증합니다.
"""

from __future__ import annotations

from baldur.services.security.hooks import (
    clear_session_invalidation_hooks,
    get_session_invalidation_hooks,
    register_session_invalidation_hook,
)


def _dummy_hook(user_id: int) -> str:
    return f"dummy({user_id})"


def _another_hook(user_id: int) -> str:
    return f"another({user_id})"


# =============================================================================
# Tests
# =============================================================================


class TestSecurityHooksBehavior:
    """세션 무효화 콜백 레지스트리 동작 검증."""

    def test_register_hook(self):
        """콜백 등록 후 목록에 포함되는지 확인."""
        register_session_invalidation_hook(_dummy_hook)

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 1
        assert hooks[0] is _dummy_hook

    def test_clear_hooks(self):
        """clear_session_invalidation_hooks() 후 빈 리스트 확인."""
        register_session_invalidation_hook(_dummy_hook)
        register_session_invalidation_hook(_another_hook)
        assert len(get_session_invalidation_hooks()) == 2

        clear_session_invalidation_hooks()
        assert get_session_invalidation_hooks() == []

    def test_multiple_hooks_order(self):
        """여러 콜백이 등록 순서대로 반환되는지 확인."""
        register_session_invalidation_hook(_dummy_hook)
        register_session_invalidation_hook(_another_hook)

        hooks = get_session_invalidation_hooks()
        assert len(hooks) == 2
        assert hooks[0] is _dummy_hook
        assert hooks[1] is _another_hook

    def test_get_hooks_returns_copy(self):
        """get_session_invalidation_hooks()가 내부 리스트의 복사본을 반환하는지 확인."""
        register_session_invalidation_hook(_dummy_hook)

        hooks = get_session_invalidation_hooks()
        hooks.clear()  # 외부에서 수정

        # 내부 리스트는 변경되지 않아야 함
        assert len(get_session_invalidation_hooks()) == 1

    def test_hook_callable(self):
        """등록된 콜백이 정상적으로 호출되는지 확인."""
        register_session_invalidation_hook(_dummy_hook)

        hooks = get_session_invalidation_hooks()
        result = hooks[0](42)
        assert result == "dummy(42)"

    def test_empty_hooks_by_default(self):
        """초기 상태에서 콜백이 비어있는지 확인."""
        assert get_session_invalidation_hooks() == []
