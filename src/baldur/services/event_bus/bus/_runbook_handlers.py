"""
Runbook Approval Default Event Handlers

Default handlers for runbook approval lifecycle events:
- APPROVAL_REQUIRED: log + metrics
- APPROVAL_GRANTED: log + metrics
- APPROVAL_REJECTED: log (WARNING) + metrics

Registered by ``register_default_handlers()`` in ``default_handlers.py``.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy singleton — Prometheus counter
# ---------------------------------------------------------------------------
_runbook_handler_counter = None


def _get_runbook_handler_counter():
    global _runbook_handler_counter
    if _runbook_handler_counter is None:
        from baldur.metrics.registry import get_or_create_counter

        _runbook_handler_counter = get_or_create_counter(
            "baldur_runbook_event_handled_total",
            "Total runbook events handled by default handlers",
            ["event_type"],
        )
    return _runbook_handler_counter


# ---------------------------------------------------------------------------
# Approval handlers (NORMAL priority)
# ---------------------------------------------------------------------------


def _on_runbook_approval_required(event: Any) -> None:
    """Handle RUNBOOK_APPROVAL_REQUIRED — log + metrics."""
    data = getattr(event, "data", {}) or {}

    logger.info(
        "event_bus.runbook_approval_required",
        runbook_id=data.get("runbook_id"),
        runbook_name=data.get("runbook_name"),
        execution_id=data.get("execution_id"),
        risk_level=data.get("risk_level"),
    )

    try:
        _get_runbook_handler_counter().labels(event_type="approval_required").inc()
    except Exception:
        pass


def _on_runbook_approval_granted(event: Any) -> None:
    """Handle RUNBOOK_APPROVAL_GRANTED — log + metrics."""
    data = getattr(event, "data", {}) or {}

    logger.info(
        "event_bus.runbook_approval_granted",
        execution_id=data.get("execution_id"),
        approved_by=data.get("approved_by"),
    )

    try:
        _get_runbook_handler_counter().labels(event_type="approval_granted").inc()
    except Exception:
        pass


def _on_runbook_approval_rejected(event: Any) -> None:
    """Handle RUNBOOK_APPROVAL_REJECTED — log (WARNING) + metrics."""
    data = getattr(event, "data", {}) or {}

    logger.warning(
        "event_bus.runbook_approval_rejected",
        execution_id=data.get("execution_id"),
        rejected_by=data.get("rejected_by"),
        reason=data.get("reason"),
    )

    try:
        _get_runbook_handler_counter().labels(event_type="approval_rejected").inc()
    except Exception:
        pass
