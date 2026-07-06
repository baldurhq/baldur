"""Error Budget Service / Gate Interfaces (519 PR 2 / D-c1).

OSS-side Protocols for PRO error-budget singletons. PRO ships realized
implementations behind ``baldur_pro.services.error_budget`` /
``baldur_pro.services.error_budget_gate``; OSS callers resolve via
``ProviderRegistry.<slot>.safe_get()`` and use the returned instance with
a None-guard.

Methods are Interface Segregation — only those OSS code currently calls
are declared.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ErrorBudgetGate", "ErrorBudgetService"]


@runtime_checkable
class ErrorBudgetService(Protocol):
    """Protocol for the PRO error-budget service singleton."""

    def get_status(self, *args: Any, **kwargs: Any) -> Any: ...

    def get_budget_status(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class ErrorBudgetGate(Protocol):
    """Protocol for the PRO error-budget gate singleton."""

    def check(self, *args: Any, **kwargs: Any) -> Any: ...

    def is_replay_allowed(self, *args: Any, **kwargs: Any) -> bool: ...
