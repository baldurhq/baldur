"""
Framework-agnostic Cascade Event Audit handlers.

Extracted from api/django/views/cascade.py (Phase 2a).

Endpoints:
    GET  /cascade/events/                   Cascade Event list
    GET  /cascade/events/{cascade_id}/      Cascade Event detail
    POST /cascade/verify/                   Hash Chain integrity verification
    GET  /cascade/trace/{event_id}/         Causation trace for an event
    GET  /cascade/checkpoint/               Checkpoint query
    POST /cascade/checkpoint/               Checkpoint creation
    GET  /cascade/load-shedding/status/     Load Shedding status
"""

from __future__ import annotations

import time

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext
from baldur.utils.time import utc_now

logger = structlog.get_logger()

__all__ = [
    "cascade_event_list",
    "cascade_event_detail",
    "cascade_chain_verify",
    "causation_trace",
    "cascade_checkpoint_get",
    "cascade_checkpoint_create",
    "cascade_load_shedding_status",
]


def _get_cascade_auditor():
    """Acquire CascadeEventAuditor singleton."""
    from baldur.audit.cascade_auditor import get_cascade_event_auditor

    return get_cascade_event_auditor()


def cascade_event_list(ctx: RequestContext) -> ResponseContext:
    """GET /cascade/events/ — cascade event list (viewer)."""
    namespace = ctx.get_query("namespace", "global")
    limit = min(int(ctx.get_query("limit", 100)), 1000)
    offset = int(ctx.get_query("offset", 0))
    trigger_type = ctx.get_query("trigger_type")
    is_test_param = ctx.get_query("is_test")

    auditor = _get_cascade_auditor()
    events = auditor.get_recent_events(
        namespace=namespace,
        limit=limit + offset,  # account for offset
    )

    # Apply offset
    events = events[offset : offset + limit]

    # Trigger type filter
    if trigger_type:
        events = [e for e in events if e.trigger.trigger_type == trigger_type]

    # is_test filter (string -> bool conversion)
    if is_test_param is not None:
        is_test_filter = is_test_param.lower() == "true"
        events = [e for e in events if e.is_test == is_test_filter]

    # Build response list
    event_list = []
    for event in events:
        event_list.append(
            {
                "id": event.id,
                "timestamp": event.timestamp,
                "trigger_type": event.trigger.trigger_type,
                "trigger_details": event.trigger.details,
                "effects_count": len(event.effects),
                "namespace": event.namespace,
                "has_external_trace": event.external_trace is not None,
                "is_test": event.is_test,
            }
        )

    # Total count
    total = auditor.get_event_count(namespace)

    return ResponseContext.json(
        {
            "success": True,
            "namespace": namespace,
            "events": event_list,
            "total": total,
            "limit": limit,
            "offset": offset,
            "timestamp": utc_now().isoformat(),
        }
    )


def cascade_event_detail(ctx: RequestContext) -> ResponseContext:
    """GET /cascade/events/{cascade_id}/ — cascade event detail (viewer)."""
    cascade_id = ctx.get_path_param("cascade_id", "")
    namespace = ctx.get_query("namespace", "global")

    auditor = _get_cascade_auditor()
    event = auditor.get_cascade_event(cascade_id, namespace)

    if not event:
        return ResponseContext.not_found(f"Cascade Event {cascade_id} not found")

    # Build effects list
    effects_list = []
    for effect in event.effects:
        effect_dict = {
            "action_type": effect.action_type,
            "event_id": effect.event_id,
            "success": effect.success,
            "caused_by": effect.caused_by,
            "details": effect.details,
            "timestamp": effect.executed_at,
        }
        if effect.error_message:
            effect_dict["error_message"] = effect.error_message
        effects_list.append(effect_dict)

    # External trace info
    external_trace = None
    if event.external_trace:
        external_trace = event.external_trace.to_dict()

    return ResponseContext.json(
        {
            "success": True,
            "event": {
                "id": event.id,
                "timestamp": event.timestamp,
                "namespace": event.namespace,
                "trigger": {
                    "type": event.trigger.trigger_type,
                    "event_id": event.trigger.event_id,
                    "details": event.trigger.details,
                    "triggered_by": event.trigger.triggered_by,
                },
                "effects": effects_list,
                "causation_chain": event.get_causation_chain(),
                "external_trace": external_trace,
                "hash_chain": {
                    "previous_hash": event.previous_hash,
                    "current_hash": event.current_hash,
                },
            },
            "timestamp": utc_now().isoformat(),
        }
    )


