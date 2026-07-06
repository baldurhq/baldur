"""
Framework-agnostic system control handlers.

Extracted from api/django/views/system_control.py. Provides the
Global Kill Switch + Dry Run API as pure handler functions.

Endpoints:
    GET  /system/status/              System status (read-only)
    POST /system/enable/              Re-enable baldur (admin)
    POST /system/disable/             Kill switch (admin)
    POST /system/dry-run/enable/      Dry run mode on (admin)
    POST /system/dry-run/disable/     Dry run mode off (admin)
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.services.system_control import get_system_control
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "system_status",
    "system_enable",
    "system_disable",
    "dry_run_enable",
    "dry_run_disable",
]


def system_status(ctx: RequestContext) -> ResponseContext:
    """GET /system/status/ — system status snapshot."""
    manager = get_system_control()
    state = manager.get_state()
    backend_info = manager.get_backend_info()

    return ResponseContext.json(
        {
            "system": "baldur",
            "status": "enabled" if state.enabled else "disabled",
            **state.to_dict(),
            "backend": backend_info,
            "timestamp": utc_now().isoformat(),
        }
    )


def system_enable(ctx: RequestContext) -> ResponseContext:
    """POST /system/enable/ — re-enable baldur (admin-only)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    state = get_system_control().enable(actor=resolve_actor(ctx), reason=reason)
    return ResponseContext.json(
        {
            "success": True,
            "message": "Baldur system enabled",
            "state": state.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def system_disable(ctx: RequestContext) -> ResponseContext:
    """POST /system/disable/ — kill switch (admin-only)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    if not reason:
        return ResponseContext.json(
            {
                "success": False,
                "error": "reason is required",
                "message": "Please provide a reason for disabling the system",
            },
            status_code=400,
        )
    state = get_system_control().disable(actor=resolve_actor(ctx), reason=reason)
    return ResponseContext.json(
        {
            "success": True,
            "message": "Baldur system DISABLED (Kill Switch activated)",
            "warning": "All baldur operations are now stopped",
            "state": state.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def dry_run_enable(ctx: RequestContext) -> ResponseContext:
    """POST /system/dry-run/enable/ — enable dry run mode (admin-only)."""
    state = get_system_control().enable_dry_run(actor=resolve_actor(ctx))
    return ResponseContext.json(
        {
            "success": True,
            "message": "Dry run mode ENABLED",
            "info": "Baldur will observe and log but not take actions",
            "state": state.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def dry_run_disable(ctx: RequestContext) -> ResponseContext:
    """POST /system/dry-run/disable/ — disable dry run mode (admin-only)."""
    body = ctx.json_body or {}
    if not body.get("confirm"):
        return ResponseContext.json(
            {
                "success": False,
                "error": "confirmation required",
                "message": "Set 'confirm': true to disable dry run mode and go LIVE",
            },
            status_code=400,
        )
    state = get_system_control().disable_dry_run(actor=resolve_actor(ctx))
    return ResponseContext.json(
        {
            "success": True,
            "message": "Dry run mode DISABLED - Baldur is now LIVE",
            "warning": "All baldur actions will now be executed for real",
            "state": state.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )
