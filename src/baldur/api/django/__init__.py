"""
Django REST Framework API for the baldur system.

This module provides DRF views, serializers, and URL configurations
for the baldur control API.

Usage:
    # In your Django project's urls.py:
    from baldur.api.django import urls as baldur_urls

    urlpatterns = [
        path('api/baldur/', include(baldur_urls)),
    ]

    # In your Django project's settings.py:
    MIDDLEWARE = [
        "baldur.audit.trace.trace_id_middleware",                  # [1] Trace ID
        "baldur.api.django.middleware.HealthBridgeMiddleware",     # [2] Health Bridge
        "baldur.api.django.tiering.TieringMiddleware",             # [3] Tiering
        "baldur.api.django.middleware.BaldurMiddleware",      # [4] Baldur
        # ... Django Core Middlewares ...
        "baldur.api.django.pool_circuit_breaker.PoolCircuitBreakerMiddleware",  # [8] Pool CB
        # ... other middlewares ...
        "baldur.api.django.audit_middleware.AuditMiddleware",      # [11] Audit (맨 마지막!)
    ]
"""

from baldur.api.django.audit_middleware import (
    AuditMiddleware,
    is_audit_middleware_enabled,
)
from baldur.api.django.middleware import (
    BaldurMiddleware,
    HealthBridgeMiddleware,
)
from baldur.api.django.pool_circuit_breaker import (
    PoolCircuitBreaker,
    PoolCircuitBreakerMiddleware,
    circuit_breaker_reset,
    circuit_breaker_status,
    pool_circuit_breaker,
)
from baldur.api.django.serializers import (
    AuditLogListResponseSerializer,
    ControlAPIActions,
    ControlAPIEnvironments,
    ControlErrorResponseSerializer,
    ControlRequestSerializer,
    ControlResponseSerializer,
    ControlStatusResponseSerializer,
    MetricsResponseSerializer,
    ServiceStateSerializer,
)
from baldur.api.django.tiering import TieringMiddleware
from baldur.api.django.views import (
    BaldurHealthView,
    BaldurMetricsView,
    ControlActionView,
    ControlAPIService,
    ControlAuditView,
    ControlStatusView,
    DLQReplayView,
    QuickAllowView,
    QuickBlockView,
    QuickResetView,
    ServiceStatusView,
    get_control_api_service,
)
from baldur.scaling.tiering import (
    TierRegistry,
    get_tier_registry,
)

__all__ = [
    # Views
    "ControlActionView",
    "ControlStatusView",
    "ServiceStatusView",
    "ControlAuditView",
    "QuickAllowView",
    "QuickBlockView",
    "QuickResetView",
    "BaldurHealthView",
    "BaldurMetricsView",
    "DLQReplayView",
    # Service
    "get_control_api_service",
    "ControlAPIService",
    # Serializers
    "ControlRequestSerializer",
    "ControlResponseSerializer",
    "ControlErrorResponseSerializer",
    "ControlStatusResponseSerializer",
    "ServiceStateSerializer",
    "AuditLogListResponseSerializer",
    "MetricsResponseSerializer",
    # Constants
    "ControlAPIActions",
    "ControlAPIEnvironments",
    # Middleware - Gateway Pipeline
    "HealthBridgeMiddleware",
    "BaldurMiddleware",
    "AuditMiddleware",
    "is_audit_middleware_enabled",
    "PoolCircuitBreakerMiddleware",
    "PoolCircuitBreaker",
    "pool_circuit_breaker",
    "circuit_breaker_status",
    "circuit_breaker_reset",
    "TieringMiddleware",
    "TierRegistry",
    "get_tier_registry",
]
