"""
Baldur API Views Package.

This package provides REST API endpoints for the Baldur system.

Performance Optimization:
    All views are lazy-loaded to minimize Django app startup time.
    Views are loaded on-demand when first accessed.

Usage:
    # Recommended: Import from specific submodules for best performance
    from baldur.api.django.views.circuit_breaker import ControlActionView
    from baldur.api.django.views.health import BaldurHealthView

    # Legacy: Still works via lazy import (backward compatible)
    from baldur.api.django.views import ControlActionView

Modules:
- circuit_breaker: Control API views for Circuit Breaker management
- dlq: DLQ (Dead Letter Queue) management views
- health: Health check and metrics views
- system_control: Kill switch and system control views
- config: Runtime configuration views
- drift_threshold: Drift threshold configuration views
- error_budget: Error budget and deployment policy views
- config_history: Configuration history views
- chaos: Chaos Engineering views (already lazy)
- governance: Governance API views
- xtest_mode: X-Test-Mode views
- auto_tuning: Auto tuning views
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# =============================================================================
# LAZY IMPORTS - All views loaded on-demand for faster startup
# =============================================================================

# Mapping of symbol names to their module paths
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # -------------------------------------------------------------------------
    # circuit_breaker.py (11 symbols)
    # -------------------------------------------------------------------------
    "ControlRequest": (
        "baldur.api.django.views.circuit_breaker",
        "ControlRequest",
    ),
    "ControlResponse": (
        "baldur.api.django.views.circuit_breaker",
        "ControlResponse",
    ),
    "ControlAPIService": (
        "baldur.api.django.views.circuit_breaker",
        "ControlAPIService",
    ),
    "get_control_api_service": (
        "baldur.api.django.views.circuit_breaker",
        "get_control_api_service",
    ),
    "ControlActionView": (
        "baldur.api.django.views.circuit_breaker",
        "ControlActionView",
    ),
    "ControlStatusView": (
        "baldur.api.django.views.circuit_breaker",
        "ControlStatusView",
    ),
    "ServiceStatusView": (
        "baldur.api.django.views.circuit_breaker",
        "ServiceStatusView",
    ),
    "ControlAuditView": (
        "baldur.api.django.views.circuit_breaker",
        "ControlAuditView",
    ),
    "QuickAllowView": (
        "baldur.api.django.views.circuit_breaker",
        "QuickAllowView",
    ),
    "QuickBlockView": (
        "baldur.api.django.views.circuit_breaker",
        "QuickBlockView",
    ),
    "QuickResetView": (
        "baldur.api.django.views.circuit_breaker",
        "QuickResetView",
    ),
    # -------------------------------------------------------------------------
    # dlq.py (8 symbols)
    # -------------------------------------------------------------------------
    "DLQReplayView": ("baldur.api.django.views.dlq", "DLQReplayView"),
    "DLQCleanupStatsView": ("baldur.api.django.views.dlq", "DLQCleanupStatsView"),
    "DLQArchiveView": ("baldur.api.django.views.dlq", "DLQArchiveView"),
    "DLQPurgeView": ("baldur.api.django.views.dlq", "DLQPurgeView"),
    "DLQListView": ("baldur.api.django.views.dlq", "DLQListView"),
    "DLQDetailView": ("baldur.api.django.views.dlq", "DLQDetailView"),
    "DLQRetryView": ("baldur.api.django.views.dlq", "DLQRetryView"),
    "DLQResolveView": ("baldur.api.django.views.dlq", "DLQResolveView"),
    # -------------------------------------------------------------------------
    # dashboard.py (1 symbol)
    # -------------------------------------------------------------------------
    "DashboardSummaryView": (
        "baldur.api.django.views.dashboard",
        "DashboardSummaryView",
    ),
    # -------------------------------------------------------------------------
    # health.py (6 symbols)
    # -------------------------------------------------------------------------
    "BaldurHealthView": (
        "baldur.api.django.views.health",
        "BaldurHealthView",
    ),
    "LivenessView": ("baldur.api.django.views.health", "LivenessView"),
    "ReadinessView": ("baldur.api.django.views.health", "ReadinessView"),
    "ConnectionPoolHealthView": (
        "baldur.api.django.views.health",
        "ConnectionPoolHealthView",
    ),
    "simple_health_ping_view": (
        "baldur.api.django.views.health",
        "simple_health_ping_view",
    ),
    "BaldurMetricsView": (
        "baldur.api.django.views.health",
        "BaldurMetricsView",
    ),
    # -------------------------------------------------------------------------
    # system_control.py (9 symbols)
    # -------------------------------------------------------------------------
    "SystemStatusView": (
        "baldur.api.django.views.system_control",
        "SystemStatusView",
    ),
    "SystemEnableView": (
        "baldur.api.django.views.system_control",
        "SystemEnableView",
    ),
    "SystemDisableView": (
        "baldur.api.django.views.system_control",
        "SystemDisableView",
    ),
    "DryRunEnableView": (
        "baldur.api.django.views.system_control",
        "DryRunEnableView",
    ),
    "DryRunDisableView": (
        "baldur.api.django.views.system_control",
        "DryRunDisableView",
    ),
    "is_baldur_enabled": (
        "baldur.api.django.views.system_control",
        "is_baldur_enabled",
    ),
    "is_dry_run": ("baldur.api.django.views.system_control", "is_dry_run"),
    "get_system_control": (
        "baldur.api.django.views.system_control",
        "get_system_control",
    ),
    # -------------------------------------------------------------------------
    # config.py (14 symbols)
    # -------------------------------------------------------------------------
    "AllConfigView": ("baldur.api.django.views.config", "AllConfigView"),
    "ResetConfigView": ("baldur.api.django.views.config", "ResetConfigView"),
    "PendingChangesView": ("baldur.api.django.views.config", "PendingChangesView"),
    "CancelPendingChangeView": (
        "baldur.api.django.views.config",
        "CancelPendingChangeView",
    ),
    "CircuitBreakerConfigView": (
        "baldur.api.django.views.config",
        "CircuitBreakerConfigView",
    ),
    "DLQConfigView": ("baldur.api.django.views.config", "DLQConfigView"),
    "RetryConfigView": ("baldur.api.django.views.config", "RetryConfigView"),
    "SLAConfigView": ("baldur.api.django.views.config", "SLAConfigView"),
    "RateLimitConfigView": (
        "baldur.api.django.views.config",
        "RateLimitConfigView",
    ),
    "SecurityConfigView": ("baldur.api.django.views.config", "SecurityConfigView"),
    "IdempotencyConfigView": (
        "baldur.api.django.views.config",
        "IdempotencyConfigView",
    ),
    "NotificationConfigView": (
        "baldur.api.django.views.config",
        "NotificationConfigView",
    ),
    "ForensicConfigView": ("baldur.api.django.views.config", "ForensicConfigView"),
    "MetricsConfigView": ("baldur.api.django.views.config", "MetricsConfigView"),
    # -------------------------------------------------------------------------
    # drift_threshold.py (2 symbols)
    # -------------------------------------------------------------------------
    "DriftThresholdConfigView": (
        "baldur.api.django.views.drift_threshold",
        "DriftThresholdConfigView",
    ),
    "DriftThresholdResetView": (
        "baldur.api.django.views.drift_threshold",
        "DriftThresholdResetView",
    ),
    # -------------------------------------------------------------------------
    # error_budget.py (7 symbols)
    # -------------------------------------------------------------------------
    "ErrorBudgetStatusView": (
        "baldur.api.django.views.error_budget",
        "ErrorBudgetStatusView",
    ),
    "ErrorBudgetHistoryView": (
        "baldur.api.django.views.error_budget",
        "ErrorBudgetHistoryView",
    ),
    "DeploymentVerdictView": (
        "baldur.api.django.views.error_budget",
        "DeploymentVerdictView",
    ),
    "DeploymentFreezeAcknowledgeView": (
        "baldur.api.django.views.error_budget",
        "DeploymentFreezeAcknowledgeView",
    ),
    "DeploymentOverrideView": (
        "baldur.api.django.views.error_budget",
        "DeploymentOverrideView",
    ),
    "DeploymentFreezeLiftView": (
        "baldur.api.django.views.error_budget",
        "DeploymentFreezeLiftView",
    ),
    "ActiveOverrideView": (
        "baldur.api.django.views.error_budget",
        "ActiveOverrideView",
    ),
    # -------------------------------------------------------------------------
    # config_history.py (4 symbols)
    # -------------------------------------------------------------------------
    "ConfigHistoryView": (
        "baldur.api.django.views.config_history",
        "ConfigHistoryView",
    ),
    "ConfigVersionDetailView": (
        "baldur.api.django.views.config_history",
        "ConfigVersionDetailView",
    ),
    "ConfigRollbackView": (
        "baldur.api.django.views.config_history",
        "ConfigRollbackView",
    ),
    "ConfigCompareView": (
        "baldur.api.django.views.config_history",
        "ConfigCompareView",
    ),
    # -------------------------------------------------------------------------
    # chaos/ (16 symbols) - already lazy in chaos/__init__.py
    # -------------------------------------------------------------------------
    "SafetyGuardConfigView": (
        "baldur.api.django.views.chaos",
        "SafetyGuardConfigView",
    ),
    "ChaosBlastRadiusPolicyView": (
        "baldur.api.django.views.chaos",
        "ChaosBlastRadiusPolicyView",
    ),
    "SchedulerConfigView": (
        "baldur.api.django.views.chaos",
        "SchedulerConfigView",
    ),
    "ReportConfigView": ("baldur.api.django.views.chaos", "ReportConfigView"),
    "ScheduleListView": ("baldur.api.django.views.chaos", "ScheduleListView"),
    "ScheduleDetailView": ("baldur.api.django.views.chaos", "ScheduleDetailView"),
    "ScheduleApprovalView": (
        "baldur.api.django.views.chaos",
        "ScheduleApprovalView",
    ),
    "ScheduleExecuteView": (
        "baldur.api.django.views.chaos",
        "ScheduleExecuteView",
    ),
    "KillSwitchView": ("baldur.api.django.views.chaos", "KillSwitchView"),
    "SafetyCheckView": ("baldur.api.django.views.chaos", "SafetyCheckView"),
    "BlastRadiusCheckView": (
        "baldur.api.django.views.chaos",
        "BlastRadiusCheckView",
    ),
    "ReportListView": ("baldur.api.django.views.chaos", "ReportListView"),
    "ReportDetailView": ("baldur.api.django.views.chaos", "ReportDetailView"),
    "ReportGenerateView": ("baldur.api.django.views.chaos", "ReportGenerateView"),
    "GradeHistoryView": ("baldur.api.django.views.chaos", "GradeHistoryView"),
    "PendingApprovalsView": (
        "baldur.api.django.views.chaos",
        "PendingApprovalsView",
    ),
    # -------------------------------------------------------------------------
    # governance/ (4 symbols) — ApiService abstraction deferred to v1.1
    # -------------------------------------------------------------------------
    "MetricStatusView": ("baldur.api.django.views.governance", "MetricStatusView"),
    "GovernanceReconcileView": (
        "baldur.api.django.views.governance",
        "GovernanceReconcileView",
    ),
    "GovernanceModeView": (
        "baldur.api.django.views.governance",
        "GovernanceModeView",
    ),
    # -------------------------------------------------------------------------
    # xtest/ package (11 symbols) - uses the direct-import pattern
    # -------------------------------------------------------------------------
    "XTestModeMixin": ("baldur.api.django.views.xtest", "XTestModeMixin"),
    "InjectCBFailureView": (
        "baldur.api.django.views.xtest",
        "InjectCBFailureView",
    ),
    "ResetCBView": ("baldur.api.django.views.xtest", "ResetCBView"),
    "CBStatusDetailView": ("baldur.api.django.views.xtest", "CBStatusDetailView"),
    "InjectErrorBudgetView": (
        "baldur.api.django.views.xtest",
        "InjectErrorBudgetView",
    ),
    "SystemSnapshotView": ("baldur.api.django.views.xtest", "SystemSnapshotView"),
    "FastFailTestView": ("baldur.api.django.views.xtest", "FastFailTestView"),
    # DLQ X-Test Views
    "InjectDLQEntryView": ("baldur.api.django.views.xtest", "InjectDLQEntryView"),
    "DLQXTestStatusView": ("baldur.api.django.views.xtest", "DLQXTestStatusView"),
    "ForceStatusView": ("baldur.api.django.views.xtest", "ForceStatusView"),
    "ResetDLQXTestView": ("baldur.api.django.views.xtest", "ResetDLQXTestView"),
    # -------------------------------------------------------------------------
    # auto_tuning.py (9 symbols)
    # -------------------------------------------------------------------------
    "AutoTuningStatusView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningStatusView",
    ),
    "AutoTuningEnableView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningEnableView",
    ),
    "AutoTuningDisableView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningDisableView",
    ),
    "AutoTuningModuleEnableView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningModuleEnableView",
    ),
    "AutoTuningModuleDisableView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningModuleDisableView",
    ),
    "AutoTuningBoundsView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningBoundsView",
    ),
    "AutoTuningHistoryView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningHistoryView",
    ),
    "AutoTuningOverrideView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningOverrideView",
    ),
    "AutoTuningMetricsView": (
        "baldur.api.django.views.auto_tuning",
        "AutoTuningMetricsView",
    ),
    # -------------------------------------------------------------------------
    # grafana_webhook.py (2 symbols)
    # -------------------------------------------------------------------------
    "GrafanaAlertWebhookView": (
        "baldur.api.django.views.grafana_webhook",
        "GrafanaAlertWebhookView",
    ),
    "GrafanaAlertWebhookTestView": (
        "baldur.api.django.views.grafana_webhook",
        "GrafanaAlertWebhookTestView",
    ),
    # -------------------------------------------------------------------------
    # dlq_compressed.py (3 symbols)
    # -------------------------------------------------------------------------
    "DLQCompressedListView": (
        "baldur.api.django.views.dlq_compressed",
        "DLQCompressedListView",
    ),
    "DLQCompressedDetailView": (
        "baldur.api.django.views.dlq_compressed",
        "DLQCompressedDetailView",
    ),
    "DLQCompressedSummaryView": (
        "baldur.api.django.views.dlq_compressed",
        "DLQCompressedSummaryView",
    ),
    # -------------------------------------------------------------------------
    # bulkhead.py (2 symbols)
    # -------------------------------------------------------------------------
    "BulkheadStatusView": (
        "baldur.api.django.views.bulkhead",
        "BulkheadStatusView",
    ),
    "BulkheadDetailView": (
        "baldur.api.django.views.bulkhead",
        "BulkheadDetailView",
    ),
    # -------------------------------------------------------------------------
    # audit_resilience.py (9 symbols)
    # -------------------------------------------------------------------------
    "AuditHealthView": (
        "baldur.api.django.views.audit_resilience",
        "AuditHealthView",
    ),
    "CircuitBreakerStatusView": (
        "baldur.api.django.views.audit_resilience",
        "CircuitBreakerStatusView",
    ),
    "CircuitBreakerResetView": (
        "baldur.api.django.views.audit_resilience",
        "CircuitBreakerResetView",
    ),
    "CircuitBreakerForceOpenView": (
        "baldur.api.django.views.audit_resilience",
        "CircuitBreakerForceOpenView",
    ),
    "CircuitBreakerResetAllView": (
        "baldur.api.django.views.audit_resilience",
        "CircuitBreakerResetAllView",
    ),
    "AuditMetricsView": (
        "baldur.api.django.views.audit_resilience",
        "AuditMetricsView",
    ),
    "DegradedModeStatusView": (
        "baldur.api.django.views.audit_resilience",
        "DegradedModeStatusView",
    ),
    "DegradedModeForceView": (
        "baldur.api.django.views.audit_resilience",
        "DegradedModeForceView",
    ),
    "MetricsResetView": (
        "baldur.api.django.views.audit_resilience",
        "MetricsResetView",
    ),
    # -------------------------------------------------------------------------
    # continuous_audit.py (10 symbols)
    # -------------------------------------------------------------------------
    "ContinuousAuditQueryView": (
        "baldur.api.django.views.continuous_audit",
        "ContinuousAuditQueryView",
    ),
    "ContinuousAuditDetailView": (
        "baldur.api.django.views.continuous_audit",
        "ContinuousAuditDetailView",
    ),
    "ContinuousAuditAutoTuningView": (
        "baldur.api.django.views.continuous_audit",
        "ContinuousAuditAutoTuningView",
    ),
    "DriftHistoryView": (
        "baldur.api.django.views.continuous_audit",
        "DriftHistoryView",
    ),
    "ComplianceHistoryView": (
        "baldur.api.django.views.continuous_audit",
        "ComplianceHistoryView",
    ),
    "IntegrityVerifyView": (
        "baldur.api.django.views.continuous_audit",
        "IntegrityVerifyView",
    ),
    "ChainStateView": (
        "baldur.api.django.views.continuous_audit",
        "ChainStateView",
    ),
    "ExportJSONLView": (
        "baldur.api.django.views.continuous_audit",
        "ExportJSONLView",
    ),
    "ExportCSVView": (
        "baldur.api.django.views.continuous_audit",
        "ExportCSVView",
    ),
    "ConfigView": (
        "baldur.api.django.views.continuous_audit",
        "ConfigView",
    ),
    # -------------------------------------------------------------------------
    # compliance.py (7 symbols)
    # -------------------------------------------------------------------------
    "ComplianceStandardsView": (
        "baldur.api.django.views.compliance",
        "ComplianceStandardsView",
    ),
    "ComplianceChecksView": (
        "baldur.api.django.views.compliance",
        "ComplianceChecksView",
    ),
    "ComplianceRunView": (
        "baldur.api.django.views.compliance",
        "ComplianceRunView",
    ),
    "ComplianceReportsView": (
        "baldur.api.django.views.compliance",
        "ComplianceReportsView",
    ),
    "ComplianceReportDetailView": (
        "baldur.api.django.views.compliance",
        "ComplianceReportDetailView",
    ),
    "CompliancePendingEvidenceView": (
        "baldur.api.django.views.compliance",
        "CompliancePendingEvidenceView",
    ),
    "ComplianceEvidenceReviewView": (
        "baldur.api.django.views.compliance",
        "ComplianceEvidenceReviewView",
    ),
}

# Cache for lazily loaded symbols
_loaded_symbols: dict[str, object] = {}


def __getattr__(name: str) -> object:
    """Lazy import for backward compatibility.

    This allows:
        from baldur.api.django.views import ControlActionView

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

    raise AttributeError(f"module 'baldur.api.django.views' has no attribute '{name}'")


