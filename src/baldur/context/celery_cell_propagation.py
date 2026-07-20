"""
cell_id tagging for Celery tasks — hybrid three-stage propagation.

Propagation priority:
1. Inherit the cell_id from the HTTP context (ContextVar)
2. Extract service_name/namespace/domain from kwargs
3. task_name fallback (last resort)

Existing propagation patterns for reference:
- context/celery_propagation.py: CausationContext before_task_publish
  auto-injection
- adapters/celery/signal_hooks.py: the on_before_task_publish handler
"""

from __future__ import annotations

from typing import Any

import structlog
from celery.signals import before_task_publish

logger = structlog.get_logger()

# Routing keys to extract from kwargs (in priority order)
CELERY_ROUTING_KEYS = [
    "service_name",  # CB, postmortem tasks
    "namespace",  # Recovery, incident tasks
    "domain",  # DLQ replay tasks
    "user_id",  # CB force open/close tasks
]


def _extract_routing_key(kwargs: dict[str, Any]) -> tuple[str, str] | None:
    """
    Extract a routing key from the Celery kwargs.

    Args:
        kwargs: Task kwargs

    Returns:
        (key_name, value), or None

    Note:
        Every Celery task in the current codebase uses flat 1-depth kwargs, so
        a plain dict.get() is sufficient. If nested structures are introduced
        later, only this function needs to change.
    """
    for key in CELERY_ROUTING_KEYS:
        value = kwargs.get(key)
        if value is not None:
            return (key, str(value))
    return None


@before_task_publish.connect
def add_cell_id_to_task(
    sender: str | None = None,
    headers: dict | None = None,
    body: Any = None,
    **kwargs,
) -> None:
    """
    Insert cell_id when a task is published — the hybrid three-stage flow.

    Same signal pattern as the existing on_before_task_publish in
    celery_propagation.py. An existing cell_id is never overwritten.
    """
    if headers is None:
        return

    # Skip when cell_id was already set explicitly
    if headers.get("cell_id"):
        return

    try:
        from baldur.settings.cell_topology import get_cell_topology_settings

        settings = get_cell_topology_settings()
        if not settings.enabled or not settings.tagging_enabled:
            return

        # ── Priority 1: inherit the cell_id from the HTTP context (ContextVar) ──
        from baldur.context.cell_context import get_current_cell_id

        current_cell = get_current_cell_id()
        if current_cell:
            headers["cell_id"] = current_cell
            return

        # ── Priority 2: extract a routing key from kwargs ──
        from baldur.services.cell_topology import get_cell_registry

        registry = get_cell_registry()

        # body structure: [args, kwargs, embed] (Celery protocol v2)
        task_kwargs: dict[str, Any] = {}
        if (
            body
            and isinstance(body, (list, tuple))
            and len(body) > 1
            and isinstance(body[1], dict)
        ):
            task_kwargs = body[1]

        routing = _extract_routing_key(task_kwargs)
        if routing:
            key_name, value = routing
            headers["cell_id"] = registry.get_cell_for_key(f"{key_name}:{value}")
            return

        # ── Priority 3: task_name fallback ──
        task_name = headers.get("task", "unknown")
        headers["cell_id"] = registry.get_cell_for_key(f"task:{task_name}")

    except Exception:
        pass  # Ignore tagging failures — preserve existing behaviour (fail-open)


# extract_cell_id_on_prerun / clear_cell_id_on_postrun were folded into
# restore_all_task_context / cleanup_all_task_context in
# context/celery_context_utils.py. The wrappers below are kept for backward
# compatibility.


def extract_cell_id_on_prerun(task: Any = None, **kwargs) -> None:
    """Set cell_id on the ContextVar at task_prerun (compatibility wrapper)."""
    if task is None:
        return
    try:
        from baldur.context.cell_context import _current_cell_id

        request = getattr(task, "request", None)
        if request is None:
            return
        cell_id = request.get("cell_id") if hasattr(request, "get") else None
        if cell_id:
            token = _current_cell_id.set(cell_id)
            task._cell_id_token = token
    except Exception:
        pass


def clear_cell_id_on_postrun(task: Any = None, **kwargs) -> None:
    """Reset the cell_id ContextVar at task_postrun (compatibility wrapper)."""
    if task is None:
        return
    try:
        from baldur.context.cell_context import _current_cell_id

        token = getattr(task, "_cell_id_token", None)
        if token is not None:
            _current_cell_id.reset(token)
    except Exception:
        pass
