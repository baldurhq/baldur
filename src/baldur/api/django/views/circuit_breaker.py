"""
Baldur Control API Views - Circuit Breaker Control (thin HandlerAPIView wrappers).

Business logic lives in api/handlers/circuit_breaker.py. The serializer-based
validation previously done inline is now embedded in the handler itself so
the same logic runs under Django, FastAPI, Flask, or the admin server.

Endpoints:
- POST /api/baldur/control/                      - Execute control action
- GET  /api/baldur/status/                       - Get all service states
- GET  /api/baldur/status/{service_name}/        - Get specific service state
- GET  /api/baldur/audit/                        - Get audit logs
- POST /api/baldur/allow/{service_name}/         - Quick allow
- POST /api/baldur/block/{service_name}/         - Quick block
- POST /api/baldur/reset/{service_name}/         - Quick reset
"""

from __future__ import annotations

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.circuit_breaker import (
    control_action,
    control_audit,
    control_status,
    quick_allow,
    quick_block,
    quick_reset,
    service_status,
)
from baldur.interfaces.web_framework import PermissionLevel
from baldur.services.control_api_service import (
    ControlAPIService,
    ControlRequest,
    ControlResponse,
    get_control_api_service,
)

__all__ = [
    # Service re-exports (backward compatibility)
    "ControlAPIService",
    "ControlRequest",
    "ControlResponse",
    "get_control_api_service",
    # Views
    "ControlActionView",
    "ControlStatusView",
    "ServiceStatusView",
    "ControlAuditView",
    "QuickAllowView",
    "QuickBlockView",
    "QuickResetView",
]


class ControlActionView(HandlerAPIView):
    """POST /api/baldur/control/ — execute control action (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = control_action


class ControlStatusView(HandlerAPIView):
    """GET /api/baldur/status/ — all service states (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = control_status


class ServiceStatusView(HandlerAPIView):
    """GET /api/baldur/status/{service_name}/ — single service (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = service_status


class ControlAuditView(HandlerAPIView):
    """GET /api/baldur/control/audit-log/ — audit logs (viewer+)."""

    permission_level = PermissionLevel.VIEWER
    handler = control_audit


class QuickAllowView(HandlerAPIView):
    """POST /api/baldur/allow/{service_name}/ — quick allow (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = quick_allow


class QuickBlockView(HandlerAPIView):
    """POST /api/baldur/block/{service_name}/ — quick block (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = quick_block


class QuickResetView(HandlerAPIView):
    """POST /api/baldur/reset/{service_name}/ — quick reset (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = quick_reset