def __dir__() -> list[str]:
    """List available symbols for IDE autocompletion."""
    return list(__all__)


# TYPE_CHECKING block for IDE support without runtime import
if TYPE_CHECKING:
    # Audit Resilience Views (369)
    from baldur.api.django.views.audit_resilience import (
        AuditHealthView,
        AuditMetricsView,
        CircuitBreakerForceOpenView,
        CircuitBreakerResetAllView,
        CircuitBreakerResetView,
        CircuitBreakerStatusView,
        DegradedModeForceView,
        DegradedModeStatusView,
        MetricsResetView,
    )

    # Auto Tuning Views
    from baldur.api.django.views.auto_tuning import (
        AutoTuningBoundsView,
        AutoTuningDisableView,
        AutoTuningEnableView,
        AutoTuningHistoryView,
        AutoTuningMetricsView,
        AutoTuningModuleDisableView,
        AutoTuningModuleEnableView,
        AutoTuningOverrideView,
        AutoTuningStatusView,
    )

    # Chaos Engineering Views
    from baldur.api.django.views.chaos import (
        BlastRadiusCheckView,
        ChaosBlastRadiusPolicyView,
        GradeHistoryView,
        KillSwitchView,
        PendingApprovalsView,
        ReportConfigView,
        ReportDetailView,
        ReportGenerateView,
        ReportListView,
        SafetyCheckView,
        SafetyGuardConfigView,
        ScheduleApprovalView,
        ScheduleDetailView,
        ScheduleExecuteView,
        ScheduleListView,
        SchedulerConfigView,
    )
    from baldur.api.django.views.circuit_breaker import (
        ControlActionView,
        ControlAPIService,
        ControlAuditView,
        ControlRequest,
        ControlResponse,
        ControlStatusView,
        QuickAllowView,
        QuickBlockView,
        QuickResetView,
        ServiceStatusView,
        get_control_api_service,
    )

    # Runtime Config Views
    from baldur.api.django.views.config import (
        AllConfigView,
        CancelPendingChangeView,
        CircuitBreakerConfigView,
        DLQConfigView,
        ForensicConfigView,
        IdempotencyConfigView,
        MetricsConfigView,
        NotificationConfigView,
        PendingChangesView,
        RateLimitConfigView,
        ResetConfigView,
        RetryConfigView,
        SecurityConfigView,
        SLAConfigView,
    )

    # Config History Views
    from baldur.api.django.views.config_history import (
        ConfigCompareView,
        ConfigHistoryView,
        ConfigRollbackView,
        ConfigVersionDetailView,
    )

    # Continuous Audit Views (369)
    from baldur.api.django.views.continuous_audit import (
        ChainStateView,
        ComplianceHistoryView,
        ConfigView,
        ContinuousAuditAutoTuningView,
        ContinuousAuditDetailView,
        ContinuousAuditQueryView,
        DriftHistoryView,
        ExportCSVView,
        ExportJSONLView,
        IntegrityVerifyView,
    )

    # Dashboard Views
    from baldur.api.django.views.dashboard import (
        DashboardSummaryView,
    )

    # DLQ Views
    from baldur.api.django.views.dlq import (
        DLQArchiveView,
        DLQCleanupStatsView,
        DLQDetailView,
        DLQListView,
        DLQPurgeView,
        DLQReplayView,
        DLQResolveView,
        DLQRetryView,
    )

    # DLQ Compressed Views
    from baldur.api.django.views.dlq_compressed import (
        DLQCompressedDetailView,
        DLQCompressedListView,
        DLQCompressedSummaryView,
    )

    # Drift Threshold Configuration Views
    from baldur.api.django.views.drift_threshold import (
        DriftThresholdConfigView,
        DriftThresholdResetView,
    )

    # Error Budget & Deployment Policy Views
    from baldur.api.django.views.error_budget import (
        ActiveOverrideView,
        DeploymentFreezeAcknowledgeView,
        DeploymentFreezeLiftView,
        DeploymentOverrideView,
        DeploymentVerdictView,
        ErrorBudgetHistoryView,
        ErrorBudgetStatusView,
    )

    # Governance API Views (New Unified Hub)
    from baldur.api.django.views.governance import (
        GovernanceModeView,
        GovernanceReconcileView,
        MetricStatusView,
    )

    # Health & Metrics Views
    from baldur.api.django.views.health import (
        BaldurHealthView,
        BaldurMetricsView,
        ConnectionPoolHealthView,
        LivenessView,
        ReadinessView,
        simple_health_ping_view,
    )

    # System Control Views (Kill Switch)
    from baldur.api.django.views.system_control import (
        DryRunDisableView,
        DryRunEnableView,
        SystemDisableView,
        SystemEnableView,
        SystemStatusView,
        get_system_control,
        is_baldur_enabled,
        is_dry_run,
    )

    # X-Test-Mode Views (Stage 48: Chaos Proof)
    # Uses the direct-import pattern (instead of the xtest_mode.py re-export)
    from baldur.api.django.views.xtest import (  # DLQ X-Test Views
        CBStatusDetailView,
        DLQXTestStatusView,
        FastFailTestView,
        ForceStatusView,
        InjectCBFailureView,
        InjectDLQEntryView,
        InjectErrorBudgetView,
        ResetCBView,
        ResetDLQXTestView,
        SystemSnapshotView,
        XTestModeMixin,
    )

