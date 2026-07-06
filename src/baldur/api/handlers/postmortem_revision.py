"""
Framework-agnostic Postmortem Revision handlers.

Extracted from api/django/views/postmortem_revision.py (Phase 2b).

Endpoints:
    GET   /postmortem/{incident_id}/revisions                 Revision list
    POST  /postmortem/{incident_id}/revisions                 Create revision
    GET   /postmortem/{incident_id}/revisions/{revision_number}  Revision detail
    GET   /postmortem/{incident_id}/revisions/compare         Compare revisions
    POST  /postmortem/{incident_id}/seal                      Seal postmortem
    DELETE /postmortem/{incident_id}/seal                     Unseal postmortem
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "postmortem_revision_list",
    "postmortem_revision_create",
    "postmortem_revision_detail",
    "postmortem_revision_compare",
    "postmortem_seal",
    "postmortem_unseal",
]


def _manager():
    try:
        from baldur_pro.services.postmortem.revision import (
            get_postmortem_revision_manager,
        )
    except ImportError:
        get_postmortem_revision_manager = None  # type: ignore[assignment,misc]

    return get_postmortem_revision_manager()


def _write_revision_audit_log(
    event_type: str,
    incident_id: str,
    actor_id: str,
    details: dict[str, Any],
) -> None:
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type=event_type,
            source="API.PostmortemRevision",
            details=details,
            success=True,
            domain="baldur",
            target_id=incident_id,
        )
    except Exception as e:
        logger.warning("postmortem_revision.write_audit_log_failed", error=e)


def postmortem_revision_list(ctx: RequestContext) -> ResponseContext:
    """GET /postmortem/{incident_id}/revisions — revision list (viewer)."""
    incident_id = ctx.get_path_param("incident_id")
    manager = _manager()
    summary = manager.get_revision_summary(incident_id)

    return ResponseContext.json(
        {
            "status": "success",
            "revisions": summary["revisions"],
            "total_count": summary["total_count"],
            "is_sealed": summary["is_sealed"],
            "latest_revision": summary["latest_revision"],
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_revision_create(ctx: RequestContext) -> ResponseContext:
    """POST /postmortem/{incident_id}/revisions — create revision (operator)."""
    incident_id = ctx.get_path_param("incident_id")
    body = ctx.json_body or {}

    data = body.get("data")
    change_reason = body.get("change_reason")
    change_type_str = body.get("change_type", "analysis_update")

    if not data:
        return ResponseContext.bad_request("data field is required")
    if not change_reason:
        return ResponseContext.bad_request("change_reason field is required")

    from baldur.models.runtime_config import RevisionChangeType

    try:
        change_type = RevisionChangeType(change_type_str)
    except ValueError:
        return ResponseContext.bad_request(f"Invalid change_type: {change_type_str}")

    actor = resolve_actor(ctx)
    manager = _manager()

    try:
        revision = manager.create_revision(
            incident_id=incident_id,
            new_data=data,
            changed_by=actor,
            change_reason=change_reason,
            change_type=change_type,
        )
    except ValueError as e:
        return ResponseContext.bad_request(str(e))

    _write_revision_audit_log(
        event_type="POSTMORTEM_REVISION_CREATED",
        incident_id=incident_id,
        actor_id=actor,
        details={
            "revision_id": revision.revision_id,
            "revision_number": revision.revision_number,
            "change_type": revision.change_type.value,
            "change_reason": change_reason,
            "actor_id": actor,
        },
    )

    return ResponseContext.created(
        {
            "status": "success",
            "revision": revision.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_revision_detail(ctx: RequestContext) -> ResponseContext:
    """GET /postmortem/{incident_id}/revisions/{revision_number} — revision detail (viewer)."""
    incident_id = ctx.get_path_param("incident_id")
    try:
        revision_number = int(ctx.get_path_param("revision_number"))
    except (TypeError, ValueError):
        return ResponseContext.bad_request("revision_number must be an integer")

    manager = _manager()
    revision = manager.get_revision(incident_id, revision_number)

    if revision is None:
        return ResponseContext.not_found(
            f"Revision {revision_number} not found for {incident_id}"
        )

    return ResponseContext.json(
        {
            "status": "success",
            "revision": revision.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_revision_compare(ctx: RequestContext) -> ResponseContext:
    """GET /postmortem/{incident_id}/revisions/compare — compare revisions (viewer)."""
    incident_id = ctx.get_path_param("incident_id")

    revision_a_str = ctx.get_query("a")
    revision_b_str = ctx.get_query("b")

    if not revision_a_str or not revision_b_str:
        return ResponseContext.bad_request(
            "Both 'a' and 'b' query parameters are required"
        )

    try:
        revision_a = int(revision_a_str)
        revision_b = int(revision_b_str)
    except ValueError:
        return ResponseContext.bad_request("Parameters 'a' and 'b' must be integers")

    manager = _manager()

    try:
        diff = manager.compare_revisions(incident_id, revision_a, revision_b)
    except ValueError as e:
        return ResponseContext.not_found(str(e))

    return ResponseContext.json(
        {
            "status": "success",
            "comparison": {
                "revision_a": revision_a,
                "revision_b": revision_b,
                "added": diff.added,
                "removed": diff.removed,
                "modified": diff.modified,
                "unchanged": diff.unchanged,
                "has_changes": diff.has_changes,
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_seal(ctx: RequestContext) -> ResponseContext:
    """POST /postmortem/{incident_id}/seal — seal postmortem (admin)."""
    incident_id = ctx.get_path_param("incident_id")
    body = ctx.json_body or {}
    seal_reason = body.get("seal_reason", "Analysis completed")
    actor = resolve_actor(ctx)

    manager = _manager()

    try:
        seal_revision = manager.seal_postmortem(
            incident_id=incident_id,
            sealed_by=actor,
            seal_reason=seal_reason,
        )
    except ValueError as e:
        return ResponseContext.bad_request(str(e))

    _write_revision_audit_log(
        event_type="POSTMORTEM_SEALED",
        incident_id=incident_id,
        actor_id=actor,
        details={
            "revision_id": seal_revision.revision_id,
            "revision_number": seal_revision.revision_number,
            "seal_reason": seal_reason,
            "actor_id": actor,
        },
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Postmortem '{incident_id}' sealed successfully",
            "revision": seal_revision.to_dict(),
            "timestamp": utc_now().isoformat(),
        }
    )


def postmortem_unseal(ctx: RequestContext) -> ResponseContext:
    """DELETE /postmortem/{incident_id}/seal — unseal postmortem (admin)."""
    incident_id = ctx.get_path_param("incident_id")
    body = ctx.json_body or {}
    unseal_reason = body.get("unseal_reason")
    approval_chain = body.get("approval_chain", [])

    if not unseal_reason:
        return ResponseContext.bad_request("unseal_reason is required")

    actor = resolve_actor(ctx)
    manager = _manager()

    try:
        manager.unseal_postmortem(
            incident_id=incident_id,
            unsealed_by=actor,
            unseal_reason=unseal_reason,
            approval_chain=approval_chain,
        )
    except ValueError as e:
        return ResponseContext.bad_request(str(e))

    _write_revision_audit_log(
        event_type="POSTMORTEM_UNSEALED",
        incident_id=incident_id,
        actor_id=actor,
        details={
            "unseal_reason": unseal_reason,
            "approval_chain": approval_chain,
            "actor_id": actor,
        },
    )

    return ResponseContext.json(
        {
            "status": "success",
            "message": f"Postmortem '{incident_id}' unsealed successfully",
            "timestamp": utc_now().isoformat(),
        }
    )
