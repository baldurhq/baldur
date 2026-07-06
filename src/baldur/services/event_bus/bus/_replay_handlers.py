"""
DLQ Replay event handlers — structured logging only.

Metrics are recorded via MetricEventHandler direct calls in the
replay service (not via EventBus handlers), preventing double-counting.
These handlers solve C3 ("0 handlers registered") by providing
structured log output for replay events.
"""

from __future__ import annotations

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _on_dlq_replay_completed(event: BaldurEvent) -> None:
    """Structured log for per-item replay completion."""
    logger.info(
        "event_handler.dlq_replay_completed",
        dlq_id=event.data.get("dlq_id"),
        domain=event.data.get("domain"),
        success=event.data.get("success"),
        replay_attempt=event.data.get("replay_attempt"),
    )


def _on_dlq_replay_failed(event: BaldurEvent) -> None:
    """Warning log for replay handler crash."""
    logger.warning(
        "event_handler.dlq_replay_failed",
        dlq_id=event.data.get("dlq_id"),
        domain=event.data.get("domain"),
        error_type=event.data.get("error_type"),
        error_message=event.data.get("error_message"),
        replay_attempt=event.data.get("replay_attempt"),
    )


def _on_dlq_replay_batch_completed(event: BaldurEvent) -> None:
    """Structured log for batch replay completion."""
    logger.info(
        "event_handler.dlq_replay_batch_completed",
        domain=event.data.get("domain"),
        total=event.data.get("total"),
        success_count=event.data.get("success_count"),
        failed_count=event.data.get("failed_count"),
    )


def _on_dlq_replay_blocked(event: BaldurEvent) -> None:
    """Warning log for governance-blocked replay."""
    logger.warning(
        "event_handler.dlq_replay_blocked",
        dlq_id=event.data.get("dlq_id"),
        domain=event.data.get("domain"),
        block_reason=event.data.get("block_reason"),
        block_message=event.data.get("block_message"),
    )
