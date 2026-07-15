"""
Framework-agnostic DLQ (Dead Letter Queue) management handlers.

Extracted from api/django/views/dlq.py. Delegates to baldur_pro DLQ service
for business logic; this module only adapts request/response shapes.

Endpoints:
    POST /dlq/replay/                  Trigger DLQ replay
    GET  /dlq/cleanup/stats/           Cleanup statistics
    POST /dlq/cleanup/archive/         Archive old resolved entries
    POST /dlq/cleanup/purge/           Destructive purge (admin)
    GET  /dlq/facets/                  Faceted status×domain counts (filter UI)
    GET  /dlq/list/                    Paginated list
    GET  /dlq/{pk}/                    Single entry detail
    POST /dlq/{pk}/retry/              Retry single entry
    POST /dlq/{pk}/resolve/            Manual resolve
    POST /dlq/{pk}/force-redrive/      Force-redrive an at-cap entry (admin)
    POST /dlq/test/create/             Test entry (admin; debug-only)
"""

from __future__ import annotations

import structlog

from baldur.api.handlers._common import resolve_actor
from baldur.core.exceptions import (
    DLQEntryNotFoundError,
    DLQError,
    DLQReplayError,
    DLQStateConflictError,
)
from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "dlq_replay",
    "dlq_cleanup_stats",
    "dlq_cleanup_archive",
    "dlq_cleanup_purge",
    "dlq_facets",
    "dlq_list",
    "dlq_detail",
    "dlq_retry",
    "dlq_resolve",
    "dlq_force_redrive",
    "dlq_test_create",
]


def _dlq_error_response(exc: DLQError) -> ResponseContext | None:
    """Map a DLQ domain exception to a semantic HTTP response.

    Keeps HTTP knowledge in the handler layer (the service raises framework-
    agnostic semantic types). Most-specific subclass first; returns ``None``
    for an unrecognised ``DLQError`` subclass so the caller can re-raise.

    - ``DLQEntryNotFoundError`` -> 404 Not Found
    - ``DLQStateConflictError`` -> 409 Conflict (resolved/archived/at-cap/
      double-click)
    - ``DLQReplayError`` -> 500 (unexpected replay-execution failure)
    - other ``DLQError`` -> 400 Bad Request
    """
    if isinstance(exc, DLQEntryNotFoundError):
        return ResponseContext.json({"error": str(exc)}, status_code=404)
    if isinstance(exc, DLQStateConflictError):
        return ResponseContext.json({"error": str(exc)}, status_code=409)
    if isinstance(exc, DLQReplayError):
        return ResponseContext.json({"error": str(exc)}, status_code=500)
    if isinstance(exc, DLQError):
        return ResponseContext.json({"error": str(exc)}, status_code=400)
    return None


def _get_service():
    from baldur.factory.registry import ProviderRegistry

    service = ProviderRegistry.dlq_service.safe_get()
    if service is None:
        raise RuntimeError("DLQ handlers require baldur_pro DLQService")
    return service


def _get_read_service():
    """Resolve the DLQ read + single-entry-action backing (always resolves).

    Registry-first: the PRO ``DLQService`` (registered under ACTIVE
    entitlement) supersedes; otherwise the OSS ``DLQReadService``. Unlike
    ``_get_service()`` there is no ``RuntimeError`` exit — the read + single-entry
    surface (list / detail / facets / cleanup stats / retry / resolve /
    force-redrive) is OSS-available.
    """
    from baldur.factory.registry import ProviderRegistry
    from baldur.services.dlq_read import get_dlq_read_service

    service = ProviderRegistry.dlq_service.safe_get()
    if service is not None:
        return service
    return get_dlq_read_service()


