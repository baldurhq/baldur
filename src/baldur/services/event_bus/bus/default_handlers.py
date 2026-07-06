"""Default event handlers and handler registration for the Baldur Event Bus."""

from __future__ import annotations

import structlog

from baldur.audit.helpers import (
    log_error_budget_blocked_audit,
    log_error_budget_recovered_audit,
    log_error_budget_warning_audit,
)

from .event_types import EventPriority, EventType
from .models import BaldurEvent

logger = structlog.get_logger()

__all__ = [
    "register_default_handlers",
]


# =============================================================================
# Inline Event Handlers
# =============================================================================


def _on_emergency_level_changed(event: BaldurEvent):
    """
    Structured log + operator notification on emergency level change.

    Notification is dispatched only on escalation to LEVEL_2 or LEVEL_3.
    Domain automation (throttle adjustment, auto-tuning, backoff, cache
    invalidation) is owned by the respective subscribers — this handler
    is the operator-facing path.

    Tolerates two payload shapes:
    - Production: EmergencyManager publishes level/previous_level as the
      EmergencyLevel ``.value`` strings ("normal", "level_2", ...) and pre-
      computes ``is_escalation`` using EmergencyLevel-aware comparison
      (``manager.py:1018-1021``). The pre-computed flag is trusted; raw
      ``>`` comparison on the string values would be lexicographic and
      treat "normal" > "level_2" as True, inverting the escalation gate.
    - Unit tests / programmatic emitters: numeric severity (int 0-3) for
      level/previous_level, no ``is_escalation`` field. The handler derives
      it from the numeric ordering.
    """
    level_value = event.data.get("level", 0)
    previous_level_value = event.data.get("previous_level", 0)

    level_severity = _coerce_emergency_severity(level_value)
    previous_severity = _coerce_emergency_severity(previous_level_value)

    pre_computed = event.data.get("is_escalation")
    if pre_computed is None:
        is_escalation = level_severity > previous_severity
    else:
        is_escalation = bool(pre_computed)

    logger.info(
        "event_bus.emergency_level_changed_handled",
        previous_level=previous_level_value,
        event_log_level=level_value,
        is_escalation=is_escalation,
    )

    try:
        _get_emergency_level_event_counter().labels(level=str(level_value)).inc()
    except Exception:
        logger.debug(
            "event_bus.emergency_level_metric_increment_failed", level=level_value
        )

    if is_escalation and level_severity >= 2:
        _notify_emergency_level_escalation(event, level_severity, previous_severity)
    elif previous_severity >= 2 and level_severity < 2:
        # Resolve (stand-down): severity crossed below LEVEL_2 — the exact
        # inverse of the firing gate. One resolve per firing episode:
        # 3->2 stays silent (condition still >= L2); 1->0 stays silent
        # (nothing ever fired below L2). 2->1, 2->0, 3->0 each notify once.
        _notify_emergency_level_resolved(event, level_severity, previous_severity)


