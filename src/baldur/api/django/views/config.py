"""
Runtime Configuration API Views — thin HandlerAPIView wrappers.

Business logic lives in api/handlers/config.py. Per-config-type endpoints
use ``functools.partial`` to bind ``config_name`` into the generic
``config_get`` / ``config_update`` handlers.

Endpoints:
- GET  /api/baldur/config/                    - Get all config
- POST /api/baldur/config/reset/              - Reset all to defaults
- GET  /api/baldur/config/pending/            - Get pending changes
- POST /api/baldur/config/pending/<id>/cancel - Cancel pending change
- GET/PUT /api/baldur/config/<type>/          - Per-config-type get/update
- PUT /api/baldur/config/slo/ (+DELETE)       - SLO special-case
"""

from __future__ import annotations

from functools import partial

from baldur.api.django.base import HandlerAPIView
from baldur.api.handlers.config import (
    all_config_get,
    cancel_pending_change,
    config_get,
    config_reset,
    config_update,
    logging_config_update,
    pending_changes_get,
    slo_config_delete,
    slo_config_update,
)
from baldur.interfaces.web_framework import HttpMethod, PermissionLevel

__all__ = [
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
    "LoggingConfigView",
    "MetricsConfigView",
    "ErrorBudgetConfigView",
    "SLOConfigView",
    "ReplayAutomationConfigView",
]


def _bind_config_handlers(config_name: str):
    """Partial-bind ``config_name`` into the generic config handlers."""
    return (
        partial(config_get, config_name=config_name),
        partial(config_update, config_name=config_name),
    )


class AllConfigView(HandlerAPIView):
    """GET /api/baldur/config/ — all configuration with strategies."""

    permission_level = PermissionLevel.VIEWER
    handler = all_config_get


class ResetConfigView(HandlerAPIView):
    """POST /api/baldur/config/reset/ — reset all to defaults (admin)."""

    permission_level = PermissionLevel.ADMIN
    handler = config_reset


class PendingChangesView(HandlerAPIView):
    """GET /api/baldur/config/pending/ — pending changes."""

    permission_level = PermissionLevel.VIEWER
    handler = pending_changes_get


class CancelPendingChangeView(HandlerAPIView):
    """POST /api/baldur/config/pending/<pending_id>/cancel/ — cancel pending."""

    permission_level = PermissionLevel.ADMIN
    handler = cancel_pending_change


class _ConfigSectionView(HandlerAPIView):
    """Base for per-config GET/PUT pairs.

    Subclasses bind the generic handler pair via ``_bind_config_handlers``.
    GET = viewer+, PUT = admin (matches the original ``BaseConfigView``
    permission policy).
    """

    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
    }
    permission_level = PermissionLevel.ADMIN


def _make_section_view(name: str, config_name: str) -> type[_ConfigSectionView]:
    get_fn, put_fn = _bind_config_handlers(config_name)
    return type(
        name,
        (_ConfigSectionView,),
        {
            "__doc__": f"GET/PUT /api/baldur/config/{config_name}/",
            "handler_map": {HttpMethod.GET: get_fn, HttpMethod.PUT: put_fn},
        },
    )


CircuitBreakerConfigView = _make_section_view(
    "CircuitBreakerConfigView", "circuit_breaker"
)
DLQConfigView = _make_section_view("DLQConfigView", "dlq")
RetryConfigView = _make_section_view("RetryConfigView", "retry")
SLAConfigView = _make_section_view("SLAConfigView", "sla")
RateLimitConfigView = _make_section_view("RateLimitConfigView", "rate_limit")
SecurityConfigView = _make_section_view("SecurityConfigView", "security")
IdempotencyConfigView = _make_section_view("IdempotencyConfigView", "idempotency")
NotificationConfigView = _make_section_view("NotificationConfigView", "notification")
ForensicConfigView = _make_section_view("ForensicConfigView", "forensic")
MetricsConfigView = _make_section_view("MetricsConfigView", "metrics")
ErrorBudgetConfigView = _make_section_view("ErrorBudgetConfigView", "error_budget")
ReplayAutomationConfigView = _make_section_view(
    "ReplayAutomationConfigView", "replay_automation"
)


class LoggingConfigView(_ConfigSectionView):
    """GET/PUT /api/baldur/config/logging/ — PUT also applies runtime levels."""

    handler_map = {
        HttpMethod.GET: partial(config_get, config_name="logging"),
        HttpMethod.PUT: logging_config_update,
    }


class SLOConfigView(HandlerAPIView):
    """GET/PUT/DELETE /api/baldur/config/slo/ — SLO definitions."""

    handler_map = {
        HttpMethod.GET: partial(config_get, config_name="slo"),
        HttpMethod.PUT: slo_config_update,
        HttpMethod.DELETE: slo_config_delete,
    }
    permission_map = {
        HttpMethod.GET: PermissionLevel.VIEWER,
        HttpMethod.PUT: PermissionLevel.ADMIN,
        HttpMethod.DELETE: PermissionLevel.ADMIN,
    }
    permission_level = PermissionLevel.ADMIN
