"""
FullStopGuard — triple-condition Guard: Emergency LEVEL_3 + DB CB OPEN + error
budget exhausted.

Extracts the direct CircuitBreakerService / ErrorBudgetService / EmergencyMode
references hardcoded in AdaptiveThrottle.check_full_stop_conditions() into a
constructor-injected Guard.

All three conditions must hold to reject, so when only one holds another Guard
(ThrottleGovernanceGuard, etc.) can still block on its own.

Fail-open principle:
    A provider that fails to import or call is treated as an unmet condition
    (the call is allowed through). The create_default_full_stop_guard() factory
    wires the default providers automatically.

Usage::

    from baldur.resilience.policies.guards.full_stop import (
        create_default_full_stop_guard,
    )
    policy.add_guard(create_default_full_stop_guard())
"""

from __future__ import annotations

from collections.abc import Callable

import structlog

from baldur.interfaces.resilience_policy import (
    GuardResult,
    PolicyContext,
)

logger = structlog.get_logger()


class FullStopGuard:
    """
    Full Stop triple-condition Guard.

    Rejects only when all three conditions hold:
    1. Emergency LEVEL_3 or above
    2. A core DB Circuit Breaker is OPEN
    3. Error Budget fully exhausted (at or below 0%)

    Each condition is injected as a Callable provider;
    create_default_full_stop_guard() wires them automatically via lazy import.
    """

    def __init__(
        self,
        emergency_provider: Callable[[], int],
        cb_state_provider: Callable[[str], str],
        budget_provider: Callable[[], float],
    ) -> None:
        """
        Abstract external system dependencies as injected Callables.

        Args:
            emergency_provider: returns the Emergency Level (0=NORMAL,
                3=CRITICAL)
            cb_state_provider: service name → CB state ("open"/"closed")
            budget_provider: returns the remaining Error Budget percent
        """
        self._get_emergency_level = emergency_provider
        self._get_cb_state = cb_state_provider
        self._get_budget_remaining = budget_provider

    @property
    def name(self) -> str:
        """Guard identifier."""
        return "full_stop"

    def check(self, context: PolicyContext | None = None) -> GuardResult:
        """
        Check the Full Stop triple condition.

        Rejects only when all three conditions hold. When any single condition
        is unmet the call passes (another Guard owns that individual block).

        Returns:
            GuardResult(allowed=False) — all three conditions hold
            GuardResult(allowed=True) — otherwise
        """
        is_level_3 = self._get_emergency_level() >= 3
        db_cb_open = self._get_cb_state("database") == "open"
        budget_exhausted = self._get_budget_remaining() <= 0

        if is_level_3 and db_cb_open and budget_exhausted:
            return GuardResult(
                allowed=False,
                reason="full_stop:LEVEL_3+DB_CB_OPEN+BUDGET_EXHAUSTED",
                metadata={
                    "emergency_level": self._get_emergency_level(),
                    "db_cb_state": "open",
                    "budget_remaining": self._get_budget_remaining(),
                },
            )

        return GuardResult(allowed=True)


def create_default_full_stop_guard() -> FullStopGuard:  # noqa: C901
    """
    Build the default FullStopGuard.

    Lazily imports CircuitBreakerService, ErrorBudgetService, and EmergencyMode
    to wire the providers automatically. A provider that fails to import is
    fail-open (condition unmet = call allowed through).

    Returns:
        a FullStopGuard instance
    """

    def _get_emergency_level() -> int:
        """Emergency Level lookup (fail-open: 0=NORMAL)."""
        try:
            from baldur.factory.registry import ProviderRegistry

            manager = ProviderRegistry.emergency_manager.safe_get()
            if manager is None:
                return 0
            return manager.get_current_level().severity
        except Exception:
            return 0

    def _get_cb_state(service: str) -> str:
        """Core DB Circuit Breaker state lookup (fail-open: "closed")."""
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            db_services = [
                "database",
                "db",
                "postgres",
                "mysql",
                "redis",
                "mongodb",
            ]
            for db_name in db_services:
                try:
                    state = cb_service.get_state(db_name)
                    if state == "open":
                        return "open"
                except Exception:
                    pass
            return "closed"
        except ImportError:
            return "closed"
        except Exception:
            return "closed"

    def _get_budget_remaining() -> float:
        """Error budget remaining percent (fail-open: 100.0)."""
        try:
            from baldur.factory.registry import ProviderRegistry

            service = ProviderRegistry.error_budget_service.safe_get()
            if service is None:
                return 100.0
            return service.get_budget_status().budget_remaining_percent
        except Exception:
            return 100.0

    return FullStopGuard(
        emergency_provider=_get_emergency_level,
        cb_state_provider=_get_cb_state,
        budget_provider=_get_budget_remaining,
    )