def _coerce_emergency_severity(value) -> int:
    """Convert a level field (int severity, EmergencyLevel enum, or string
    ``.value`` like "level_2") to a numeric 0-3 severity. Returns 0 on
    anything unparseable so the escalation gate fails closed."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        from baldur.models.emergency import EmergencyLevel

        if isinstance(value, EmergencyLevel):
            return value.severity
        if isinstance(value, str):
            return EmergencyLevel(value).severity
    except Exception:
        pass
    return 0


def _notify_emergency_level_escalation(
    event: BaldurEvent, level: int, previous_level: int
) -> None:
    """Send operator notification on LEVEL_2 / LEVEL_3 escalation.

    Mirrors _send_postmortem_notification (_cb_handlers.py): graceful
    degradation when baldur_pro / UnifiedNotificationManager is unavailable.
    """
    try:
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
            get_unified_notification_manager,
        )
    except ImportError:
        logger.debug(
            "notification.unified_notification_unavailable",
            event_type="emergency_level_changed",
            level=level,
        )
        return

    priority = (
        NotificationPriority.CRITICAL if level >= 3 else NotificationPriority.HIGH
    )
    reason = event.data.get("reason") or "unspecified"
    activated_by = event.data.get("activated_by") or "system"

    payload = NotificationPayload(
        title=f"Emergency Mode escalated to LEVEL_{level}",
        message=(
            f"Level transition: LEVEL_{previous_level} → LEVEL_{level}\n"
            f"Reason: {reason}\n"
            f"Activated by: {activated_by}"
        ),
        priority=priority,
        category=NotificationCategory.OPERATIONS,
        source="EventHandler.EmergencyLevel",
        metadata={
            "level": level,
            "previous_level": previous_level,
            "reason": reason,
            "activated_by": activated_by,
        },
        dedup_key=f"emergency_level_escalation:{previous_level}->{level}",
    )

    try:
        manager = get_unified_notification_manager()
        result = manager.notify(payload)
        if result.success and not result.suppressed:
            logger.warning(
                "notification.emergency_level_escalation_sent",
                level=level,
                previous_level=previous_level,
            )
        elif result.suppressed:
            logger.debug(
                "notification.emergency_level_escalation_suppressed",
                level=level,
                suppression_reason=result.suppression_reason,
            )
    except Exception as e:
        logger.warning(
            "notification.emergency_level_escalation_failed",
            level=level,
            error=str(e),
        )


def _notify_emergency_level_resolved(
    event: BaldurEvent, level: int, previous_level: int
) -> None:
    """Send an operator stand-down notification on emergency de-escalation.

    Symmetric to _notify_emergency_level_escalation but Slack-only and
    low-urgency: priority LOW (routes to Slack via channel_routing) with
    channels=["slack"] pinned so threshold/level escalation in UNM cannot
    lift the resolve onto a paging channel. Sent inline via the singleton
    manager (deactivate runs on an admin/API thread, gradual recovery on its
    worker thread — both rare), mirroring the escalation path's inline send.
    """
    try:
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
            get_unified_notification_manager,
        )
    except ImportError:
        logger.debug(
            "notification.unified_notification_unavailable",
            event_type="emergency_level_changed",
            level=level,
        )
        return

    level_label = "NORMAL" if level <= 0 else f"LEVEL_{level}"
    reason = event.data.get("reason") or "unspecified"

    payload = NotificationPayload(
        title=f"Emergency Mode stood down to {level_label}",
        message=(
            f"Level transition: LEVEL_{previous_level} → {level_label}\n"
            f"Reason: {reason}"
        ),
        priority=NotificationPriority.LOW,
        category=NotificationCategory.OPERATIONS,
        source="EventHandler.EmergencyLevel",
        channels=["slack"],
        metadata={
            "level": level,
            "previous_level": previous_level,
            "reason": reason,
        },
        dedup_key=f"emergency_level_resolved:{previous_level}->{level}",
    )

    try:
        manager = get_unified_notification_manager()
        result = manager.notify(payload)
        if result.success and not result.suppressed:
            logger.info(
                "notification.emergency_level_resolved_sent",
                level=level,
                previous_level=previous_level,
            )
        elif result.suppressed:
            logger.debug(
                "notification.emergency_level_resolved_suppressed",
                level=level,
                suppression_reason=result.suppression_reason,
            )
    except Exception as e:
        logger.warning(
            "notification.emergency_level_resolved_failed",
            level=level,
            error=str(e),
        )


def _on_error_budget_critical(event: BaldurEvent):
    """
    Log, audit, and invalidate governance cache on error budget CRITICAL threshold.

    Actual automation blocking (chaos experiments, replay) is handled by
    governance checks (governance/checks.py:is_error_budget_blocking).
    This handler provides: structured log, audit trail, governance cache
    invalidation, and Prometheus metric.
    """
    budget_percent = event.data.get("budget_percent", 0)
    threshold = event.data.get("threshold", 10)

    logger.warning(
        "event_bus.error_budget_critical_handled",
        budget_percent=budget_percent,
        threshold=threshold,
    )

    try:
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.governance.get().invalidate_governance_cache()
    except Exception:
        logger.warning("event_bus.governance_cache_invalidation_failed")

    log_error_budget_blocked_audit(
        action="error_budget_critical_event",
        gate_status="CRITICAL",
        error_budget_percent=budget_percent,
        threshold_percent=threshold,
    )

    try:
        _get_error_budget_event_counter().labels(status="critical").inc()
    except Exception:
        logger.debug(
            "event_bus.error_budget_metric_increment_failed", status="critical"
        )


def _on_error_budget_warning(event: BaldurEvent):
    """
    Log, audit, and invalidate governance cache on error budget WARNING threshold.

    Fires on OPEN → WARNING transition when budget drops below warning_threshold_percent.
    """
    budget_percent = event.data.get("budget_percent", 0)
    threshold = event.data.get("threshold", 20)

    logger.warning(
        "event_bus.error_budget_warning_received",
        budget_percent=budget_percent,
        threshold=threshold,
    )

    try:
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.governance.get().invalidate_governance_cache()
    except Exception:
        logger.warning("event_bus.governance_cache_invalidation_failed")

    log_error_budget_warning_audit(budget_percent, threshold)

    try:
        _get_error_budget_event_counter().labels(status="warning").inc()
    except Exception:
        logger.debug("event_bus.error_budget_metric_increment_failed", status="warning")


def _on_error_budget_recovered(event: BaldurEvent):
    """
    Log, audit, and invalidate governance cache on error budget RECOVERED transition.

    Fires on WARNING/BLOCKED → OPEN transition when budget recovers.
    """
    budget_percent = event.data.get("budget_percent", 0)
    threshold = event.data.get("threshold", 20)

    logger.info(
        "event_bus.error_budget_recovered",
        budget_percent=budget_percent,
        threshold=threshold,
    )

    try:
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.governance.get().invalidate_governance_cache()
    except Exception:
        logger.warning("event_bus.governance_cache_invalidation_failed")

    log_error_budget_recovered_audit(budget_percent, threshold)

    try:
        _get_error_budget_event_counter().labels(status="recovered").inc()
    except Exception:
        logger.debug(
            "event_bus.error_budget_metric_increment_failed", status="recovered"
        )


def _on_security_violation_critical(event: BaldurEvent):
    """
    Activate Emergency Mode on critical security violation.

    Triggers LEVEL_2 emergency on TOKEN_FORGED, DATA_TAMPERED, INJECTION_ATTEMPT.
    Security service follows "NEVER self-heal" — this handler bridges the gap
    via event bus, not direct coupling.
    """
    violation_type = event.data.get("violation_type", "unknown")
    incident_id = event.data.get("incident_id", "unknown")
    reason = f"Security violation: {violation_type} (incident #{incident_id})"

    # 1. Core state change — failure here is critical (emergency not activated)
    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.models.emergency import EmergencyLevel

        manager = ProviderRegistry.emergency_manager.safe_get()
        if manager is None:
            raise RuntimeError("baldur_pro EmergencyManager not registered")
        manager.activate_auto(level=EmergencyLevel.LEVEL_2, reason=reason)
    except Exception as e:
        logger.exception(
            "event_bus.security_violation_emergency_failed",
            error=str(e),
        )
        return

    # 2. Side-effect — failure is non-critical (cache expires naturally via 30s TTL)
    # Note: activate_auto() internally emits EMERGENCY_LEVEL_CHANGED,
    # which may also trigger cache invalidation via other handlers.
    # Duplicate invalidation is harmless (dict.clear() is idempotent).
    try:
        from baldur.factory.registry import ProviderRegistry

        ProviderRegistry.governance.get().invalidate_governance_cache()
    except Exception:
        logger.warning("event_bus.governance_cache_invalidation_failed")

    logger.warning(
        "event_bus.security_violation_emergency_activated",
        violation_type=violation_type,
        incident_id=incident_id,
        emergency_level=EmergencyLevel.LEVEL_2.value,
        reason=reason,
    )


# =============================================================================
# Metrics Helpers
# =============================================================================

_error_budget_event_counter = None
_emergency_level_event_counter = None


def _get_error_budget_event_counter():
    """Lazy singleton for error budget event Prometheus counter."""
    global _error_budget_event_counter
    if _error_budget_event_counter is None:
        from baldur.metrics.registry import get_or_create_counter

        _error_budget_event_counter = get_or_create_counter(
            "baldur_error_budget_event_handled_total",
            "Total error budget events handled by default handlers",
            ["status"],
        )
    return _error_budget_event_counter


def _get_emergency_level_event_counter():
    """Lazy singleton for emergency level changed event Prometheus counter."""
    global _emergency_level_event_counter
    if _emergency_level_event_counter is None:
        from baldur.metrics.registry import get_or_create_counter

        _emergency_level_event_counter = get_or_create_counter(
            "baldur_emergency_level_changed_total",
            "Total emergency level transitions handled by default handlers",
            ["level"],
        )
    return _emergency_level_event_counter


def _on_scheduled_event_started(event: BaldurEvent):
    """
    ML context injection handler on scheduled event start.

    When integrated with ML (SpikeClassifier, PredictiveForecaster),
    this handler propagates context.scheduled_event=True.
    Currently a logging-only stub; actual logic will be added after ML implementation.
    """
    event_id = event.data.get("event_id", "unknown")
    name = event.data.get("name", "")
    logger.info(
        "event_handler.scheduled_event_started",
        event_id=event_id,
        name=name,
        expected_rps_multiplier=event.data.get("expected_rps_multiplier"),
        tags=event.data.get("tags", []),
    )


def _on_scheduled_event_ended(event: BaldurEvent):
    """
    ML context release handler on scheduled event end.

    When integrated with ML, this handler clears the context.scheduled_event flag.
    Currently a logging-only stub; actual logic will be added after ML implementation.
    """
    event_id = event.data.get("event_id", "unknown")
    logger.info(
        "event_handler.scheduled_event_ended",
        event_id=event_id,
    )


# =============================================================================
# Handler Sub-Module Imports (lazy to avoid circular imports)
# =============================================================================


def register_default_handlers():  # noqa: PLR0915
    """
    Register default event handlers.

    Called during application initialization.
    """
    from .convenience import get_event_bus

    bus = get_event_bus()

    if bus._handlers_registered:
        return

    # --- Sub-module handler imports ---
    from ._cb_handlers import (
        _on_circuit_breaker_closed,
        _on_circuit_breaker_closed_notify,
        _on_circuit_breaker_closed_postmortem,
        _on_circuit_breaker_opened_notify,
        _on_circuit_breaker_opened_snapshot,
    )
    from ._emergency_postmortem import (
        _on_emergency_recovery_completed_postmortem,
    )
    from ._throttle_handlers import (
        _on_emergency_level_changed_throttle,
        _on_kill_switch_activated_throttle,
    )

    # Emergency events — dispatched fire-and-forget so the publisher (request)
    # thread is never blocked on the operator-notification path. The
    # EmergencyManager emits EMERGENCY_LEVEL_CHANGED while holding its
    # _state_lock; this handler calls back into get_current_level() (via the
    # UnifiedNotificationManager priority resolver) which re-acquires that same
    # lock from the dispatch thread. With await_result=True the publisher would
    # join this handler under the lock it needs — a cross-thread deadlock broken
    # only by the 5s handler_timeout, stalling every de-escalation (Release /
    # recovery-to-NORMAL). Fire-and-forget mirrors the CB notify handlers below.
    bus.subscribe(
        EventType.EMERGENCY_LEVEL_CHANGED,
        _on_emergency_level_changed,
        priority=EventPriority.HIGH,
        await_result=False,
    )

    # Error Budget events
    bus.subscribe(
        EventType.ERROR_BUDGET_CRITICAL,
        _on_error_budget_critical,
        priority=EventPriority.CRITICAL,
    )

    bus.subscribe(
        EventType.ERROR_BUDGET_WARNING,
        _on_error_budget_warning,
        priority=EventPriority.HIGH,
    )

    bus.subscribe(
        EventType.ERROR_BUDGET_RECOVERED,
        _on_error_budget_recovered,
        priority=EventPriority.NORMAL,
    )

    # Security events
    bus.subscribe(
        EventType.SECURITY_VIOLATION_CRITICAL,
        _on_security_violation_critical,
        priority=EventPriority.CRITICAL,
    )

    # Circuit Breaker events

    # Integrity gate (CRITICAL: runs before Replay)
    try:
        from baldur.services.event_bus.integrity_gate import (
            on_circuit_breaker_closed_integrity_gate,
        )

        bus.subscribe(
            EventType.CIRCUIT_BREAKER_CLOSED,
            on_circuit_breaker_closed_integrity_gate,
            priority=EventPriority.CRITICAL,
        )
    except ImportError:
        pass  # Ignore if integrity_gate module is not installed

    # Replay trigger (best-effort side effect) — dispatched fire-and-forget
    # so the publisher (request) thread is never blocked on the Celery
    # delegation, even under inline/eager task execution. The CRITICAL
    # integrity gate above stays awaited, so its event.data[INTEGRITY_FAILED_KEY]
    # write is visible to this handler before it is submitted.
    bus.subscribe(
        EventType.CIRCUIT_BREAKER_CLOSED,
        _on_circuit_breaker_closed,
        priority=EventPriority.NORMAL,
        await_result=False,
    )

    # Circuit Breaker auto post-mortem handler (low priority, fire-and-forget)
    bus.subscribe(
        EventType.CIRCUIT_BREAKER_CLOSED,
        _on_circuit_breaker_closed_postmortem,
        priority=EventPriority.LOW,
        await_result=False,
    )

    # Circuit Breaker recovery (stand-down) notification handler — mirror of
    # the OPEN notify handler; enqueues the Slack resolve via Celery (HIGH so
    # it runs ahead of replay/post-mortem in the handler order). Fire-and-forget
    # so a slow/inline webhook POST never blocks the publisher thread.
    bus.subscribe(
        EventType.CIRCUIT_BREAKER_CLOSED,
        _on_circuit_breaker_closed_notify,
        priority=EventPriority.HIGH,
        await_result=False,
    )

    # Circuit Breaker notification handler (fire-and-forget)
    bus.subscribe(
        EventType.CIRCUIT_BREAKER_OPENED,
        _on_circuit_breaker_opened_notify,
        priority=EventPriority.HIGH,
        await_result=False,
    )

    # Circuit Breaker OPEN snapshot save handler (fire-and-forget)
    bus.subscribe(
        EventType.CIRCUIT_BREAKER_OPENED,
        _on_circuit_breaker_opened_snapshot,
        priority=EventPriority.NORMAL,
        await_result=False,
    )

    # Emergency Recovery completed auto postmortem handler (low priority)
    bus.subscribe(
        EventType.EMERGENCY_RECOVERY_COMPLETED,
        _on_emergency_recovery_completed_postmortem,
        priority=EventPriority.LOW,
    )

    # =========================================================================
    # Throttle Event Handlers
    # =========================================================================

    bus.subscribe(
        EventType.EMERGENCY_LEVEL_CHANGED,
        _on_emergency_level_changed_throttle,
        priority=EventPriority.HIGH,
    )

    # CB handlers (OPENED/CLOSED/HALF_OPENED) and EB duplicate handlers
    # (CRITICAL/RECOVERED) moved to AdaptiveThrottle mixins:
    # - CircuitBreakerHandlerMixin (_circuit_breaker.py)
    # - ErrorBudgetHandlerMixin (_error_budget.py)

    bus.subscribe(
        EventType.KILL_SWITCH_ACTIVATED,
        _on_kill_switch_activated_throttle,
        priority=EventPriority.CRITICAL,
    )

    # =========================================================================
    # Capacity Reservation Event Handlers (ML integration stub)
    # =========================================================================

    bus.subscribe(
        EventType.SCHEDULED_EVENT_STARTED,
        _on_scheduled_event_started,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.SCHEDULED_EVENT_ENDED,
        _on_scheduled_event_ended,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # DLQ Replay Event Handlers (logging only — metrics via MetricEventHandler)
    # =========================================================================
    from ._replay_handlers import (
        _on_dlq_replay_batch_completed,
        _on_dlq_replay_blocked,
        _on_dlq_replay_completed,
        _on_dlq_replay_failed,
    )

    bus.subscribe(
        EventType.DLQ_REPLAY_COMPLETED,
        _on_dlq_replay_completed,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.DLQ_REPLAY_FAILED,
        _on_dlq_replay_failed,
        priority=EventPriority.HIGH,
    )

    bus.subscribe(
        EventType.DLQ_REPLAY_BATCH_COMPLETED,
        _on_dlq_replay_batch_completed,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.DLQ_REPLAY_BLOCKED,
        _on_dlq_replay_blocked,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Saga Event Handlers
    # =========================================================================
    from ._saga_handlers import (
        _on_saga_compensated,
        _on_saga_compensation_failed,
        _on_saga_completed,
        _on_saga_timed_out,
    )

    bus.subscribe(
        EventType.SAGA_TIMED_OUT,
        _on_saga_timed_out,
        priority=EventPriority.CRITICAL,
    )

    bus.subscribe(
        EventType.SAGA_COMPENSATION_FAILED,
        _on_saga_compensation_failed,
        priority=EventPriority.CRITICAL,
    )

    bus.subscribe(
        EventType.SAGA_COMPLETED,
        _on_saga_completed,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.SAGA_COMPENSATED,
        _on_saga_compensated,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Notification Event Handlers
    # =========================================================================
    from ._notification_handlers import _on_notification_delivery_failed

    bus.subscribe(
        EventType.NOTIFICATION_DELIVERY_FAILED,
        _on_notification_delivery_failed,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Runbook Approval Event Handlers
    # =========================================================================
    from ._runbook_handlers import (
        _on_runbook_approval_granted,
        _on_runbook_approval_rejected,
        _on_runbook_approval_required,
    )

    bus.subscribe(
        EventType.RUNBOOK_APPROVAL_REQUIRED,
        _on_runbook_approval_required,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.RUNBOOK_APPROVAL_GRANTED,
        _on_runbook_approval_granted,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.RUNBOOK_APPROVAL_REJECTED,
        _on_runbook_approval_rejected,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Chaos Event Handlers
    # =========================================================================
    from ._chaos_handlers import (
        _on_chaos_experiment_blocked,
        _on_chaos_experiment_started,
        _on_chaos_experiment_stopped,
    )

    bus.subscribe(
        EventType.CHAOS_EXPERIMENT_BLOCKED,
        _on_chaos_experiment_blocked,
        priority=EventPriority.HIGH,
    )

    bus.subscribe(
        EventType.CHAOS_EXPERIMENT_STARTED,
        _on_chaos_experiment_started,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.CHAOS_EXPERIMENT_STOPPED,
        _on_chaos_experiment_stopped,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Learning Event Handlers
    # =========================================================================
    from ._learning_handlers import (
        _on_learning_manual_only_activated,
        _on_learning_manual_only_deactivated,
        _on_learning_parameter_blacklisted,
        _on_learning_pattern_detected,
    )

    bus.subscribe(
        EventType.LEARNING_PARAMETER_BLACKLISTED,
        _on_learning_parameter_blacklisted,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.LEARNING_PATTERN_DETECTED,
        _on_learning_pattern_detected,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.LEARNING_MANUAL_ONLY_ACTIVATED,
        _on_learning_manual_only_activated,
        priority=EventPriority.NORMAL,
    )

    bus.subscribe(
        EventType.LEARNING_MANUAL_ONLY_DEACTIVATED,
        _on_learning_manual_only_deactivated,
        priority=EventPriority.NORMAL,
    )

    # =========================================================================
    # Daily Report Event Handlers
    # =========================================================================
    from ._daily_report_handlers import (
        _on_daily_report_send_failed,
    )

    bus.subscribe(
        EventType.DAILY_REPORT_SEND_FAILED,
        _on_daily_report_send_failed,
        priority=EventPriority.NORMAL,
    )

    bus._handlers_registered = True
    logger.info("event_bus.default_handlers_registered")