def cascade_chain_verify(ctx: RequestContext) -> ResponseContext:
    """POST /cascade/verify/ — hash chain integrity verification (admin)."""
    body = ctx.json_body or {}
    namespace = body.get("namespace", "global")
    from_checkpoint = body.get("from_checkpoint", True)
    full_verify = body.get("full_verify", False)

    start_time = time.time()

    auditor = _get_cascade_auditor()

    # Run verification
    if full_verify or not from_checkpoint:
        result = auditor.verify_chain_integrity(namespace)
    else:
        result = auditor.verify_chain_integrity_from_checkpoint(namespace)

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Checkpoint info
    checkpoint_info = None
    if from_checkpoint and not full_verify:
        checkpoint = auditor.get_checkpoint(namespace)
        if checkpoint:
            checkpoint_info = {
                "timestamp": checkpoint.get("timestamp"),
                "hash": checkpoint.get("last_hash"),
            }

    return ResponseContext.json(
        {
            "success": True,
            "valid": result.get("valid", False),
            "namespace": namespace,
            "verified_count": result.get("verified_count", 0),
            "from_checkpoint": from_checkpoint and not full_verify,
            "checkpoint": checkpoint_info,
            "verification_time_ms": elapsed_ms,
            "errors": result.get("errors", []),
            "timestamp": utc_now().isoformat(),
        }
    )


def causation_trace(ctx: RequestContext) -> ResponseContext:
    """GET /cascade/trace/{event_id}/ — causation trace for an event (viewer)."""
    event_id = ctx.get_path_param("event_id", "")
    namespace = ctx.get_query("namespace", "global")
    direction = ctx.get_query("direction", "ancestors")

    if direction not in ("ancestors", "descendants", "both"):
        return ResponseContext.bad_request(
            "direction must be one of: ancestors, descendants, both"
        )

    auditor = _get_cascade_auditor()

    # Causation trace
    trace = auditor.trace_causation(event_id, namespace)

    if not trace:
        return ResponseContext.not_found(
            f"Event {event_id} not found or no causation trace"
        )

    # Extract cascade_id from trace items
    cascade_id = None
    if trace:
        for item in trace:
            if "cascade_id" in item:
                cascade_id = item["cascade_id"]
                break

    return ResponseContext.json(
        {
            "success": True,
            "event_id": event_id,
            "direction": direction,
            "trace": trace,
            "cascade_id": cascade_id,
            "namespace": namespace,
            "timestamp": utc_now().isoformat(),
        }
    )


def cascade_checkpoint_get(ctx: RequestContext) -> ResponseContext:
    """GET /cascade/checkpoint/ — checkpoint query (admin)."""
    namespace = ctx.get_query("namespace", "global")

    auditor = _get_cascade_auditor()
    checkpoint = auditor.get_checkpoint(namespace)

    return ResponseContext.json(
        {
            "success": True,
            "namespace": namespace,
            "checkpoint": checkpoint,
            "timestamp": utc_now().isoformat(),
        }
    )


def cascade_checkpoint_create(ctx: RequestContext) -> ResponseContext:
    """POST /cascade/checkpoint/ — checkpoint creation (admin)."""
    body = ctx.json_body or {}
    namespace = body.get("namespace", "global")

    auditor = _get_cascade_auditor()
    checkpoint = auditor.create_checkpoint(namespace)

    logger.info(
        "cascade_api.checkpoint_created",
        namespace=namespace,
    )

    return ResponseContext.json(
        {
            "success": True,
            "namespace": namespace,
            "checkpoint": checkpoint,
            "timestamp": utc_now().isoformat(),
        }
    )


def cascade_load_shedding_status(ctx: RequestContext) -> ResponseContext:
    """GET /cascade/load-shedding/status/ — load shedding status (viewer)."""
    from baldur.audit.cascade_load_shedding import get_cascade_load_shedding

    load_shedding = get_cascade_load_shedding()
    # get_status() requires runtime buffer stats; admin endpoint exposes
    # current config + zero-baseline so operators can see policy without
    # tying into the runtime audit buffer here. Real buffer stats are
    # surfaced separately via /audit/buffer endpoints.
    status_info = load_shedding.get_status(buffer_size=0, buffer_capacity=1)

    return ResponseContext.json(
        {
            "success": True,
            **status_info,
            "timestamp": utc_now().isoformat(),
        }
    )
