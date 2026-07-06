"""
Error Budget Guard — 에러 버짓 잔여량 기반 실행 허용 검증.

ErrorBudgetGate의 check_automation_allowed()를 래핑하여
PolicyComposer 파이프라인 실행 전 에러 버짓 소진 여부를 확인한다.

context.tier_id/region 기반 판정:
- context=None이면 글로벌 판정 (tier_id=None → 전역 에러 버짓)
- context.tier_id/region 지정 시 티어/리전별 판정

Fail-Open 원칙: ErrorBudgetGate import/호출 실패 시 통과 허용.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class ErrorBudgetGuard:
    """ErrorBudgetGate 가드.

    check_automation_allowed()의 GateCheckResult를
    GuardResult로 변환하여 반환한다.
    """

    @property
    def name(self) -> str:
        """Guard 식별자."""
        return "error_budget_gate"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        에러 버짓 잔여량 체크.

        context.tier_id/region 기반 판정.
        context=None이면 글로벌 판정 (tier_id=None → 전역 에러 버짓).

        Returns:
            GuardResult: allowed=True면 통과, False면 거부
        """
        try:
            from baldur_pro.services.error_budget_gate.gate import (
                check_automation_allowed,
            )

            tier_id = context.tier_id if context else None
            region = context.region if context else None

            gate_result = check_automation_allowed(
                tier_id=tier_id,
                region=region,
            )

            if not gate_result.allowed:
                return GuardResult(
                    allowed=False,
                    reason=gate_result.reason or "Error budget exhausted",
                    metadata={
                        "error_budget_percent": gate_result.error_budget_percent,
                        "threshold_percent": gate_result.threshold_percent,
                    },
                )
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="error_budget_gate",
                dependency="baldur_pro.services.error_budget_gate.gate",
            )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="error_budget_gate",
                check="automation_allowed",
                error=str(e),
                exc_info=True,
            )

        return GuardResult(allowed=True)
