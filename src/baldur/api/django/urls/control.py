"""Manual control + status + quick action URL patterns."""

from __future__ import annotations

from django.urls import path

from baldur.api.django.pool_circuit_breaker import (
    circuit_breaker_reset,
    circuit_breaker_status,
)
from baldur.api.django.views import (
    ControlActionView,
    ControlAuditView,
    ControlStatusView,
    QuickAllowView,
    QuickBlockView,
    QuickResetView,
    ServiceStatusView,
)

urlpatterns = [
    # Control API
    path("control/", ControlActionView.as_view(), name="control-action"),
    # Status endpoints
    path("status/", ControlStatusView.as_view(), name="status"),
    path(
        "status/<str:service_name>/", ServiceStatusView.as_view(), name="service-status"
    ),
    # Audit logs (renamed from "audit/" to avoid collision with audit API namespace)
    path("control/audit-log/", ControlAuditView.as_view(), name="control-audit-log"),
    # Quick actions
    path("allow/<str:service_name>/", QuickAllowView.as_view(), name="quick-allow"),
    path("block/<str:service_name>/", QuickBlockView.as_view(), name="quick-block"),
    path("reset/<str:service_name>/", QuickResetView.as_view(), name="quick-reset"),
    # Pool Circuit Breaker API
    path("circuit-breaker/pool/status/", circuit_breaker_status, name="pool-cb-status"),
    path("circuit-breaker/pool/reset/", circuit_breaker_reset, name="pool-cb-reset"),
]
