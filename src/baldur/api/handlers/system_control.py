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

Every change response carries ``persisted`` (``true`` in the store, ``false``
not applied there, ``null`` outcome unknown) and ``applies`` (``everywhere``,
``this_process`` or ``none``). A change the store did not confirm answers 503.
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.core.exceptions import SystemControlStoreError
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.services.system_control import (
    SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS,
    SystemControlChange,
    get_system_control,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "system_status",
    "system_enable",
    "system_disable",
    "dry_run_enable",
    "dry_run_disable",
]

_DISABLED_EFFECT = (
    "Baldur's automatic interventions step aside: each protected call runs "
    "once, with no retry, no circuit-breaker recording or refusal, no DLQ "
    "capture and no rate-limit force-open. Fallbacks, timeouts, idempotency "
    "keys, bulkheads and operator circuit-breaker Blocks stay in force."
)

_HELD_MESSAGE = (
    "The state store did not confirm this change. It is in force in the "
    "process that served this request only, and is retried there on every "
    "refresh for as long as that process runs."
)


def _reach_text() -> str:
    return (
        "Every process sharing the state store applies it within about "
        f"{SYSTEM_CONTROL_REFRESH_INTERVAL_SECONDS:g} seconds."
    )


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
            # True while this process holds a change the store has not
            # confirmed: other processes do not see it.
            "persist_dirty": manager.is_persist_dirty(),
            # This process's reads of the store: reachability, when the copy
            # was last refreshed and how old it is, the last read error, and
            # whether the refresher thread runs.
            **manager.get_refresh_status(),
            "backend": backend_info,
            "timestamp": utc_now().isoformat(),
        }
    )


def _change_response(
    change: SystemControlChange, message: str, **extra: object
) -> ResponseContext:
    persisted = change.persisted is True
    body: dict[str, object] = {
        "success": persisted,
        "message": message if persisted else _HELD_MESSAGE,
        **extra,
        "state": change.state.to_dict(),
        **change.response_fields(),
        "timestamp": utc_now().isoformat(),
    }
    return ResponseContext.json(body, status_code=200 if persisted else 503)


def _store_error_response(error: SystemControlStoreError) -> ResponseContext:
    return ResponseContext.json(
        {
            "success": False,
            "error": "state_store_unavailable",
            "message": str(error),
            "state": get_system_control().get_state(refresh=False).to_dict(),
            "persisted": error.persisted,
            "applies": error.applies,
            "withdrew_held_change": error.withdrew_held_change,
            "may_still_land": error.may_still_land,
            "timestamp": utc_now().isoformat(),
        },
        status_code=503,
    )


def system_enable(ctx: RequestContext) -> ResponseContext:
    """POST /system/enable/ — re-enable baldur (admin-only)."""
    body = ctx.json_body or {}
    reason = body.get("reason", "")
    try:
        change = get_system_control().enable(actor=resolve_actor(ctx), reason=reason)
    except SystemControlStoreError as e:
        return _store_error_response(e)
    return _change_response(change, "Baldur system enabled", reach=_reach_text())


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
    change = get_system_control().disable(actor=resolve_actor(ctx), reason=reason)
    return _change_response(
        change,
        "Baldur system DISABLED (Kill Switch activated)",
        effect=_DISABLED_EFFECT,
        reach=_reach_text(),
    )


def dry_run_enable(ctx: RequestContext) -> ResponseContext:
    """POST /system/dry-run/enable/ — enable dry run mode (admin-only)."""
    change = get_system_control().enable_dry_run(actor=resolve_actor(ctx))
    return _change_response(
        change,
        "Dry run mode ENABLED",
        info="Baldur will observe and log but not take actions",
        reach=_reach_text(),
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
    try:
        change = get_system_control().disable_dry_run(actor=resolve_actor(ctx))
    except SystemControlStoreError as e:
        return _store_error_response(e)
    return _change_response(
        change,
        "Dry run mode DISABLED - Baldur is now LIVE",
        warning="All baldur actions will now be executed for real",
        reach=_reach_text(),
    )
