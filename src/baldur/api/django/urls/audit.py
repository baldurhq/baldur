"""Audit URL patterns (resilience CB ops + continuous audit logs/integrity/export)."""

from __future__ import annotations

from django.urls import path

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
from baldur.api.django.views.continuous_audit import (
    ChainStateView,
    ComplianceHistoryView,
    ContinuousAuditAutoTuningView,
    ContinuousAuditDetailView,
    ContinuousAuditQueryView,
    DriftHistoryView,
    ExportCSVView,
    ExportJSONLView,
    IntegrityVerifyView,
)
from baldur.api.django.views.continuous_audit import (
    ConfigView as AuditConfigView,
)

urlpatterns = [
    # Audit Resilience API (from audit/api.py)
    path("audit/resilience/health/", AuditHealthView.as_view(), name="audit-health"),
    path("audit/resilience/metrics/", AuditMetricsView.as_view(), name="audit-metrics"),
    path(
        "audit/resilience/metrics/reset/",
        MetricsResetView.as_view(),
        name="audit-metrics-reset",
    ),
    path(
        "audit/resilience/circuit-breakers/",
        CircuitBreakerStatusView.as_view(),
        name="audit-circuit-breakers-list",
    ),
    path(
        "audit/resilience/circuit-breakers/<str:name>/",
        CircuitBreakerStatusView.as_view(),
        name="audit-circuit-breaker-detail",
    ),
    path(
        "audit/resilience/circuit-breakers/<str:name>/reset/",
        CircuitBreakerResetView.as_view(),
        name="audit-circuit-breaker-reset",
    ),
    path(
        "audit/resilience/circuit-breakers/<str:name>/force-open/",
        CircuitBreakerForceOpenView.as_view(),
        name="audit-circuit-breaker-force-open",
    ),
    path(
        "audit/resilience/circuit-breakers/reset-all/",
        CircuitBreakerResetAllView.as_view(),
        name="audit-circuit-breakers-reset-all",
    ),
    path(
        "audit/resilience/degraded-mode/",
        DegradedModeStatusView.as_view(),
        name="audit-degraded-mode-status",
    ),
    path(
        "audit/resilience/degraded-mode/<str:action>/",
        DegradedModeForceView.as_view(),
        name="audit-degraded-mode-action",
    ),
    # Continuous Audit API (from audit/continuous_audit_api.py)
    path("audit/logs/", ContinuousAuditQueryView.as_view(), name="audit-logs"),
    path(
        "audit/logs/<str:log_id>/",
        ContinuousAuditDetailView.as_view(),
        name="audit-log-detail",
    ),
    path(
        "audit/auto-tuning/",
        ContinuousAuditAutoTuningView.as_view(),
        name="audit-auto-tuning",
    ),
    path("audit/drift/", DriftHistoryView.as_view(), name="audit-drift"),
    path(
        "audit/compliance/",
        ComplianceHistoryView.as_view(),
        name="audit-compliance",
    ),
    path(
        "audit/integrity/verify/",
        IntegrityVerifyView.as_view(),
        name="audit-integrity-verify",
    ),
    path(
        "audit/integrity/state/",
        ChainStateView.as_view(),
        name="audit-chain-state",
    ),
    path(
        "audit/export/jsonl/",
        ExportJSONLView.as_view(),
        name="audit-export-jsonl",
    ),
    path(
        "audit/export/csv/",
        ExportCSVView.as_view(),
        name="audit-export-csv",
    ),
    path("audit/config/", AuditConfigView.as_view(), name="audit-config"),
]
