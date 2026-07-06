"""
ThrottleGovernanceGuard — Kill Switch + Emergency + ErrorBudget + BreakGlass guard.

Replaces the four hardcoded governance dependencies that lived on
AdaptiveThrottle via the GovernanceCheckMixin inheritance with a single
PolicyComposer-compatible Guard.

Resolution strategy:
    GovernanceChecker is resolved via ProviderRegistry.governance (516 D2/D3) —
    cached lazily on first ``check()`` to avoid both per-request import
    overhead (#510) and ordering issues with PRO entitlement init.

PolicyComposer registers this Guard via ``add_guard()`` to short-circuit
ThrottlePolicy execution when governance state forbids it.

Fail-open principle:
    The NoOp default GovernanceChecker registered by OSS returns "allowed"
    for every check; if PRO is absent, this Guard always passes.
    Hot-path exceptions are also caught and logged WARNING per the
    KillSwitchGuard / ErrorBudgetGuard precedent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

if TYPE_CHECKING:
    from baldur.interfaces.governance import GovernanceChecker

logger = structlog.get_logger()


class ThrottleGovernanceGuard:
    """Kill Switch + Emergency + ErrorBudget + BreakGlass combined Guard.

    Order:
    1. Break Glass — if active, all other checks bypassed (emergency override).
    2. Kill Switch (is_system_enabled) → reject if disabled.
    3. Emergency Level → reject at LEVEL_3+.
    4. Error Budget → reject if exhausted.
    """

    def __init__(self) -> None:
        self._governance: GovernanceChecker | None = None
        self._governance_resolved: bool = False

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "throttle_governance"

    def _get_governance(self) -> GovernanceChecker | None:
        """Lazily resolve and cache the GovernanceChecker provider.

        Lazy (not eager in __init__) because guards may be constructed
        before ``baldur.init()`` registers the PRO provider in test
        fixtures, REPL sessions, and Django auto-discovery. The OSS NoOp
        default makes the pre-init window fail-open by construction.
        """
        if self._governance_resolved:
            return self._governance
        try:
            from baldur.factory.registry import ProviderRegistry

            self._governance = ProviderRegistry.governance.get()
        except Exception as e:
            logger.warning(
                "guard.governance_resolve_failed_fail_open",
                guard_name="throttle_governance",
                error=str(e),
            )
            self._governance = None
        self._governance_resolved = True
        return self._governance

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """Run governance pre-checks.

        Break Glass → Kill Switch → Emergency → Error Budget, with each
        delegating to the registered GovernanceChecker.
        """
        if self._is_break_glass_active():
            logger.debug(
                "guard.break_glass_active",
                guard_name="throttle_governance",
            )
            return GuardResult(allowed=True)

        governance = self._get_governance()
        if governance is None:
            return GuardResult(allowed=True)

        kill_switch_result = self._check_kill_switch(governance)
        if not kill_switch_result.allowed:
            return kill_switch_result

        emergency_result = self._check_emergency_level(governance)
        if not emergency_result.allowed:
            return emergency_result

        budget_result = self._check_error_budget(governance, context)
        if not budget_result.allowed:
            return budget_result

        return GuardResult(allowed=True)

    def _check_kill_switch(self, governance: GovernanceChecker) -> GuardResult:
        """Kill Switch global state check (Fail-Open)."""
        try:
            if not governance.is_system_enabled():
                return GuardResult(
                    allowed=False,
                    reason="kill_switch_disabled",
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="throttle_governance",
                check="kill_switch",
                error=str(e),
                exc_info=True,
            )
        return GuardResult(allowed=True)

    def _check_emergency_level(self, governance: GovernanceChecker) -> GuardResult:
        """Emergency Level check — reject at LEVEL_3+ (Fail-Open)."""
        try:
            is_blocked, level_name = governance.is_emergency_blocking(min_level=3)
            if is_blocked:
                return GuardResult(
                    allowed=False,
                    reason=f"emergency_level_{level_name}",
                    metadata={"emergency_level": level_name},
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="throttle_governance",
                check="emergency_level",
                error=str(e),
                exc_info=True,
            )
        return GuardResult(allowed=True)

    def _check_error_budget(
        self,
        governance: GovernanceChecker,
        context: PolicyContext | None,
    ) -> GuardResult:
        """Error Budget remaining check (Fail-Open)."""
        try:
            tier_id = context.tier_id if context else None
            region = context.region if context else None

            is_blocked, current_pct, _threshold_pct = (
                governance.is_error_budget_blocking(
                    tier_id=tier_id,
                    region=region,
                )
            )

            if is_blocked:
                return GuardResult(
                    allowed=False,
                    reason="error_budget_exhausted",
                    metadata={"error_budget_percent": current_pct},
                )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="throttle_governance",
                check="error_budget",
                error=str(e),
                exc_info=True,
            )
        return GuardResult(allowed=True)

    def _is_break_glass_active(self) -> bool:
        """Break Glass active flag (Fail-Open → False).

        Reads OSS settings directly; per 516 D3 this is OSS-internal and
        not a PRO-boundary concern, so the per-request try-import is
        retained — a settings-caching optimization is out of scope here.
        """
        try:
            from baldur.settings.governance import get_governance_settings

            return get_governance_settings().break_glass_enabled
        except ImportError:
            logger.debug(
                "guard.dependency_missing",
                guard_name="throttle_governance",
                check="break_glass",
                dependency="baldur.settings.governance",
            )
        except Exception as e:
            logger.warning(
                "guard.check_failed_fail_open",
                guard_name="throttle_governance",
                check="break_glass",
                error=str(e),
                exc_info=True,
            )
        return False
