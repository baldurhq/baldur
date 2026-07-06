"""
KillSwitchGuard, ErrorBudgetGuard 단위 테스트.

테스트 대상: services/retry_handler/guards.py
- KillSwitchGuard: 글로벌 Kill Switch 사전 검증 (Fail-Open)
- ErrorBudgetGuard: 에러 예산 게이트 사전 검증 (Fail-Open)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.resilience_policy import PolicyContext
from baldur.services.retry_handler.guards import (
    ErrorBudgetGuard,
    KillSwitchGuard,
)

# =============================================================================
# KillSwitchGuard — 계약 검증
# =============================================================================


class TestKillSwitchGuardContract:
    """KillSwitchGuard 고정 식별자 검증."""

    def test_name_is_kill_switch(self):
        """KillSwitchGuard.name은 'kill_switch'이다."""
        assert KillSwitchGuard().name == "kill_switch"


# =============================================================================
# KillSwitchGuard — 동작 검증
# =============================================================================


class TestKillSwitchGuardBehavior:
    """KillSwitchGuard 동작 검증. Fail-Open 원칙 포함."""

    @patch("baldur.services.system_control.SystemControlManager")
    def test_enabled_allows_execution(self, mock_cls):
        """시스템이 활성화 상태면 allowed=True를 반환한다."""
        mock_cls.return_value.is_enabled.return_value = True
        result = KillSwitchGuard().check()
        assert result.allowed is True

    @patch("baldur.services.system_control.SystemControlManager")
    def test_disabled_blocks_execution(self, mock_cls):
        """시스템이 비활성화 상태면 allowed=False를 반환한다."""
        mock_cls.return_value.is_enabled.return_value = False
        result = KillSwitchGuard().check()
        assert result.allowed is False
        assert "Kill Switch" in result.reason

    def test_fail_open_on_import_error(self):
        """SystemControlManager import 실패 시 Fail-Open으로 통과한다."""
        with patch(
            "baldur.services.system_control.SystemControlManager",
            side_effect=ImportError("not found"),
        ):
            result = KillSwitchGuard().check()
            assert result.allowed is True

    def test_fail_open_on_runtime_error(self):
        """SystemControlManager 호출 중 에러 시 Fail-Open으로 통과한다."""
        with patch(
            "baldur.services.system_control.SystemControlManager",
            side_effect=RuntimeError("redis down"),
        ):
            result = KillSwitchGuard().check()
            assert result.allowed is True

    def test_check_ignores_context(self):
        """context가 전달되어도 무시한다."""
        ctx = PolicyContext(tier_id="critical")
        with patch(
            "baldur.services.system_control.SystemControlManager",
            side_effect=RuntimeError("redis down"),
        ):
            result = KillSwitchGuard().check(context=ctx)
            assert result.allowed is True


# =============================================================================
# ErrorBudgetGuard — 계약 검증
# =============================================================================


class TestErrorBudgetGuardContract:
    """ErrorBudgetGuard 고정 식별자 검증."""

    def test_name_is_error_budget(self):
        """ErrorBudgetGuard.name은 'error_budget'이다."""
        assert ErrorBudgetGuard().name == "error_budget"


# =============================================================================
# ErrorBudgetGuard — 동작 검증
# =============================================================================


class TestErrorBudgetGuardBehavior:
    """ErrorBudgetGuard 동작 검증. context 전달 및 Fail-Open 원칙 포함."""

    @pytest.fixture(autouse=True)
    def _require_pro(self):
        pytest.importorskip("baldur_pro")

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_allowed_when_budget_sufficient(self, mock_gate):
        """에러 예산이 충분하면 allowed=True를 반환한다."""
        gate_result = MagicMock(allowed=True, error_budget_percent=45.0)
        mock_gate.return_value = gate_result

        result = ErrorBudgetGuard().check()
        assert result.allowed is True
        assert result.metadata["error_budget_percent"] == 45.0

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_blocked_when_budget_low(self, mock_gate):
        """에러 예산이 부족하면 allowed=False를 반환한다."""
        gate_result = MagicMock(
            allowed=False, error_budget_percent=5.0, threshold_percent=10.0
        )
        mock_gate.return_value = gate_result

        result = ErrorBudgetGuard().check()
        assert result.allowed is False
        assert "budget" in result.reason.lower()
        assert result.metadata["error_budget_percent"] == 5.0
        assert result.metadata["threshold_percent"] == 10.0

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_passes_context_tier_id_and_region(self, mock_gate):
        """context.tier_id와 region을 check_automation_allowed에 전달한다."""
        mock_gate.return_value = MagicMock(allowed=True, error_budget_percent=50.0)
        ctx = PolicyContext(tier_id="critical", region="us-east")
        ErrorBudgetGuard().check(context=ctx)
        mock_gate.assert_called_once_with(tier_id="critical", region="us-east")

    @patch("baldur_pro.services.error_budget_gate.check_automation_allowed")
    def test_context_none_passes_none_values(self, mock_gate):
        """context=None이면 tier_id=None, region=None으로 전달된다."""
        mock_gate.return_value = MagicMock(allowed=True, error_budget_percent=50.0)
        ErrorBudgetGuard().check(context=None)
        mock_gate.assert_called_once_with(tier_id=None, region=None)

    def test_fail_open_on_import_error(self):
        """check_automation_allowed import 실패 시 Fail-Open으로 통과한다."""
        with patch(
            "baldur_pro.services.error_budget_gate.check_automation_allowed",
            side_effect=ImportError("not found"),
        ):
            result = ErrorBudgetGuard().check()
            assert result.allowed is True

    def test_fail_open_on_runtime_error(self):
        """check_automation_allowed 호출 중 에러 시 Fail-Open으로 통과한다."""
        with patch(
            "baldur_pro.services.error_budget_gate.check_automation_allowed",
            side_effect=RuntimeError("service unavailable"),
        ):
            result = ErrorBudgetGuard().check()
            assert result.allowed is True
