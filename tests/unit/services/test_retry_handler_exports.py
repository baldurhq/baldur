"""
retry_handler 패키지 Re-export 단위 테스트.

테스트 대상: services/retry_handler/__init__.py
- 패키지 레벨 re-export 검증 (RetryPolicy, Guards, Sinks)

Note: the ``@with_retry`` decorator was removed in 670 (superseded by the
unified ``@retry`` in resilience/policies/async_retry.py). Its behavior is now
covered by test_retry_decorator.py.
"""

from __future__ import annotations

import pytest

from baldur.services import retry_handler as pkg

# =============================================================================
# 패키지 Re-export — 계약 검증
# =============================================================================


class TestRetryHandlerPackageExportsContract:
    """retry_handler 패키지에서 새 클래스들이 정상 re-export되는지 검증."""

    @pytest.mark.parametrize(
        "name",
        [
            "RetryPolicy",
            "RetryPolicyConfig",
            "KillSwitchGuard",
            "ErrorBudgetGuard",
            "DLQSink",
            "detect_rate_limit",
        ],
    )
    def test_new_symbol_importable(self, name: str):
        """새로 추가된 심볼이 패키지에서 import 가능하다."""
        assert hasattr(pkg, name), f"{name} is not exported from retry_handler"

    def test_all_new_symbols_in_dunder_all(self):
        """__all__에 새 심볼 6개가 포함되어 있다."""
        expected = {
            "RetryPolicy",
            "RetryPolicyConfig",
            "KillSwitchGuard",
            "ErrorBudgetGuard",
            "DLQSink",
            "detect_rate_limit",
        }
        assert expected.issubset(set(pkg.__all__))

    def test_with_retry_removed(self):
        """``with_retry`` was removed in 670 (superseded by unified ``@retry``)."""
        assert not hasattr(pkg, "with_retry")
        assert "with_retry" not in pkg.__all__
