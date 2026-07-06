"""
Framework-agnostic DLQ Compressed Entries handlers.

Extracted from api/django/views/dlq_compressed.py (Phase 2b).

Endpoints:
    GET /dlq-compressed                              Compressed DLQ entry list
    GET /dlq-compressed/{entry_id}                    Compressed entry detail
    GET /dlq-compressed/summary                       Compressed entry summary
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "dlq_compressed_list",
    "dlq_compressed_detail",
    "dlq_compressed_summary",
]


def _repository():
    from baldur.factory.registry import ProviderRegistry

    repo = ProviderRegistry.dlq_repository.safe_get()
    if repo is None:
        raise RuntimeError("Compressed DLQ handlers require baldur_pro DLQRepository")
    return repo


def _serialize_compressed_entry(entry) -> dict:
    return {
        "id": entry.id,
        "domain": entry.domain,
        "failure_type": entry.failure_type,
        "error_code": entry.error_code,
        "count": entry.count,
        "first_seen": entry.first_seen.isoformat(),
        "last_seen": entry.last_seen.isoformat(),
        "sample_error_message": entry.sample_error_message,
        "sample_context": entry.sample_context,
        "status": entry.status,
        "compressed_at": entry.compressed_at.isoformat(),
        "stale_at": entry.stale_at.isoformat() if entry.stale_at else None,
        "archived_at": entry.archived_at.isoformat() if entry.archived_at else None,
    }


def dlq_compressed_list(ctx: RequestContext) -> ResponseContext:
    """GET /dlq-compressed — compressed DLQ entry list (viewer)."""
    repository = _repository()

    domain = ctx.get_query("domain")
    entry_status = ctx.get_query("status")
    try:
        limit = min(int(ctx.get_query("limit", 100)), 1000)
    except (TypeError, ValueError):
        limit = 100

    entries = repository.get_compressed_entries(
        domain=domain,
        status=entry_status,
        limit=limit,
    )

    return ResponseContext.json(
        {
            "count": len(entries),
            "has_more": len(entries) >= limit,
            "results": [_serialize_compressed_entry(e) for e in entries],
        }
    )


def dlq_compressed_detail(ctx: RequestContext) -> ResponseContext:
    """GET /dlq-compressed/{entry_id} — compressed entry detail (viewer)."""
    entry_id = ctx.get_path_param("entry_id")
    repository = _repository()

    entries = repository.get_compressed_entries(limit=1000)
    entry = next((e for e in entries if e.id == entry_id), None)
    if entry is None:
        return ResponseContext.not_found("Compressed entry not found")

    return ResponseContext.json(_serialize_compressed_entry(entry))


def dlq_compressed_summary(ctx: RequestContext) -> ResponseContext:
    """GET /dlq-compressed/summary — compressed entry summary (viewer)."""
    repository = _repository()
    summary = repository.get_compressed_summary()
    return ResponseContext.json(summary)
