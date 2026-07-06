"""
Baldur Health & Metrics Views — thin HandlerAPIView wrappers.

Business logic lives in api/handlers/health.py, api/handlers/metrics.py,
and api/handlers/error_budget_gate.py.

Endpoints:
- GET  /api/baldur/health/              - Health check (V3: cached)
- GET  /api/baldur/health/live/         - Kubernetes liveness probe
- GET  /api/baldur/health/ready/        - Kubernetes readiness probe
- GET  /api/baldur/health/pool/         - Connection pool health
- GET  /api/baldur/health/ping/         - Simple ping (V3: ultra-lightweight)
- GET  /api/baldur/metrics/             - Get metrics (JSON)
- GET  /api/baldur/prometheus/          - Prometheus text exposition
- GET  /api/baldur/health/gate/         - Error Budget Gate health
- GET/PUT /api/baldur/config/gate/      - Error Budget Gate configuration
- POST /api/baldur/gate/reset/          - Error Budget Gate reset
"""

from __future__ import annotations

from typing import Any

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.error_budget_gate import (
    gate_config_get,
    gate_config_update,
    gate_health,
    gate_reset,
)
from baldur.api.handlers.health import (
    health_check,
    liveness_check,
    pool_health_check,
    readiness_check,
    simple_health_ping,
)
from baldur.api.handlers.metrics import (
    baldur_metrics,
    prometheus_text_metrics,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
    "BaldurHealthView",
    "LivenessView",
    "ReadinessView",
    "ConnectionPoolHealthView",
    "simple_health_ping_view",
    "BaldurMetricsView",
    "PrometheusTextMetricsView",
    "ErrorBudgetGateHealthView",
    "ErrorBudgetGateConfigView",
    "ErrorBudgetGateResetView",
]


class BaldurHealthView(HandlerAPIView):
    """GET /api/baldur/health/ — system health (V3 cached)."""

    permission_level = PermissionLevel.PUBLIC
    handler = health_check


class LivenessView(HandlerAPIView):
    """GET /api/baldur/health/live/ — liveness probe."""

    permission_level = PermissionLevel.PUBLIC
    handler = liveness_check


class ReadinessView(HandlerAPIView):
    """GET /api/baldur/health/ready/ — readiness probe."""

    permission_level = PermissionLevel.PUBLIC
    handler = readiness_check


class ConnectionPoolHealthView(HandlerAPIView):
    """GET /api/baldur/health/pool/ — connection pool health."""

    permission_level = PermissionLevel.PUBLIC
    handler = pool_health_check


class _SimpleHealthPingView(HandlerAPIView):
    """Thin wrapper — exposed as the ``simple_health_ping_view`` callable below."""

    permission_level = PermissionLevel.PUBLIC
    handler = simple_health_ping


# The as_view() callable used by URL patterns. Typed ``Any`` so the rebinding
# from the imported handler function is transparent across module boundaries.
simple_health_ping_view: Any = _SimpleHealthPingView.as_view()


class BaldurMetricsView(HandlerAPIView):
    """GET /api/baldur/metrics/ — control-API metrics (authenticated)."""

    permission_level = PermissionLevel.AUTHENTICATED
    handler = baldur_metrics


class PrometheusTextMetricsView(HandlerAPIView):
    """GET /api/baldur/prometheus/ — Prometheus text exposition."""

    permission_level = PermissionLevel.PUBLIC
    handler = prometheus_text_metrics


class ErrorBudgetGateHealthView(HandlerAPIView):
    """GET /api/baldur/health/gate/ — Error Budget Gate health."""

    permission_level = PermissionLevel.PUBLIC
    handler = gate_health


class ErrorBudgetGateConfigView(HandlerAPIView):
    """GET/PUT /api/baldur/config/gate/ — Error Budget Gate configuration."""

    handler_map = {
        HttpMethod.GET: gate_config_get,
        HttpMethod.PUT: gate_config_update,
    }
    permission_level = PermissionLevel.AUTHENTICATED


class ErrorBudgetGateResetView(HandlerAPIView):
    """POST /api/baldur/gate/reset/ — reset gate components."""

    permission_level = PermissionLevel.AUTHENTICATED
    handler = gate_reset
