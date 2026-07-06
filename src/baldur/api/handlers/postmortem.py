"""
Framework-agnostic Post-mortem handlers.

Extracted from api/django/views/postmortem.py (Phase 2b).

Endpoints:
    POST /postmortem/generate                       Generate post-mortem report
    GET  /postmortem/incidents                       List incidents
    GET  /postmortem/incidents/{incident_id}         Incident detail
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.dlq.helpers import (
    add_healing_incident,
    get_healing_incidents,
    get_healing_incidents_count,
)
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "postmortem_generate",
    "postmortem_incidents_list",
    "postmortem_incident_detail",
]


def _log_postmortem_audit(
    incident_id: str,
    affected_services: list,
    duration_seconds: float | None,
    user: str,
) -> None:
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type="POSTMORTEM_MANUAL_GENERATED",
            source="API.Postmortem",
            details={
                "incident_id": incident_id,
                "affected_services": affected_services,
                "duration_seconds": duration_seconds,
                "triggered_by": user,
            },
            success=True,
            domain="baldur",
            target_id=incident_id,
        )
    except Exception as e:
        logger.warning("postmortem.log_audit_failed", error=e)


def postmortem_generate(ctx: RequestContext) -> ResponseContext:
    """POST /postmortem/generate — generate post-mortem report (operator)."""
    body = ctx.json_body or {}
    incident_id = body.get("incident_id")

    from baldur.api.django.views.xtest.base import (
        collect_system_snapshot,
        get_healing_events,
    )
    from baldur.services.circuit_breaker import get_circuit_breaker_service
    from baldur.services.event_bus import get_event_bus

    try:
        from baldur_pro.services.postmortem.store import (
            build_timeline,
            collect_service_states,
            generate_postmortem_data,
        )
    except ImportError:
        build_timeline = None  # type: ignore[assignment,misc]
        collect_service_states = None  # type: ignore[assignment,misc]
        generate_postmortem_data = None  # type: ignore[assignment,misc]

    bus = get_event_bus()
    try:
        from baldur.settings.api_view import get_api_view_settings

        history_limit = get_api_view_settings().postmortem_history_limit
    except Exception:
        history_limit = 100

    history = bus.get_history(limit=history_limit)
    cb_service = get_circuit_breaker_service()

    affected, unaffected = collect_service_states(cb_service)
    snapshot = collect_system_snapshot()
    local_events = get_healing_events(20)
    timeline = build_timeline(history, local_events)

    if not incident_id:
        incident_id = f"HEAL-{utc_now().strftime('%Y-%m%d-%H%M')}"

    fast_fail_count = len([e for e in history if e.get("data", {}).get("fast_fail")])

    postmortem = generate_postmortem_data(
        incident_id, timeline, affected, unaffected, fast_fail_count, snapshot
    )

    add_healing_incident(postmortem)

    logger.info("postmortem.postmortem_generated", incident_id=incident_id)

    actor = resolve_actor(ctx)
    _log_postmortem_audit(
        incident_id=incident_id,
        affected_services=affected,
        duration_seconds=postmortem.get("duration_seconds"),
        user=actor,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "postmortem": postmortem,
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_incidents_list(ctx: RequestContext) -> ResponseContext:
    """GET /postmortem/incidents — list incidents (viewer)."""
    try:
        from baldur.settings.postmortem import get_postmortem_settings

        default_limit = get_postmortem_settings().incidents_default_limit
    except Exception:
        default_limit = 10

    try:
        limit = int(ctx.get_query("limit", default_limit))
        offset = int(ctx.get_query("offset", 0))
    except (TypeError, ValueError):
        limit = default_limit
        offset = 0

    start_date = ctx.get_query("start_date")
    end_date = ctx.get_query("end_date")
    service = ctx.get_query("service")
    min_duration_str = ctx.get_query("min_duration")
    min_duration = float(min_duration_str) if min_duration_str else None

    incidents = get_healing_incidents(
        limit=limit,
        offset=offset,
        start_date=start_date,
        end_date=end_date,
        service=service,
        min_duration=min_duration,
    )

    total_count = get_healing_incidents_count(
        start_date=start_date,
        end_date=end_date,
        service=service,
        min_duration=min_duration,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "incidents": incidents,
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_incident_detail(ctx: RequestContext) -> ResponseContext:
    """GET /postmortem/incidents/{incident_id} — incident detail (viewer)."""
    incident_id = ctx.get_path_param("incident_id")
    try:
        from baldur_pro.services.postmortem.store import get_incident_by_id
    except ImportError:
        get_incident_by_id = None  # type: ignore[assignment,misc]

    incident = get_incident_by_id(incident_id)

    if incident is None:
        return ResponseContext.not_found(f"Incident with ID '{incident_id}' not found")

    return ResponseContext.json(
        {
            "status": "success",
            "incident": incident,
            "timestamp": utc_now().isoformat(),
        }
    )
