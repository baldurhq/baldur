"""
Chaos Default Event Handlers

Default handlers for chaos experiment lifecycle events:
- BLOCKED: log (WARNING) + metrics
- STARTED: log (DEBUG) + metrics
- STOPPED: log (INFO if success or dry_run, WARNING otherwise) + metrics

Registered by ``register_default_handlers()`` in ``default_handlers.py``.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Status label cardinality guard — prevent TSDB explosion from unexpected values
# ---------------------------------------------------------------------------
_KNOWN_STATUSES = frozenset(
    {
        "started",
        "completed",
        "failed",
        "aborted",
        "skipped",
        "blocked",
        "rolled_back",
        "recovery_monitoring",
        "error",
    }
)

# ---------------------------------------------------------------------------
# Lazy singleton — Prometheus counter
# ---------------------------------------------------------------------------
_chaos_handler_counter = None


def _get_chaos_handler_counter():
    global _chaos_handler_counter
    if _chaos_handler_counter is None:
        from baldur.metrics.registry import get_or_create_counter

        _chaos_handler_counter = get_or_create_counter(
            "baldur_chaos_event_handled_total",
            "Total chaos events handled by default handlers",
            ["status"],
        )
    return _chaos_handler_counter


# ---------------------------------------------------------------------------
# BLOCKED handler (HIGH priority)
# ---------------------------------------------------------------------------


def _on_chaos_experiment_blocked(event: Any) -> None:
    """Handle CHAOS_EXPERIMENT_BLOCKED — log + metrics."""
    from baldur.settings.chaos import get_chaos_settings

    if not get_chaos_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}

    logger.warning(
        "event_bus.chaos_experiment_blocked",
        schedule_id=data.get("schedule_id"),
        experiment_id=data.get("experiment_id"),
        experiment_type=data.get("experiment_type"),
        target_service=data.get("target_service"),
        force=data.get("force", False),
        block_reason=data.get("block_reason"),
        block_status=data.get("block_status"),
    )

    try:
        _get_chaos_handler_counter().labels(status="blocked").inc()
    except Exception:
        logger.debug("event_bus.chaos_metrics_increment_failed", status="blocked")


# ---------------------------------------------------------------------------
# STARTED handler (NORMAL priority)
# ---------------------------------------------------------------------------


def _on_chaos_experiment_started(event: Any) -> None:
    """Handle CHAOS_EXPERIMENT_STARTED — log + metrics."""
    from baldur.settings.chaos import get_chaos_settings

    if not get_chaos_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}

    logger.debug(
        "event_bus.chaos_experiment_started",
        schedule_id=data.get("schedule_id"),
        experiment_id=data.get("experiment_id"),
        experiment_type=data.get("experiment_type"),
        target_service=data.get("target_service"),
        force=data.get("force", False),
        dry_run=data.get("dry_run", False),
    )

    try:
        _get_chaos_handler_counter().labels(status="started").inc()
    except Exception:
        logger.debug("event_bus.chaos_metrics_increment_failed", status="started")


# ---------------------------------------------------------------------------
# STOPPED handler (NORMAL priority)
# ---------------------------------------------------------------------------


def _on_chaos_experiment_stopped(event: Any) -> None:
    """Handle CHAOS_EXPERIMENT_STOPPED — log + metrics."""
    from baldur.settings.chaos import get_chaos_settings

    if not get_chaos_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}
    raw_status = data.get("status", "unknown")
    status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
    success = data.get("success", False)

    log_fn = logger.info if (success or data.get("dry_run", False)) else logger.warning
    log_fn(
        "event_bus.chaos_experiment_stopped",
        schedule_id=data.get("schedule_id"),
        experiment_id=data.get("experiment_id"),
        experiment_type=data.get("experiment_type"),
        target_service=data.get("target_service"),
        status=status,
        duration_seconds=data.get("duration_seconds"),
        success=success,
        force=data.get("force", False),
        dry_run=data.get("dry_run", False),
        error_message=data.get("error_message"),
    )

    try:
        _get_chaos_handler_counter().labels(status=status).inc()
    except Exception:
        logger.debug("event_bus.chaos_metrics_increment_failed", status=status)
