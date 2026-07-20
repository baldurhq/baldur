"""
Error Budget Guard — admission check based on remaining error budget.

Wraps ErrorBudgetGate's check_automation_allowed() to test whether the error
budget is exhausted before the PolicyComposer pipeline runs.

Decision is context.tier_id/region based:
- context=None → global decision (tier_id=None → the global error budget)
- context.tier_id/region set → per-tier / per-region decision

Fail-open principle: if ErrorBudgetGate fails to import or call, the request
passes.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class ErrorBudgetGuard:
    """ErrorBudgetGate guard.

    Converts the GateCheckResult from check_automation_allowed() into a
    GuardResult.
    """

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "error_budget_gate"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Check the remaining error budget.

        Decision is context.tier_id/region based; context=None means a global
        decision (tier_id=None → the global error budget).

        Returns:
            GuardResult: allowed=True passes, False rejects
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
