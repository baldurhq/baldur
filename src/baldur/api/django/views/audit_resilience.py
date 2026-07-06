"""
Audit Resilience API Endpoints.

Provides REST API for managing audit system resilience:
- Circuit breaker status and control
- Metrics endpoint (Prometheus format)
- Degraded mode status and control
- Health check endpoint

Relocated from audit/api.py (369 — Audit API Relocation).
Handlers extracted to api/handlers/audit.py and api/handlers/audit_resilience.py
(Phase 2b — 432).
"""

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.audit import audit_health, circuit_breaker_status
from baldur.api.handlers.audit_resilience import (
    audit_metrics,
    circuit_breaker_force_open,
    circuit_breaker_reset,
    circuit_breaker_reset_all,
    degraded_mode_force,
    degraded_mode_status,
    metrics_reset,
)
from baldur.interfaces.web_framework import PermissionLevel

__all__ = [
    "AuditHealthView",
    "CircuitBreakerStatusView",
    "CircuitBreakerResetView",
    "CircuitBreakerForceOpenView",
    "CircuitBreakerResetAllView",
    "AuditMetricsView",
    "DegradedModeStatusView",
    "DegradedModeForceView",
    "MetricsResetView",
]


class AuditHealthView(HandlerAPIView):
    """Health check endpoint for audit system."""

    permission_level = PermissionLevel.VIEWER
    handler = audit_health


class CircuitBreakerStatusView(HandlerAPIView):
    """Circuit breaker status view (list and detail)."""

    permission_level = PermissionLevel.VIEWER
    handler = circuit_breaker_status


class CircuitBreakerResetView(HandlerAPIView):
    """Reset a specific circuit breaker."""

    permission_level = PermissionLevel.ADMIN
    handler = circuit_breaker_reset


class CircuitBreakerForceOpenView(HandlerAPIView):
    """Force open a specific circuit breaker."""

    permission_level = PermissionLevel.ADMIN
    handler = circuit_breaker_force_open


class CircuitBreakerResetAllView(HandlerAPIView):
    """Reset all circuit breakers."""

    permission_level = PermissionLevel.ADMIN
    handler = circuit_breaker_reset_all


class AuditMetricsView(HandlerAPIView):
    """Prometheus-compatible metrics endpoint."""

    permission_level = PermissionLevel.VIEWER
    handler = audit_metrics


class DegradedModeStatusView(HandlerAPIView):
    """Degraded mode status view."""

    permission_level = PermissionLevel.VIEWER
    handler = degraded_mode_status


class DegradedModeForceView(HandlerAPIView):
    """Force degraded mode on/off."""

    permission_level = PermissionLevel.ADMIN
    handler = degraded_mode_force


class MetricsResetView(HandlerAPIView):
    """Reset metrics (for testing)."""

    permission_level = PermissionLevel.OPERATOR
    handler = metrics_reset
