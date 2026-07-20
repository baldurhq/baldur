"""
Policy Guards — pre-check module.

Provides Guard implementations that verify global / per-tier conditions before
the PolicyComposer pipeline runs.

- KillSwitchGuard: system-wide enabled/disabled check
- ErrorBudgetGuard: remaining error budget check
- ThrottleGovernanceGuard: Kill Switch/Emergency/ErrorBudget/BreakGlass combined
- FullStopGuard: triple condition — Emergency LEVEL_3 + DB CB OPEN + budget
  exhausted
- LoadSheddingGuard: priority-based load shedding
- BackpressureGuard: RateController-based queue overload prevention
"""

from baldur.resilience.policies.guards.backpressure import BackpressureGuard
from baldur.resilience.policies.guards.error_budget import ErrorBudgetGuard
from baldur.resilience.policies.guards.full_stop import (
    FullStopGuard,
    create_default_full_stop_guard,
)
from baldur.resilience.policies.guards.governance import ThrottleGovernanceGuard
from baldur.resilience.policies.guards.kill_switch import KillSwitchGuard
from baldur.resilience.policies.guards.load_shedding import LoadSheddingGuard

__all__ = [
    "BackpressureGuard",
    "ErrorBudgetGuard",
    "FullStopGuard",
    "KillSwitchGuard",
    "LoadSheddingGuard",
    "ThrottleGovernanceGuard",
    "create_default_full_stop_guard",
]
