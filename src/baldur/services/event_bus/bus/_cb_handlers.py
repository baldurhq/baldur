"""
Circuit Breaker notification and Postmortem handlers.

This module is an internal implementation of the baldur.services.event_bus.bus package.
"""

from __future__ import annotations

import structlog

from . import BaldurEvent

logger = structlog.get_logger()


def _on_circuit_breaker_opened_notify(event: BaldurEvent) -> None:
    """
    Deliver the CB OPEN notification — Celery task when available, else inline.

    Notification sending that includes network I/O (e.g. Slack Webhook HTTP
    calls) is delegated to a Celery task via ``.delay()``. The publisher
    (request) thread is never blocked on the task body because this handler is
    subscribed fire-and-forget (``await_result=False``) — the EventBus
    dispatches it without awaiting the result, which holds even when the task
    runs inline (``task_always_eager`` / no broker).

    On a core-only OSS install without the ``celery`` extra, the task import
    raises ``ImportError``; instead of skipping, the handler delivers the OSS
    Slack push synchronously through the shared wrapper. The POST is bounded
    (``safe_urlopen`` timeout) and fail-open (never raises), and the default
    ``async_pool`` dispatch runs this handler off the request thread — so the
    inline POST does not block app traffic. When Celery IS importable, the
    task body's own ``ImportError`` branch owns OSS delivery, so this fallback
    is unreachable and exactly one delivery happens.
    """
    try:
        try:
            from baldur.adapters.celery.tasks import send_cb_open_notification

            send_cb_open_notification.delay(
                service_name=event.data.get("service_name", "unknown"),
                timestamp=event.data.get("timestamp", ""),
            )
        except ImportError:
            logger.debug("event_handler.cb_notification_oss_fallback")
            from baldur.adapters.notification import _send_cb_open_notification_oss

            _send_cb_open_notification_oss(
                service_name=event.data.get("service_name", "unknown"),
                timestamp=event.data.get("timestamp", ""),
            )
    except Exception as e:
        logger.warning(
            "notification.enqueue_cb_notification_failed",
            error=e,
        )


def _on_circuit_breaker_closed_notify(event: BaldurEvent) -> None:
    """
    Deliver the recovery (stand-down) notification on CB CLOSED.

    Mirror of ``_on_circuit_breaker_opened_notify``. CB CLOSED is emitted on
    the app request thread (the ``record_success`` recovery path); the Slack
    Webhook HTTP call is delegated to a Celery task via ``.delay()``. App
    traffic stays latency-free because this handler is subscribed
    fire-and-forget (``await_result=False``) — the EventBus dispatches it
    without awaiting the result, which holds even when the task runs inline
    (eager / no broker).

    On a core-only OSS install without the ``celery`` extra, the task import
    raises ``ImportError``; instead of skipping, the handler delivers the OSS
    recovery push synchronously through the shared wrapper (bounded, fail-open;
    off the request thread under the default ``async_pool`` dispatch). When
    Celery IS importable, the task body's own ``ImportError`` branch owns OSS
    delivery, so exactly one delivery happens. Dedup (single-send across broker
    redelivery and multi-pod fan-out) is enforced downstream by the task's
    singleton manager cooldown — see ``send_cb_close_notification``; the
    core-only fallback is a single "dumb" POST (no dedup), the documented OSS
    trade-off.
    """
    try:
        try:
            from baldur.adapters.celery.tasks import send_cb_close_notification

            send_cb_close_notification.delay(
                service_name=event.data.get("service_name", "unknown"),
                timestamp=event.data.get("timestamp", ""),
                previous_state=event.data.get("previous_state", ""),
                trigger=event.data.get("trigger", ""),
            )
        except ImportError:
            logger.debug("event_handler.cb_notification_oss_fallback")
            from baldur.adapters.notification import _send_cb_close_notification_oss

            _send_cb_close_notification_oss(
                service_name=event.data.get("service_name", "unknown"),
                timestamp=event.data.get("timestamp", ""),
                previous_state=event.data.get("previous_state", ""),
                trigger=event.data.get("trigger", ""),
            )
    except Exception as e:
        logger.warning(
            "notification.enqueue_cb_notification_failed",
            error=e,
        )


def _collect_web_server_metrics() -> dict | None:
    """Collect the Web Server's cached system metrics (~0ms). Returns None on failure."""
    try:
        from baldur.services.system_metrics_cache import get_system_metrics_cache

        cache = get_system_metrics_cache()
        if cache.is_running():
            return cache.get_snapshot_dict()
    except Exception:
        pass
    return None


def _on_circuit_breaker_opened_snapshot(event: BaldurEvent) -> None:
    """
    Delegate system snapshot collection to a Celery task on CB OPEN.

    The 100ms blocking of psutil.cpu_percent(interval=0.1) and the Redis HSET
    run inside a Celery task delegated via ``.delay()``. The publisher
    (request) thread is never blocked because this handler is subscribed
    fire-and-forget (``await_result=False``) — the EventBus dispatches it
    without awaiting the result, which holds even when the task runs inline
    (eager / no broker). In environments without Celery, it safely skips via
    ImportError fallback.
    """
    service_name = event.data.get("service_name", "unknown")
    try:
        from baldur.adapters.celery.tasks import collect_cb_open_snapshot

        web_metrics = _collect_web_server_metrics()

        collect_cb_open_snapshot.delay(
            service_name=service_name,
            event_timestamp=event.timestamp.isoformat(),
            web_server_metrics=web_metrics,
        )
    except ImportError:
        logger.debug("event_handler.celery_tasks_available_skipping")
    except Exception as e:
        logger.warning(
            "event_handler.enqueue_cb_snapshot_failed",
            error=e,
        )


def _send_postmortem_notification(
    settings,
    postmortem: dict,
    incident_id: str,
    service_name: str,
    duration: int | None,
    affected_services: list[str],
) -> None:
    """
    Send a notification when post-mortem generation is complete.

    Sends only when notification_enabled is True in Settings and the duration is
    at least notification_min_duration.

    Notification priority:
    - duration >= 300 sec (5 min) or affected_services >= 3: HIGH
    - otherwise: MEDIUM
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
            incident_id=incident_id,
        )
        return

    try:
        # Check whether notifications are enabled
        if not settings.notification_enabled:
            logger.debug(
                "notification.postmortem_notification_disabled",
                incident_id=incident_id,
            )
            return

        # Check minimum duration
        notification_min_duration = settings.notification_min_duration
        if duration is not None and duration < notification_min_duration:
            logger.debug(
                "notification.postmortem_notification_skipped_duration",
                incident_id=incident_id,
                duration=duration,
                notification_min_duration=notification_min_duration,
            )
            return

        # Priority: 5 min or more, or 3 or more affected services → HIGH
        affected_count = len(affected_services) if affected_services else 0
        if (duration is not None and duration >= 300) or affected_count >= 3:
            priority = NotificationPriority.HIGH
        else:
            priority = NotificationPriority.MEDIUM

        # Build the notification body
        resolved_at = postmortem.get("resolved_at", "N/A")
        started_at = postmortem.get("started_at", "N/A")
        recommendations = postmortem.get("recommendations", [])
        recommendations_summary = (
            ", ".join(recommendations[:3]) if recommendations else "None"
        )

        message = (
            f"Incident started: {started_at}\n"
            f"Incident ended: {resolved_at}\n"
            f"Duration: {duration} sec\n"
            f"Affected services: {', '.join(affected_services) if affected_services else 'None'}\n"
            f"Recommended actions: {recommendations_summary}"
        )

        payload = NotificationPayload(
            title=f"📋 Post-mortem created: {incident_id}",
            message=message,
            priority=priority,
            category=NotificationCategory.OPERATIONS,
            source="EventHandler.Postmortem",
            metadata={
                "incident_id": incident_id,
                "service_name": service_name,
                "duration_seconds": duration,
                "affected_services": affected_services,
                "resolved_at": resolved_at,
                "postmortem_url": f"/api/xtest/incidents/{incident_id}/",
            },
            dedup_key=f"postmortem:{incident_id}",
        )

        manager = get_unified_notification_manager()
        result = manager.notify(payload)

        if result.success and not result.suppressed:
            logger.info(
                "notification.postmortem_notification_sent",
                incident_id=incident_id,
            )
        elif result.suppressed:
            logger.debug(
                "notification.postmortem_notification_suppressed",
                incident_id=incident_id,
                suppression_reason=result.suppression_reason,
            )

    except Exception as e:
        logger.warning(
            "notification.send_postmortem_notification_failed",
            error=e,
        )


def _on_circuit_breaker_closed(event: BaldurEvent):
    """
    Trigger automatic Replay on CB recovery (on-recovery replay).

    The CRITICAL-priority PostRecoveryIntegrityGate
    (integrity_gate.py) runs before this handler and sets the
    event.data[INTEGRITY_FAILED_KEY] flag.
    Replay is blocked when the flag is True.

    Dispatch goes through the one recovery dispatch path
    (``dispatch_recovery_sweep``), which reads the on-recovery switch and the
    pass budgets and records the outcome in the arming ledger. A CLOSED event
    an operator's close-with-replay emitted (``trigger == "manual"``) queues an
    operator-requested chain: the operator's own pin does not stop it.
    """
    service_name = event.data.get("service_name", "unknown")

    # Check IntegrityGate result (import the constant to avoid typos)
    try:
        from baldur.services.event_bus.integrity_gate import INTEGRITY_FAILED_KEY

        if event.data.get(INTEGRITY_FAILED_KEY, False):
            logger.critical(
                "event_handler.replay_blocked_integrity_gate",
                service_name=service_name,
                integrity_gate_result=event.data.get("integrity_gate_result", {}),
            )
            return  # stop replay
    except ImportError:
        pass  # ignore if the integrity_gate module is not installed

    # Operator-requested replay suppression (e.g., force_close(trigger_replay=False),
    # Chaos rollback, Django admin "without replay" button).
    # Default True preserves the always-replay contract for auto-recovery and
    # reset, and protects future emit sites that forget the key.
    if not event.data.get("trigger_replay", True):
        logger.info(
            "event_handler.replay_suppressed_operator_request",
            service_name=service_name,
            correlation_id=event.correlation_id,
        )
        return

    from baldur.interfaces.repositories import ResolutionTrigger
    from baldur.services.replay_service.recovery import dispatch_recovery_sweep

    dispatch_recovery_sweep(
        service_name,
        trigger=ResolutionTrigger.AUTO_REPLAY_CIRCUIT_CLOSE,
        escalate_failures=True,
        operator_requested=(
            event.data.get("trigger") == "manual"
            and event.data.get("trigger_replay") is True
        ),
    )


def _on_circuit_breaker_closed_postmortem(event: BaldurEvent):
    """
    Auto-generate a Post-mortem on CB recovery.

    Runs only when auto_enabled is True in Settings.
    When incident_group_enabled is True, groups via IncidentGroupManager.
    When grouped, a unified Postmortem is generated when the group closes.
    """
    service_name = event.data.get("service_name", "unknown")

    # Check whether auto-generation is enabled in Settings
    try:
        from baldur.settings.postmortem import get_postmortem_settings

        settings = get_postmortem_settings()

        if not settings.auto_enabled:
            logger.debug(
                "event_handler.auto_postmortem_disabled_skipping",
                service_name=service_name,
            )
            return

        min_duration = settings.auto_min_duration
        history_limit = settings.history_limit

        # Check whether incident grouping is enabled
        incident_group_enabled = getattr(settings, "incident_group_enabled", True)
    except Exception as e:
        logger.warning(
            "event_handler.get_postmortem_settings_failed",
            error=e,
        )
        return

    # Handle incident grouping
    if incident_group_enabled:
        try:
            from ._emergency_postmortem import _handle_incident_group

            _handle_incident_group(event, service_name, settings)
            # when grouping, do not generate a Postmortem immediately
            # (handled by the close_incident_group task)
            return
        except Exception as e:
            logger.warning(
                "event_handler.incident_grouping_failed_fallback",
                error=e,
            )
            # Fallback: delegate individual Postmortem to a Celery task

    # Delegate individual Post-mortem generation to a Celery task
    try:
        from baldur.adapters.celery.tasks import process_individual_postmortem

        from . import get_event_bus

        # bus.get_history() is process-local in-memory, so collect it here
        bus = get_event_bus()
        event_bus_history = bus.get_history(limit=history_limit)

        web_metrics = _collect_web_server_metrics()

        # Serialize with event.to_dict() — Celery JSON serializer compatible
        process_individual_postmortem.delay(
            service_name=service_name,
            event_data=event.to_dict(),
            event_type="circuit_breaker_closed",
            event_bus_history=event_bus_history,
            web_server_metrics=web_metrics,
        )
    except ImportError:
        # Environment without Celery: fall back to the existing synchronous path
        from ._emergency_postmortem import _create_individual_postmortem

        _create_individual_postmortem(
            event, service_name, settings, min_duration, history_limit
        )
    except Exception as e:
        logger.warning(
            "event_handler.enqueue_postmortem_failed",
            error=e,
        )
