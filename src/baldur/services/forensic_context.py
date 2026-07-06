"""
Forensic Context Capture Service.

Captures forensic context for failed tasks to aid in post-incident
investigation and root cause analysis.

Usage:
    from baldur.services.forensic_context import capture_forensic_context

    context = capture_forensic_context(
        task_id="abc-123",
        task_name="process_payment",
    )
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.core.timezone import now

logger = structlog.get_logger()


def capture_forensic_context(
    *,
    task_id: str,
    task_name: str,
    order: Any = None,
    payment: Any = None,
    user: Any = None,
    request: Any = None,
) -> dict[str, Any] | None:
    """
    Capture forensic context for a failed task.

    Collects available contextual information at the time of failure
    for later forensic analysis. All entity parameters are optional
    and handled gracefully if unavailable.

    Args:
        task_id: Celery/task queue task identifier.
        task_name: Registered task name.
        order: Optional order entity for context.
        payment: Optional payment entity for context.
        user: Optional user entity for context.
        request: Optional HTTP request for context.

    Returns:
        Dictionary with captured forensic context, or None on failure.
    """
    try:
        context: dict[str, Any] = {
            "task_id": task_id,
            "task_name": task_name,
            "captured_at": now().isoformat(),
        }

        # Capture order context if available
        if order is not None:
            context["order"] = _safe_extract_entity(order, "order")

        # Capture payment context if available
        if payment is not None:
            context["payment"] = _safe_extract_entity(payment, "payment")

        # Capture user context if available
        if user is not None:
            context["user"] = _safe_extract_entity(user, "user")

        # Capture request context if available
        if request is not None:
            context["request"] = _safe_extract_request(request)

        logger.debug(
            "forensic_context.captured",
            task_id=task_id,
            task_name=task_name,
        )

        return context

    except Exception as e:
        logger.debug(
            "forensic_context.capture_failed",
            task_id=task_id,
            error=str(e),
        )
        return None


def _safe_extract_entity(entity: Any, entity_type: str) -> dict[str, Any]:
    """
    Safely extract identifying fields from a domain entity.

    Args:
        entity: Domain entity object.
        entity_type: Type label for logging.

    Returns:
        Dictionary with extracted entity fields.
    """
    result: dict[str, Any] = {"type": entity_type}

    for attr in ("id", "pk", "status", "created_at", "updated_at"):
        value = getattr(entity, attr, None)
        if value is not None:
            try:
                # Convert non-serializable types to string
                if hasattr(value, "isoformat"):
                    result[attr] = value.isoformat()
                else:
                    result[attr] = value
            except Exception:
                result[attr] = str(value)

    return result


def _safe_extract_request(request: Any) -> dict[str, Any]:
    """
    Safely extract context from an HTTP request object.

    Args:
        request: HTTP request object (e.g., Django HttpRequest).

    Returns:
        Dictionary with extracted request fields.
    """
    result: dict[str, Any] = {}

    # Standard request attributes
    for attr in ("method", "path", "content_type"):
        value = getattr(request, attr, None)
        if value is not None:
            result[attr] = str(value)

    # User info (avoid PII leakage - only capture user ID)
    user = getattr(request, "user", None)
    if user is not None:
        user_id = getattr(user, "id", None) or getattr(user, "pk", None)
        if user_id is not None:
            result["user_id"] = user_id
        result["is_authenticated"] = getattr(user, "is_authenticated", False)

    return result


__all__ = [
    "capture_forensic_context",
]
