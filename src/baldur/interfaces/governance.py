"""
Governance Checker Interface.

OSS-side Protocol for governance gates (Kill Switch / Emergency Level /
Error Budget). PRO ships the realized ``check_all_governance()``
implementation behind ``baldur_pro.services.governance``; OSS callers
go through ``ProviderRegistry.governance.get()`` and invoke methods on
the resolved instance.

The :class:`NoOpGovernanceChecker` is registered as the OSS default so
the system stays fail-open when PRO is absent — every check returns
"allowed" without touching PRO infrastructure. This also lets OSS code
paths (previously gated by ``try: from baldur_pro... except: X = None``
shims) drop their ``is None`` branches: the default always answers.

Result types are reused from :mod:`baldur.models.governance` to avoid
duplicating the data shape across OSS and PRO.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from baldur.models.governance import GovernanceCheckResult

__all__ = [
    "GovernanceChecker",
    "NoOpGovernanceChecker",
]


@runtime_checkable
class GovernanceChecker(Protocol):
    """Protocol for governance gate evaluation.

    Methods mirror ``baldur_pro.services.governance.checks`` exports
    that OSS code paths consume. PRO concrete adapter delegates to the
    real pipeline; the OSS NoOp default returns fail-open answers.
    """

    def check_all_governance(
        self,
        check_kill_switch: bool = True,
        check_emergency: bool = True,
        emergency_min_level: int | None = None,
        check_error_budget: bool = True,
        operation_name: str = "unknown_operation",
        service_name: str | None = None,
        domain: str | None = None,
        audit_on_block: bool = True,
        tier_id: str | None = None,
        region: str | None = None,
        resource_context: dict[str, Any] | None = None,
    ) -> GovernanceCheckResult:
        """Run all enabled governance gates; return aggregated result."""
        ...

    def is_system_enabled(self) -> bool:
        """Return False when Kill Switch is active, True otherwise."""
        ...

    def is_emergency_blocking(self, min_level: int | None = None) -> tuple[bool, str]:
        """Return (is_blocked, level_name) for the current emergency level."""
        ...

    def is_error_budget_blocking(
        self,
        tier_id: str | None = None,
        region: str | None = None,
    ) -> tuple[bool, float, float]:
        """Return (is_blocked, current_pct, threshold_pct) for error budget."""
        ...

    def invalidate_governance_cache(self) -> None:
        """Invalidate the cached governance evaluation results.

        Called by event-bus handlers when an underlying state transition
        (Kill Switch toggled, Emergency Level changed, Error Budget threshold
        crossed) must propagate immediately instead of waiting for TTL.
        """
        ...

    def reset_governance_pipeline_cache(self) -> None:
        """Clear and rebuild the cached governance pipeline profiles.

        Test-fixture utility — invoked by ``reset_protect_caches()``
        so settings-mutating tests observe a fresh pipeline cache.
        """
        ...


class NoOpGovernanceChecker:
    """Default OSS governance checker — fail-open for every check.

    Registered as ``ProviderRegistry.governance`` default so OSS callers
    can invoke governance methods unconditionally. Returns "allowed"
    everywhere — the right behavior when PRO is absent (no governance
    pipeline to enforce).
    """

    def check_all_governance(
        self,
        check_kill_switch: bool = True,
        check_emergency: bool = True,
        emergency_min_level: int | None = None,
        check_error_budget: bool = True,
        operation_name: str = "unknown_operation",
        service_name: str | None = None,
        domain: str | None = None,
        audit_on_block: bool = True,
        tier_id: str | None = None,
        region: str | None = None,
        resource_context: dict[str, Any] | None = None,
    ) -> GovernanceCheckResult:
        return GovernanceCheckResult.allowed_result()

    def is_system_enabled(self) -> bool:
        return True

    def is_emergency_blocking(self, min_level: int | None = None) -> tuple[bool, str]:
        return False, "UNKNOWN"

    def is_error_budget_blocking(
        self,
        tier_id: str | None = None,
        region: str | None = None,
    ) -> tuple[bool, float, float]:
        return False, 100.0, 0.0

    def invalidate_governance_cache(self) -> None:
        return None

    def reset_governance_pipeline_cache(self) -> None:
        return None
