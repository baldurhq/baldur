"""
Automatic Celery causation context propagation.

Automatically propagates causality information originating from an API request
into Celery tasks.

Features:
    - before_task_publish: inject causation headers when a task is published
    - task_prerun: restore causation at task start (auto-creates a system
      cascade when none is set)
    - task_postrun: clean up causation at task end

Usage:
    # Initialize from the Celery app
    from baldur.context.celery_propagation import setup_celery_causation_propagation
    setup_celery_causation_propagation()

    # Or it is called automatically by setup_baldur_signals() in signal_hooks.py

Data flow:
    API Request → ExceptionHandler → CausationContext.start_cascade()
           ↓
    before_task_publish → causation info injected into the headers
           ↓
    Celery Task → task_prerun → CausationContext restored (depth incremented)
           ↓
    task_postrun → CausationContext cleaned up
"""

from __future__ import annotations

from typing import Any

import structlog
from celery.signals import before_task_publish

from baldur.context.causation_context import (
    CELERY_HEADER_CASCADE_ID,
    CausationContext,
    CausationInfo,
    _current_causation,
    get_causation_for_celery,
)
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# before_task_publish signal handler
# =============================================================================


_before_task_publish_connected = False


@before_task_publish.connect
def on_before_task_publish(
    sender: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
    **kwargs,
) -> None:
    """
    Inject causation headers automatically before a Celery task is published.

    When a CausationContext is set, it is included in the headers
    automatically. Developers do not need to pass
    headers=get_causation_for_celery() by hand.

    Args:
        sender: Task name
        body: Task message body
        headers: Task message headers (mutable)
    """
    if headers is None:
        return

    # Do not overwrite existing causation headers (explicit setting wins)
    if headers.get(CELERY_HEADER_CASCADE_ID):
        logger.debug(
            "causation_propagation.causation_headers_already_set",
            sender=sender,
        )
        return

    # Build the headers from the current CausationContext
    causation_headers = get_causation_for_celery()

    if causation_headers:
        headers.update(causation_headers)
        logger.debug(
            "causation_propagation.injected_causation_headers_task",
            sender=sender,
            causation_headers=causation_headers.get(CELERY_HEADER_CASCADE_ID),
        )


# =============================================================================
# Auto-creation of a system-initiated cascade (task_prerun helper)
# =============================================================================


def ensure_causation_context_for_task(
    task_name: str,
    task_id: str,
) -> Any | None:
    """
    Guarantee a CausationContext at Celery task start.

    When no causation was propagated through the call chain, a system cascade
    is created automatically. In X-Test-Mode the XTC- prefix is added
    automatically.

    Args:
        task_name: Task name
        task_id: Task ID

    Returns:
        The causation ContextVar token that was set (for cleanup)
    """
    # Leave it alone when it is already set
    if CausationContext.is_set():
        return None

    # Create a system cascade (Celery Beat, standalone runs, etc.)
    import uuid

    from baldur.context.causation_context import _get_xtest_id_prefix

    # Determine the source: check whether this is a scheduler task
    source = _detect_task_source(task_name)

    # Apply the XTC- prefix in X-Test-Mode
    prefix = _get_xtest_id_prefix()
    system_event_id = f"{prefix}SYSTEM_ROOT_{source}_{uuid.uuid4().hex[:8]}"
    cascade_id = f"{prefix}cascade-{uuid.uuid4().hex[:12]}"

    info = CausationInfo(
        cascade_id=cascade_id,
        parent_event_id=system_event_id,
        chain_depth=0,
        namespace="global",
        metadata={
            "system_source": source,
            "auto_generated": True,
            "task_name": task_name,
            "task_id": task_id,
            "created_at": utc_now().isoformat(),
        },
    )

    token = _current_causation.set(info)

    logger.debug(
        "causation_propagation.auto_created_system_cascade",
        source=source,
        cascade_id=cascade_id,
        task_name=task_name,
    )

    return token


def _detect_task_source(task_name: str) -> str:
    """
    Infer the source type from the task name.

    Args:
        task_name: Celery task name

    Returns:
        Source string (celery_beat, management_cmd, worker, etc.)
    """
    task_name_lower = task_name.lower()

    # Scheduler-related patterns
    if any(pattern in task_name_lower for pattern in ["beat", "schedule", "periodic"]):
        return "celery_beat"

    # Management-command-related patterns
    if any(pattern in task_name_lower for pattern in ["manage", "command", "admin"]):
        return "management_cmd"

    # Cron/scheduler patterns
    if any(pattern in task_name_lower for pattern in ["cron", "cleanup", "expire"]):
        return "scheduler"

    # Default
    return "worker"


# =============================================================================
# Setup function
# =============================================================================


def setup_celery_causation_propagation() -> None:
    """
    Enable automatic Celery causation propagation.

    Connects the before_task_publish signal so that the current
    CausationContext is included in the headers of every published task.

    Usage:
        from baldur.context.celery_propagation import setup_celery_causation_propagation
        setup_celery_causation_propagation()

    Note:
        Called automatically by setup_baldur_signals() in signal_hooks.py.
    """
    global _before_task_publish_connected

    if _before_task_publish_connected:
        logger.debug("causation_propagation.already_connected")
        return

    # before_task_publish is already wired by the @before_task_publish.connect
    # decorator; here we only record the connection state.
    _before_task_publish_connected = True

    logger.info("causation_propagation.celery_causation_propagation_enabled")


__all__ = [
    "setup_celery_causation_propagation",
    "ensure_causation_context_for_task",
    "on_before_task_publish",
]