__all__ = [
    # Data classes
    "ControlRequest",
    "ControlResponse",
    # Service
    "ControlAPIService",
    "get_control_api_service",
    # Control Views
    "ControlActionView",
    "ControlStatusView",
    "ServiceStatusView",
    "ControlAuditView",
    # Quick Action Views
    "QuickAllowView",
    "QuickBlockView",
    "QuickResetView",
    # DLQ Views
    "DLQReplayView",
    "DLQCleanupStatsView",
    "DLQArchiveView",
    "DLQPurgeView",
    "DLQListView",
    "DLQDetailView",
    "DLQRetryView",
    "DLQResolveView",
    # DLQ Compressed Views (351)
    "DLQCompressedListView",
    "DLQCompressedDetailView",
    "DLQCompressedSummaryView",
    # Dashboard Views
    "DashboardSummaryView",
    # Health Views
    "BaldurHealthView",
    "LivenessView",
    "ReadinessView",
    "ConnectionPoolHealthView",
    "simple_health_ping_view",
    "BaldurMetricsView",
    # System Control (Kill Switch)
    "SystemStatusView",
    "SystemEnableView",
    "SystemDisableView",
    "DryRunEnableView",
    "DryRunDisableView",
    "is_baldur_enabled",
    "is_dry_run",
    "get_system_control",
    # Config Views
    "AllConfigView",
    "ResetConfigView",
    "PendingChangesView",
    "CancelPendingChangeView",
    "CircuitBreakerConfigView",
    "DLQConfigView",
    "RetryConfigView",
    "SLAConfigView",
    "RateLimitConfigView",
    "SecurityConfigView",
    "IdempotencyConfigView",
    "NotificationConfigView",
    "ForensicConfigView",
    "MetricsConfigView",
    # Drift Threshold Config Views
    "DriftThresholdConfigView",
    "DriftThresholdResetView",
    # Config History Views
    "ConfigHistoryView",
    "ConfigVersionDetailView",
    "ConfigRollbackView",
    "ConfigCompareView",
    # Error Budget & Deployment Policy Views
    "ErrorBudgetStatusView",
    "ErrorBudgetHistoryView",
    "DeploymentVerdictView",
    "DeploymentFreezeAcknowledgeView",
    "DeploymentOverrideView",
    "DeploymentFreezeLiftView",
    "ActiveOverrideView",
    # Chaos Engineering Views
    "SafetyGuardConfigView",
    "ChaosBlastRadiusPolicyView",
    "SchedulerConfigView",
    "ReportConfigView",
    "ScheduleListView",
    "ScheduleDetailView",
    "ScheduleApprovalView",
    "ScheduleExecuteView",
    "KillSwitchView",
    "SafetyCheckView",
    "BlastRadiusCheckView",
    "ReportListView",
    "ReportDetailView",
    "ReportGenerateView",
    "GradeHistoryView",
    "PendingApprovalsView",
    # Governance API (New Unified Hub)
    "MetricStatusView",
    "GovernanceReconcileView",
    "GovernanceModeView",
    # X-Test-Mode Views (Stage 48)
    "XTestModeMixin",
    "InjectCBFailureView",
    "ResetCBView",
    "CBStatusDetailView",
    "InjectErrorBudgetView",
    "SystemSnapshotView",
    "FastFailTestView",
    # DLQ X-Test Views
    "InjectDLQEntryView",
    "DLQXTestStatusView",
    "ForceStatusView",
    "ResetDLQXTestView",
    # Auto Tuning Views (Stage 38)
    "AutoTuningStatusView",
    "AutoTuningEnableView",
    "AutoTuningDisableView",
    "AutoTuningModuleEnableView",
    "AutoTuningModuleDisableView",
    "AutoTuningBoundsView",
    "AutoTuningHistoryView",
    "AutoTuningOverrideView",
    "AutoTuningMetricsView",
    # Grafana Webhook Views
    "GrafanaAlertWebhookView",
    "GrafanaAlertWebhookTestView",
    # Audit Resilience Views (369)
    "AuditHealthView",
    "CircuitBreakerStatusView",
    "CircuitBreakerResetView",
    "CircuitBreakerForceOpenView",
    "CircuitBreakerResetAllView",
    "AuditMetricsView",
    "DegradedModeStatusView",
    "DegradedModeForceView",
    "MetricsResetView",
    # Continuous Audit Views (369)
    "ContinuousAuditQueryView",
    "ContinuousAuditDetailView",
    "ContinuousAuditAutoTuningView",
    "DriftHistoryView",
    "ComplianceHistoryView",
    "IntegrityVerifyView",
    "ChainStateView",
    "ExportJSONLView",
    "ExportCSVView",
    "ConfigView",
    # Compliance Views (345)
    "ComplianceStandardsView",
    "ComplianceChecksView",
    "ComplianceRunView",
    "ComplianceReportsView",
    "ComplianceReportDetailView",
    "CompliancePendingEvidenceView",
    "ComplianceEvidenceReviewView",
]
