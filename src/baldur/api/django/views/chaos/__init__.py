"""
Chaos Engineering API Views Package.

Full API control for the Chaos Engineering system.
All settings and operations are controllable via these endpoints.

Usage:
    # Recommended: Import from specific submodules
    from baldur.api.django.views.chaos.config_views import SafetyGuardConfigView
    from baldur.api.django.views.chaos.schedule_views import ScheduleListView
    from baldur.api.django.views.chaos.safety_views import KillSwitchView
    from baldur.api.django.views.chaos.report_views import ReportListView

    # Legacy: Still works via lazy import (backward compatible)
    from baldur.api.django.views.chaos import SafetyGuardConfigView

Submodules:
    - config_views: Configuration management (SafetyGuard, BlastRadius, Scheduler, Report)
    - schedule_views: Scheduled experiments CRUD and execution
    - safety_views: Kill switch, safety checks, TTL, dry-run
    - report_views: Report generation and history
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# =============================================================================
# LAZY IMPORTS - All views loaded on-demand
# =============================================================================

# Mapping of symbol names to their module paths
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # config_views.py
    "SafetyGuardConfigView": (
        "baldur.api.django.views.chaos.config_views",
        "SafetyGuardConfigView",
    ),
    "ChaosBlastRadiusPolicyView": (
        "baldur.api.django.views.chaos.config_views",
        "ChaosBlastRadiusPolicyView",
    ),
    "SchedulerConfigView": (
        "baldur.api.django.views.chaos.config_views",
        "SchedulerConfigView",
    ),
    "ReportConfigView": (
        "baldur.api.django.views.chaos.config_views",
        "ReportConfigView",
    ),
    # schedule_views.py
    "ScheduleListView": (
        "baldur.api.django.views.chaos.schedule_views",
        "ScheduleListView",
    ),
    "ScheduleDetailView": (
        "baldur.api.django.views.chaos.schedule_views",
        "ScheduleDetailView",
    ),
    "ScheduleApprovalView": (
        "baldur.api.django.views.chaos.schedule_views",
        "ScheduleApprovalView",
    ),
    "ScheduleExecuteView": (
        "baldur.api.django.views.chaos.schedule_views",
        "ScheduleExecuteView",
    ),
    "PendingApprovalsView": (
        "baldur.api.django.views.chaos.schedule_views",
        "PendingApprovalsView",
    ),
    # safety_views.py
    "KillSwitchView": (
        "baldur.api.django.views.chaos.safety_views",
        "KillSwitchView",
    ),
    "SafetyCheckView": (
        "baldur.api.django.views.chaos.safety_views",
        "SafetyCheckView",
    ),
    "BlastRadiusCheckView": (
        "baldur.api.django.views.chaos.safety_views",
        "BlastRadiusCheckView",
    ),
    "StopConditionsConfigView": (
        "baldur.api.django.views.chaos.safety_views",
        "StopConditionsConfigView",
    ),
    "TTLConfigView": (
        "baldur.api.django.views.chaos.safety_views",
        "TTLConfigView",
    ),
    "DryRunConfigView": (
        "baldur.api.django.views.chaos.safety_views",
        "DryRunConfigView",
    ),
    "KillAllView": ("baldur.api.django.views.chaos.safety_views", "KillAllView"),
    # report_views.py
    "ReportListView": (
        "baldur.api.django.views.chaos.report_views",
        "ReportListView",
    ),
    "ReportDetailView": (
        "baldur.api.django.views.chaos.report_views",
        "ReportDetailView",
    ),
    "ReportGenerateView": (
        "baldur.api.django.views.chaos.report_views",
        "ReportGenerateView",
    ),
    "GradeHistoryView": (
        "baldur.api.django.views.chaos.report_views",
        "GradeHistoryView",
    ),
    "DryRunAnalysisView": (
        "baldur.api.django.views.chaos.report_views",
        "DryRunAnalysisView",
    ),
}

# Cache for lazily loaded modules
_loaded_symbols: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazy import for backward compatibility.

    This allows:
        from baldur.api.django.views.chaos import SafetyGuardConfigView

    Without loading all view modules at package import time.
    """
    if name in _loaded_symbols:
        return _loaded_symbols[name]

    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_path)
        symbol = getattr(module, attr_name)
        _loaded_symbols[name] = symbol
        return symbol

    raise AttributeError(
        f"module 'baldur.api.django.views.chaos' has no attribute '{name}'"
    )


def __dir__() -> list[str]:
    """List available symbols for IDE autocompletion."""
    return list(__all__)


# TYPE_CHECKING block for IDE support without runtime import
if TYPE_CHECKING:
    from baldur.api.django.views.chaos.config_views import (
        ChaosBlastRadiusPolicyView,
        ReportConfigView,
        SafetyGuardConfigView,
        SchedulerConfigView,
    )
    from baldur.api.django.views.chaos.report_views import (
        DryRunAnalysisView,
        GradeHistoryView,
        ReportDetailView,
        ReportGenerateView,
        ReportListView,
    )
    from baldur.api.django.views.chaos.safety_views import (
        BlastRadiusCheckView,
        DryRunConfigView,
        KillAllView,
        KillSwitchView,
        SafetyCheckView,
        StopConditionsConfigView,
        TTLConfigView,
    )
    from baldur.api.django.views.chaos.schedule_views import (
        PendingApprovalsView,
        ScheduleApprovalView,
        ScheduleDetailView,
        ScheduleExecuteView,
        ScheduleListView,
    )


# =============================================================================
# __all__ - All views available (via lazy import)
# =============================================================================
__all__ = [
    # Config Views
    "SafetyGuardConfigView",
    "ChaosBlastRadiusPolicyView",
    "SchedulerConfigView",
    "ReportConfigView",
    # Schedule Views
    "ScheduleListView",
    "ScheduleDetailView",
    "ScheduleApprovalView",
    "ScheduleExecuteView",
    "PendingApprovalsView",
    # Safety Views
    "KillSwitchView",
    "SafetyCheckView",
    "BlastRadiusCheckView",
    "StopConditionsConfigView",
    "TTLConfigView",
    "DryRunConfigView",
    "KillAllView",
    # Report Views
    "ReportListView",
    "ReportDetailView",
    "ReportGenerateView",
    "GradeHistoryView",
    "DryRunAnalysisView",
]