def _parse_int(raw, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _parse_pk(ctx: RequestContext) -> str | None:
    # 538 D1: DLQ ids are opaque strings (composite token for the Redis
    # adapter). Return the raw path param verbatim; None only when absent.
    raw = ctx.get_path_param("pk")
    if raw is None or raw == "":
        return None
    return str(raw)


def _parse_reason(body: dict) -> tuple[str | None, ResponseContext | None]:
    """Validate the optional ``reason`` field (<=500 chars).

    Returns ``(reason, None)`` on success (reason is None when absent), or
    ``(None, error_response)`` with a 400 when the value is malformed. Mirrors
    the validation shape used by ``dlq_force_redrive``, but here ``reason`` is
    optional — replay is the intended non-destructive recovery path.
    """
    reason = body.get("reason")
    if reason is None:
        return None, None
    if not isinstance(reason, str):
        return None, ResponseContext.json(
            {"error": "reason must be a string"}, status_code=400
        )
    if len(reason) > 500:
        return None, ResponseContext.json(
            {"error": "reason exceeds 500 chars"}, status_code=400
        )
    return reason, None


def dlq_replay(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/replay/ — trigger DLQ replay (operator)."""
    body = ctx.json_body or {}
    batch_size = body.get("batch_size", 50)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        return ResponseContext.json(
            {"error": "batch_size must be an integer"}, status_code=400
        )
    if batch_size < 1 or batch_size > 200:
        return ResponseContext.json(
            {"error": "batch_size must be between 1 and 200"}, status_code=400
        )

    reason, reason_error = _parse_reason(body)
    if reason_error is not None:
        return reason_error

    domain = body.get("domain")
    actor = resolve_actor(ctx)

    service = _get_service()
    result = service.replay(
        domain=domain, batch_size=batch_size, actor_id=actor, reason=reason
    )

    logger.info(
        "dlq.replay_triggered_via_api",
        healing_domain=domain,
        batch_size=batch_size,
        processed_count=result.processed,
        success=result.success,
        failed=result.failed,
        request_user=actor,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "total": result.processed,
            "success_count": result.success,
            "failed_count": result.failed,
            "skipped_count": result.skipped,
        }
    )


def _auto_replay_arming_block() -> dict:
    """Compute the on-recovery arming block for the stats payload (fail-open).

    Uses the arming probe as the single source of truth. Any error resolves to
    a ``probe_failed`` block (``armed: null``) rather than a 500 — the console
    polls this handler, so it must never fault on a probe hiccup.
    """
    try:
        from baldur.services.replay_service.arming import get_on_recovery_arming_status

        status = get_on_recovery_arming_status()
        return {
            "armed": status.armed,
            "missing_link": status.missing_link,
            "missing_links": status.missing_links,
            "links": status.links,
        }
    except Exception:
        return {
            "armed": None,
            "missing_link": "probe_failed",
            "missing_links": ["probe_failed"],
            "links": {},
        }


def dlq_cleanup_stats(ctx: RequestContext) -> ResponseContext:
    """GET /dlq/cleanup/stats/ — cleanup statistics (viewer+)."""
    service = _get_read_service()
    stats = service.get_cleanup_stats()

    return ResponseContext.json(
        {
            "total": stats.total,
            "by_status": stats.by_status,
            "resolved_older_than_30_days": stats.resolved_older_than_30_days,
            "archived_older_than_90_days": stats.archived_older_than_90_days,
            "recommendations": {
                "can_archive": stats.can_archive,
                "can_purge": stats.can_purge,
            },
            "auto_replay": _auto_replay_arming_block(),
        }
    )


def dlq_cleanup_archive(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/cleanup/archive/ — archive old resolved entries (operator)."""
    body = ctx.json_body or {}
    older_than_days = _parse_int(body.get("older_than_days", 30), 30)

    service = _get_service()
    count = service.archive_old_entries(older_than_days=older_than_days)

    logger.info(
        "dlq.archived_entries_via_api",
        count=count,
        older_than_days=older_than_days,
        request_user=resolve_actor(ctx),
    )

    return ResponseContext.json(
        {
            "status": "success",
            "archived_count": count,
            "older_than_days": older_than_days,
        }
    )


def dlq_cleanup_purge(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/cleanup/purge/ — destructive purge of archived entries (admin)."""
    body = ctx.json_body or {}
    if not body.get("confirm"):
        return ResponseContext.json(
            {
                "status": "error",
                "error": "Safety check: set confirm=true to proceed with deletion",
            },
            status_code=400,
        )

    ids = body.get("ids")
    # The console sends IDs as a comma-separated string (structured form field);
    # API callers may send a JSON list. Normalize either to a list (or None).
    if isinstance(ids, str):
        ids = [s.strip() for s in ids.split(",") if s.strip()] or None

    older_than_days_raw = body.get("older_than_days")
    older_than_days = (
        _parse_int(older_than_days_raw, 0) if older_than_days_raw is not None else None
    )
    # ids and older_than_days are mutually exclusive at the repository (it raises
    # when both are given). An explicit id selection wins over the age filter so
    # the console form's default older_than_days does not collide with a pasted
    # id list.
    if ids:
        older_than_days = None

    service = _get_service()
    count = service.purge_archived(ids=ids, older_than_days=older_than_days)

    logger.warning(
        "dlq.purged_archived_entries_via_api",
        count=count,
        request_user=resolve_actor(ctx),
    )

    return ResponseContext.json(
        {
            "status": "success",
            "purged_count": count,
            "warning": "This action is irreversible",
        }
    )


def dlq_facets(ctx: RequestContext) -> ResponseContext:
    """GET /dlq/facets/ — faceted status×domain counts for the filter UI.

    Returns ``{by_status, by_domain}`` with zero-count buckets dropped.
    ``by_status`` is scoped by the ``domain`` query param and ``by_domain``
    is scoped by ``status`` (standard faceted-search — each facet excludes
    its own selection so the dimension being chosen keeps all of its
    options). An unfiltered call returns the complete maps. See
    ``FailedOperationRepository.get_facet_counts`` for the full contract.
    """
    status_filter = ctx.get_query("status") or None
    domain_filter = ctx.get_query("domain") or None

    service = _get_read_service()
    counts = service.get_facet_counts(status=status_filter, domain=domain_filter)

    return ResponseContext.json(
        {
            "by_status": counts.get("by_status", {}),
            "by_domain": counts.get("by_domain", {}),
        }
    )


def dlq_list(ctx: RequestContext) -> ResponseContext:
    """GET /dlq/list/ — paginated list (viewer+)."""
    filters: dict[str, str] = {}
    status_filter = ctx.get_query("status")
    domain_filter = ctx.get_query("domain")
    if status_filter:
        filters["status"] = status_filter
    if domain_filter:
        filters["domain"] = domain_filter

    page = _parse_int(ctx.get_query("page", 1), 1)
    page_size = _parse_int(ctx.get_query("page_size", 20), 20)

    service = _get_read_service()
    result = service.list_entries(filters=filters, page=page, page_size=page_size)

    return ResponseContext.json(
        {
            "results": result["results"],
            "pagination": {
                "page": result["page"],
                "page_size": result["page_size"],
                "total_pages": result["total_pages"],
                "total_count": result["total_count"],
                "has_next": result["has_next"],
                "has_previous": result["has_previous"],
            },
        }
    )


def dlq_detail(ctx: RequestContext) -> ResponseContext:
    """GET /dlq/{pk}/ — single entry detail (viewer+)."""
    pk = _parse_pk(ctx)
    if pk is None:
        return ResponseContext.json({"error": "pk is required"}, status_code=400)

    service = _get_read_service()
    entry = service.get_entry(pk)
    if entry is None:
        return ResponseContext.json(
            {"error": f"DLQ entry {pk} not found"}, status_code=404
        )

    return ResponseContext.json(entry)


def dlq_retry(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/{pk}/retry/ — retry a single entry (operator)."""
    pk = _parse_pk(ctx)
    if pk is None:
        return ResponseContext.json({"error": "pk is required"}, status_code=400)

    body = ctx.json_body or {}
    reason, reason_error = _parse_reason(body)
    if reason_error is not None:
        return reason_error

    service = _get_read_service()
    try:
        result = service.retry_entry(pk, reason=reason)
    except DLQError as exc:
        resp = _dlq_error_response(exc)
        if resp is not None:
            return resp
        raise

    logger.info(
        "dlq.retry_triggered_entry_user",
        pk=pk,
        request_user=resolve_actor(ctx),
    )

    # retry_entry now re-executes the entry (606 D8): success reflects whether
    # the replay handler succeeded, not merely that the counter advanced.
    return ResponseContext.json(
        {
            "status": "success" if result["success"] else "failed",
            "id": result["id"],
            "retry_count": result["retry_count"],
            "previous_retry_count": result["previous_retry_count"],
            "entry_status": result.get("status"),
            "message": result["message"],
        }
    )


def dlq_resolve(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/{pk}/resolve/ — manual resolve (operator)."""
    pk = _parse_pk(ctx)
    if pk is None:
        return ResponseContext.json({"error": "pk is required"}, status_code=400)

    body = ctx.json_body or {}
    actor = resolve_actor(ctx)
    notes = body.get("notes") or f"Manually resolved by {actor}"

    service = _get_read_service()
    try:
        result = service.resolve_entry(pk, notes=notes)
    except DLQError as exc:
        resp = _dlq_error_response(exc)
        if resp is not None:
            return resp
        raise

    logger.info(
        "dlq.entry_manually_resolved_user",
        pk=pk,
        request_user=actor,
        notes=notes,
    )

    return ResponseContext.json(
        {
            "status": "success",
            "id": result["id"],
            "previous_status": result["previous_status"],
            "current_status": result["current_status"],
            "resolved_at": result["resolved_at"],
            "notes": result["notes"],
        }
    )


def dlq_force_redrive(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/{pk}/force-redrive/ — force-redrive an at-cap entry (admin).

    A deliberate, ADMIN-gated cap-override: re-drive an entry parked in
    REQUIRES_REVIEW after a root-cause fix. Requires a ``reason`` (<=500 chars,
    mirroring the CB control override) and accepts an optional ``ticket_url``;
    both flow into the distinct DLQ_FORCE_REDRIVE audit event. A missing/blank
    reason returns 400 before any state change.
    """
    pk = _parse_pk(ctx)
    if pk is None:
        return ResponseContext.json({"error": "pk is required"}, status_code=400)

    body = ctx.json_body or {}
    reason = body.get("reason")
    if not reason or not isinstance(reason, str) or not reason.strip():
        return ResponseContext.json({"error": "reason is required"}, status_code=400)
    if len(reason) > 500:
        return ResponseContext.json(
            {"error": "reason exceeds 500 chars"}, status_code=400
        )

    ticket_url = body.get("ticket_url")
    if ticket_url is not None and (
        not isinstance(ticket_url, str) or len(ticket_url) > 500
    ):
        return ResponseContext.json(
            {"error": "ticket_url must be a string up to 500 chars"},
            status_code=400,
        )

    actor = resolve_actor(ctx)
    service = _get_read_service()
    try:
        result = service.force_redrive_entry(
            pk, actor_id=actor, reason=reason, ticket_url=ticket_url
        )
    except DLQError as exc:
        resp = _dlq_error_response(exc)
        if resp is not None:
            return resp
        raise

    logger.info(
        "dlq.force_redrive_triggered_entry_user",
        pk=pk,
        request_user=actor,
    )

    return ResponseContext.json(
        {
            "status": "success" if result["success"] else "failed",
            "id": result["id"],
            "retry_count": result["retry_count"],
            "previous_retry_count": result["previous_retry_count"],
            "entry_status": result.get("status"),
            "message": result["message"],
        }
    )


def dlq_test_create(ctx: RequestContext) -> ResponseContext:
    """POST /dlq/test/create/ — create test entry (admin; debug-only).

    Returns 201 Created. Callers that don't run DEBUG mode typically gate
    this endpoint at the framework layer.
    """
    body = ctx.json_body or {}
    user = ctx.user
    user_id = getattr(user, "id", None) if user is not None else None

    service = _get_service()
    result = service.create_test_entry(
        healing_domain=body.get("domain"),
        failure_type=body.get("failure_type"),
        user_id=user_id,
        entity_type=body.get("entity_type", "test"),
        entity_id=body.get("entity_id", ""),
        error_message=body.get("error_message", "Test failure for load testing"),
        snapshot_data=body.get("snapshot_data"),
        request_data=body.get("request_data"),
        response_data=body.get("response_data"),
        metadata=body.get("metadata"),
        created_by=resolve_actor(ctx),
    )

    logger.info(
        "dlq.test_entry_created",
        result=result.get("dlq_id") if isinstance(result, dict) else None,
        healing_domain=body.get("domain"),
        failure_type=body.get("failure_type"),
        request_user=resolve_actor(ctx),
    )

    return ResponseContext.json(result, status_code=201)
