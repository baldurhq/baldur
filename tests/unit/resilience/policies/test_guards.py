"""
KillSwitchGuard / ErrorBudgetGuard 단위 테스트 (#231).

테스트 대상:
- resilience/policies/guards/kill_switch.py (KillSwitchGuard)
- resilience/policies/guards/error_budget.py (ErrorBudgetGuard)
- resilience/policies/guards/__init__.py (re-export)

UNIT_TEST_GUIDELINES.md 준수:
- 계약 검증(Contract): 하드코딩 기대값 (name 문자열, reason 문자열)
- 동작 검증(Behavior): 소스 참조 (GuardResult 속성)
- conftest.py 배치: 1개 파일 전용 fixture → 파일 내부 (§5.1)
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from baldur.interfaces.resilience_policy import (
    PolicyContext,
)
from baldur.resilience.policies.guards import (
    ErrorBudgetGuard,
    KillSwitchGuard,
)

# =============================================================================
# 계약 검증 — KillSwitchGuard
# =============================================================================


class TestKillSwitchGuardContract:
    """KillSwitchGuard 계약 검증."""

    def test_name(self):
        """name은 'kill_switch'이다."""
        guard = KillSwitchGuard()
        assert guard.name == "kill_switch"


# =============================================================================
# 동작 검증 — KillSwitchGuard
# =============================================================================


class TestKillSwitchGuardBehavior:
    """KillSwitchGuard 동작 검증."""

    def test_import_error_fail_open(self):
        """SystemControlManager import 실패 시 Fail-Open (allowed=True)."""
        with patch(
            "baldur.resilience.policies.guards.kill_switch.KillSwitchGuard.check",
            wraps=KillSwitchGuard().check,
        ):
            # 직접 import를 모킹하여 ImportError 발생
            guard = KillSwitchGuard()
            with patch(
                "baldur.resilience.policies.guards.kill_switch.KillSwitchGuard.check"
            ):
                # ImportError 시 fail-open 되도록 원본 동작 검증
                pass

        # import 실패를 시뮬레이션
        guard = KillSwitchGuard()
        with patch.dict("sys.modules", {"baldur.services.system_control": None}):
            result = guard.check()
            assert result.allowed is True

    def test_system_enabled_allowed(self):
        """is_enabled()=True이면 allowed=True."""
        guard = KillSwitchGuard()
        mock_mgr = MagicMock()
        mock_mgr.is_enabled.return_value = True

        with patch(
            "baldur.resilience.policies.guards.kill_switch.get_system_control",
            create=True,
        ):
            # lazy import를 시뮬레이션
            mock_module = MagicMock()
            mock_module.get_system_control.return_value = mock_mgr

            with patch.dict(
                "sys.modules",
                {"baldur.services.system_control": mock_module},
            ):
                result = guard.check()

        assert result.allowed is True

    def test_system_disabled_rejected(self):
        """is_enabled()=False이면 allowed=False, reason 포함."""
        guard = KillSwitchGuard()
        mock_mgr = MagicMock()
        mock_mgr.is_enabled.return_value = False

        mock_module = MagicMock()
        mock_module.get_system_control.return_value = mock_mgr

        with patch.dict(
            "sys.modules",
            {"baldur.services.system_control": mock_module},
        ):
            result = guard.check()

        assert result.allowed is False
        assert result.reason == "System kill switch is disabled"

    def test_context_ignored(self):
        """context가 전달되어도 무시된다 (전역 상태만 체크)."""
        guard = KillSwitchGuard()
        ctx = PolicyContext(tier_id="critical", region="us-east-1")

        # import 실패 시뮬레이션으로 내부 로직이 context를 사용하지 않음을 확인
        with patch.dict("sys.modules", {"baldur.services.system_control": None}):
            result_no_ctx = guard.check()
            result_with_ctx = guard.check(context=ctx)

        assert result_no_ctx.allowed == result_with_ctx.allowed

    def test_exception_fail_open(self):
        """check 내부 일반 예외 발생 시 Fail-Open (allowed=True)."""
        guard = KillSwitchGuard()
        mock_module = MagicMock()
        mock_module.get_system_control.side_effect = RuntimeError("mgr error")

        with patch.dict(
            "sys.modules",
            {"baldur.services.system_control": mock_module},
        ):
            result = guard.check()

        assert result.allowed is True


# =============================================================================
# 계약 검증 — ErrorBudgetGuard
# =============================================================================


class TestErrorBudgetGuardContract:
    """ErrorBudgetGuard 계약 검증."""

    def test_name(self):
        """name은 'error_budget_gate'이다."""
        guard = ErrorBudgetGuard()
        assert guard.name == "error_budget_gate"


# =============================================================================
# 동작 검증 — ErrorBudgetGuard
# =============================================================================


class TestErrorBudgetGuardBehavior:
    """ErrorBudgetGuard 동작 검증."""

    def test_import_error_fail_open(self):
        """ErrorBudgetGate import 실패 시 Fail-Open (allowed=True)."""
        guard = ErrorBudgetGuard()
        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": None},
        ):
            result = guard.check()
            assert result.allowed is True

    def test_context_none_global_check(self):
        """context=None이면 tier_id=None, region=None으로 글로벌 판정."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = True
            reason: str | None = None
            error_budget_percent: float = 80.0
            threshold_percent: float = 10.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult(allowed=True)

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check(context=None)

        mock_module.check_automation_allowed.assert_called_once_with(
            tier_id=None, region=None
        )
        assert result.allowed is True

    def test_context_tier_and_region_passed(self):
        """context.tier_id/region이 check_automation_allowed에 전달된다."""
        guard = ErrorBudgetGuard()
        ctx = PolicyContext(tier_id="critical", region="us-west-2")

        @dataclass
        class MockGateResult:
            allowed: bool = True
            reason: str | None = None
            error_budget_percent: float = 80.0
            threshold_percent: float = 10.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult(allowed=True)

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check(context=ctx)

        mock_module.check_automation_allowed.assert_called_once_with(
            tier_id="critical", region="us-west-2"
        )
        assert result.allowed is True

    def test_gate_not_allowed_returns_rejected(self):
        """gate가 allowed=False를 반환하면 GuardResult도 allowed=False."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = False
            reason: str = "Budget exhausted"
            error_budget_percent: float = 2.0
            threshold_percent: float = 5.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult()

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is False
        assert result.reason == "Budget exhausted"
        assert result.metadata["error_budget_percent"] == 2.0
        assert result.metadata["threshold_percent"] == 5.0

    def test_gate_not_allowed_with_none_reason(self):
        """gate.reason이 None이면 기본 메시지 'Error budget exhausted' 사용."""
        guard = ErrorBudgetGuard()

        @dataclass
        class MockGateResult:
            allowed: bool = False
            reason: str | None = None
            error_budget_percent: float = 0.0
            threshold_percent: float = 5.0

        mock_module = MagicMock()
        mock_module.check_automation_allowed.return_value = MockGateResult()

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is False
        assert result.reason == "Error budget exhausted"

    def test_exception_fail_open(self):
        """check 내부 일반 예외 발생 시 Fail-Open (allowed=True)."""
        guard = ErrorBudgetGuard()
        mock_module = MagicMock()
        mock_module.check_automation_allowed.side_effect = RuntimeError("gate error")

        with patch.dict(
            "sys.modules",
            {"baldur_pro.services.error_budget_gate.gate": mock_module},
        ):
            result = guard.check()

        assert result.allowed is True


# =============================================================================
# 계약 검증 — guards __init__.py re-export
# =============================================================================


class TestGuardsInitReexportContract:
    """guards/__init__.py re-export 계약 검증."""

    def test_kill_switch_guard_exported(self):
        """KillSwitchGuard가 guards 패키지에서 import 가능하다."""
        from baldur.resilience.policies.guards import KillSwitchGuard

        assert KillSwitchGuard is not None

    def test_error_budget_guard_exported(self):
        """ErrorBudgetGuard가 guards 패키지에서 import 가능하다."""
        from baldur.resilience.policies.guards import ErrorBudgetGuard

        assert ErrorBudgetGuard is not None
