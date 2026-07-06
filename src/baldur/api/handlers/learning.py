"""
Framework-agnostic Self-Learning DNA handlers.

Extracted from api/django/views/learning.py (Phase 2b).

Endpoints:
    POST /learning/session/{action}                  Start/end learning session
    GET  /learning/pattern                           List patterns
    POST /learning/pattern                           Learn pattern
    GET  /learning/suggestion                        List suggestions
    POST /learning/suggestion/{suggestion_id}        Apply suggestion
    POST /learning/metric                            Record metric
    GET  /learning/insights                          Cross-stage insights
"""

from __future__ import annotations

import structlog

from baldur.interfaces.web_framework import RequestContext, ResponseContext

logger = structlog.get_logger()

__all__ = [
    "learning_session_action",
    "learning_pattern_list",
    "learning_pattern_create",
    "learning_suggestion_list",
    "learning_suggestion_apply",
    "learning_metric_record",
    "learning_insights",
]


def _service():
    # 599 D7 — canary/chaos pattern: the implementation lives in the
    # private distribution; resolve via the registry slot populated by
    # register_dormant_services(). None -> handlers return 503.
    from baldur.factory.registry import ProviderRegistry

    return ProviderRegistry.learning_service.safe_get()


def learning_session_action(ctx: RequestContext) -> ResponseContext:
    """POST /learning/session/{action} — start/end session (operator)."""
    action = ctx.get_path_param("action")
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    body = ctx.json_body or {}

    if action == "start":
        service_name = body.get("service_name", "default")
        session = service.start_session(service_name)
        return ResponseContext.created(session.to_dict())

    if action == "end":
        session_id = body.get("session_id")
        if not session_id:
            return ResponseContext.bad_request("session_id required")
        session = service.end_session(session_id)
        if not session:
            return ResponseContext.not_found("Session not found")
        return ResponseContext.json(session.to_dict())

    return ResponseContext.bad_request(f"Invalid action: {action}")


def learning_pattern_list(ctx: RequestContext) -> ResponseContext:
    """GET /learning/pattern — list patterns (viewer)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    pattern_type = ctx.get_query("type")
    try:
        min_confidence = float(ctx.get_query("min_confidence", "0.0"))
    except (TypeError, ValueError):
        min_confidence = 0.0

    from baldur.models.learning import PatternType

    pt = None
    if pattern_type:
        try:
            pt = PatternType(pattern_type)
        except ValueError:
            pass

    patterns = service.get_patterns(pattern_type=pt, min_confidence=min_confidence)
    return ResponseContext.json({"patterns": [p.to_dict() for p in patterns]})


def learning_pattern_create(ctx: RequestContext) -> ResponseContext:
    """POST /learning/pattern — learn pattern (operator)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    body = ctx.json_body or {}
    from baldur.models.learning import PatternType

    pattern = service.learn_pattern(
        pattern_type=PatternType(body.get("pattern_type", "failure")),
        name=body.get("name"),
        description=body.get("description", ""),
        features=body.get("features", {}),
        confidence=body.get("confidence", 0.8),
        session_id=body.get("session_id"),
        metadata=body.get("metadata", {}),
    )
    return ResponseContext.created(pattern.to_dict())


def learning_suggestion_list(ctx: RequestContext) -> ResponseContext:
    """GET /learning/suggestion — list suggestions (viewer)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    service_name = ctx.get_query("service_name")
    unapplied = ctx.get_query("unapplied", "false").lower() == "true"

    suggestions = service.get_suggestions(
        service_name=service_name,
        unapplied_only=unapplied,
    )
    return ResponseContext.json({"suggestions": [s.to_dict() for s in suggestions]})


def learning_suggestion_apply(ctx: RequestContext) -> ResponseContext:
    """POST /learning/suggestion/{suggestion_id} — apply suggestion (operator)."""
    suggestion_id = ctx.get_path_param("suggestion_id")
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    if service.apply_suggestion(suggestion_id):
        return ResponseContext.json({"message": "Suggestion applied"})
    return ResponseContext.not_found("Suggestion not found")


def learning_metric_record(ctx: RequestContext) -> ResponseContext:
    """POST /learning/metric — record metric (operator)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    body = ctx.json_body or {}
    raw_value = body.get("value")
    if raw_value is None:
        return ResponseContext.bad_request("value is required")
    metric = service.record_metric(
        metric_name=body.get("metric_name"),
        value=float(raw_value),
        service_name=body.get("service_name", ""),
        unit=body.get("unit", ""),
        tags=body.get("tags", {}),
    )
    return ResponseContext.created(metric.to_dict())


def learning_insights(ctx: RequestContext) -> ResponseContext:
    """GET /learning/insights — cross-stage insights (viewer)."""
    service = _service()
    if not service:
        return ResponseContext.service_unavailable("Learning service not available")

    insights = service.get_cross_stage_insights()
    return ResponseContext.json(insights)
