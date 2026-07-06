"""
Saga Default Event Handlers

Default handlers for saga lifecycle events:
- TIMED_OUT, COMPENSATION_FAILED: 3-piece set (log + audit + metrics)
- COMPLETED, COMPENSATED: log + metrics

Registered by ``register_default_handlers()`` in ``default_handlers.py``.
"""

from __future__ import annotations

from typing import Any

import structlog

from baldur.audit.helpers import (
    log_saga_compensation_failed_audit,
    log_saga_timeout_audit,
)

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Lazy singleton — Prometheus counter
# ---------------------------------------------------------------------------
_saga_handler_counter = None


def _get_saga_handler_counter():
    global _saga_handler_counter
    if _saga_handler_counter is None:
        from baldur.metrics.registry import get_or_create_counter

        _saga_handler_counter = get_or_create_counter(
            "baldur_saga_event_handled_total",
            "Total saga events handled by default handlers",
            ["status"],
        )
    return _saga_handler_counter


# ---------------------------------------------------------------------------
# Failure handlers (CRITICAL priority)
# ---------------------------------------------------------------------------


def _on_saga_timed_out(event: Any) -> None:
    """Handle SAGA_TIMED_OUT — log + audit + metrics.

    Emitted by the saga orchestrator timeout flow (orphan-saga scan →
    timeout transition → final-event emission).
    """
    from baldur.settings.saga import get_saga_settings

    if not get_saga_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}
    saga_name = data.get("saga_name", "unknown")
    instance_id = data.get("instance_id", "unknown")
    timeout_seconds = data.get("timeout_seconds")

    logger.warning(
        "event_bus.saga_timed_out_handled",
        saga_name=saga_name,
        instance_id=instance_id,
        timeout_seconds=timeout_seconds,
    )

    log_saga_timeout_audit(
        saga_name=saga_name,
        instance_id=instance_id,
        timeout_seconds=timeout_seconds,
    )

    try:
        _get_saga_handler_counter().labels(status="timed_out").inc()
    except Exception:
        pass


def _on_saga_compensation_failed(event: Any) -> None:
    """Handle SAGA_COMPENSATION_FAILED — log + audit + metrics."""
    from baldur.settings.saga import get_saga_settings

    if not get_saga_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}
    saga_name = data.get("saga_name", "unknown")
    instance_id = data.get("instance_id", "unknown")
    failed_steps = data.get("failed_steps", [])

    logger.warning(
        "event_bus.saga_compensation_failed_handled",
        saga_name=saga_name,
        instance_id=instance_id,
        failed_steps=failed_steps,
    )

    log_saga_compensation_failed_audit(
        saga_name=saga_name,
        instance_id=instance_id,
        failed_steps=failed_steps or None,
        error_message=data.get("original_failure_reason"),
    )

    try:
        _get_saga_handler_counter().labels(status="compensation_failed").inc()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Completion handlers (NORMAL priority)
# ---------------------------------------------------------------------------


def _on_saga_completed(event: Any) -> None:
    """Handle SAGA_COMPLETED — log + metrics."""
    from baldur.settings.saga import get_saga_settings

    if not get_saga_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}
    saga_name = data.get("saga_name", "unknown")
    instance_id = data.get("instance_id", "unknown")

    logger.info(
        "event_bus.saga_completed_handled",
        saga_name=saga_name,
        instance_id=instance_id,
    )

    try:
        _get_saga_handler_counter().labels(status="completed").inc()
    except Exception:
        pass


def _on_saga_compensated(event: Any) -> None:
    """Handle SAGA_COMPENSATED — log + metrics."""
    from baldur.settings.saga import get_saga_settings

    if not get_saga_settings().enabled:
        return

    data = getattr(event, "data", {}) or {}
    saga_name = data.get("saga_name", "unknown")
    instance_id = data.get("instance_id", "unknown")

    logger.info(
        "event_bus.saga_compensated_handled",
        saga_name=saga_name,
        instance_id=instance_id,
    )

    try:
        _get_saga_handler_counter().labels(status="compensated").inc()
    except Exception:
        pass
