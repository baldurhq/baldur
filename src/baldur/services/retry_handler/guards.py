"""
Retry Policy Guards — Kill Switch, Error Budget pre-checks.

Pre-check implementation that PolicyComposer calls before executing a Policy.
If Guard.check() returns allowed=False, Policy execution is blocked.
"""

from __future__ import annotations

import structlog

from baldur.interfaces.resilience_policy import GuardResult, PolicyContext

logger = structlog.get_logger()


class KillSwitchGuard:
    """
    Global Kill Switch pre-check.

    Calls SystemControlManager.is_enabled() to check whether the self-healing
    system is enabled. When disabled, blocks all Policy execution.
    """

    @property
    def name(self) -> str:
        return "kill_switch"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """Check whether the Kill Switch is active. context is unused."""
        try:
            from baldur.services.system_control import SystemControlManager

            manager = SystemControlManager()
            if manager.is_enabled():
                return GuardResult(allowed=True)
            return GuardResult(
                allowed=False,
                reason="Kill Switch is active: baldur system is disabled",
            )
        except Exception as e:
            # Fail-Open: pass when SystemControlManager fails to load
            logger.debug(
                "kill_switch_guard.systemcontrolmanager_available",
                error=e,
            )
            return GuardResult(allowed=True)


class ErrorBudgetGuard:
    """
    Error-budget gate pre-check.

    Calls check_automation_allowed() to check whether the error budget is at or
    above the threshold. If the error budget is insufficient, blocks Policy execution.
    """

    @property
    def name(self) -> str:
        return "error_budget"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """Check the remaining error-budget ratio. Extracts tier_id, region from context."""
        try:
            from baldur_pro.services.error_budget_gate import check_automation_allowed

            tier_id = context.tier_id if context else None
            region = context.region if context else None

            gate_result = check_automation_allowed(
                tier_id=tier_id,
                region=region,
            )

            if gate_result.allowed:
                return GuardResult(
                    allowed=True,
                    metadata={
                        "error_budget_percent": gate_result.error_budget_percent,
                    },
                )
            return GuardResult(
                allowed=False,
                reason=(
                    f"Error budget critically low "
                    f"({gate_result.error_budget_percent:.1f}%): "
                    f"retry blocked to prevent further errors"
                ),
                metadata={
                    "error_budget_percent": gate_result.error_budget_percent,
                    "threshold_percent": gate_result.threshold_percent,
                },
            )
        except ImportError:
            # Fail-Open when the ErrorBudgetGate module is absent
            return GuardResult(allowed=True)
        except Exception as e:
            # Fail-Open: pass when the gate check fails
            logger.warning(
                "error_budget_guard.gate_check_failed",
                error=e,
            )
            return GuardResult(allowed=True)
