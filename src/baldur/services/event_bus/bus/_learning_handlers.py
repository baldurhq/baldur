"""
Learning event handlers — structured logging + metrics.

Handles Learning subsystem events:
- LEARNING_PARAMETER_BLACKLISTED
- LEARNING_PATTERN_DETECTED
- LEARNING_MANUAL_ONLY_ACTIVATED
- LEARNING_MANUAL_ONLY_DEACTIVATED
"""

from __future__ import annotations

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _get_learning_recorder():
    """Lazy accessor for LearningMetricRecorder (graceful if metrics unavailable)."""
    try:
        from baldur.metrics.prometheus import get_metrics

        metrics = get_metrics()
        if metrics._initialized:
            return metrics.learning
    except Exception:
        pass
    return None


def _on_learning_parameter_blacklisted(event: BaldurEvent) -> None:
    """Handle LEARNING_PARAMETER_BLACKLISTED event."""
    logger.info(
        "learning.parameter_blacklisted",
        pattern_key=event.data.get("pattern_key"),
        blocked_values=event.data.get("blocked_values"),
        reason=event.data.get("reason"),
    )
    try:
        recorder = _get_learning_recorder()
        if recorder:
            recorder.record_blacklisted(
                module=event.data.get("module", "unknown"),
                reason=event.data.get("reason", "unknown"),
            )
    except Exception:
        logger.debug("event_handler.learning_metric_record_failed")


def _on_learning_pattern_detected(event: BaldurEvent) -> None:
    """Handle LEARNING_PATTERN_DETECTED event."""
    logger.info(
        "learning.pattern_detected",
        rule_name=event.data.get("rule_name"),
        pattern_type=event.data.get("pattern_type"),
    )
    try:
        recorder = _get_learning_recorder()
        if recorder:
            recorder.record_pattern(
                pattern_type=event.data.get("pattern_type", "unknown"),
                confidence=event.data.get("confidence", 0.0),
            )
    except Exception:
        logger.debug("event_handler.learning_metric_record_failed")


def _on_learning_manual_only_activated(event: BaldurEvent) -> None:
    """Handle LEARNING_MANUAL_ONLY_ACTIVATED event."""
    logger.info(
        "learning.manual_only_activated",
        module=event.data.get("module"),
    )
    try:
        recorder = _get_learning_recorder()
        if recorder:
            recorder.set_manual_only(
                module=event.data.get("module", "unknown"), enabled=True
            )
    except Exception:
        logger.debug("event_handler.learning_metric_record_failed")


def _on_learning_manual_only_deactivated(event: BaldurEvent) -> None:
    """Handle LEARNING_MANUAL_ONLY_DEACTIVATED event."""
    logger.info(
        "learning.manual_only_deactivated",
        module=event.data.get("module"),
    )
    try:
        recorder = _get_learning_recorder()
        if recorder:
            recorder.set_manual_only(
                module=event.data.get("module", "unknown"), enabled=False
            )
    except Exception:
        logger.debug("event_handler.learning_metric_record_failed")
