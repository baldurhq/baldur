"""
Emergency Postmortem auto-generation handlers.

This module is an internal implementation of the baldur.services.event_bus.bus package.
"""

from __future__ import annotations

from datetime import UTC

import structlog

from baldur.dlq.helpers import add_healing_incident

from . import BaldurEvent

logger = structlog.get_logger()


def _handle_incident_group(event: BaldurEvent, service_name: str, settings) -> None:
    """
    Add a CB CLOSED event to an IncidentGroup.

    Schedules a close timer when a new group is created.
    """
    try:
        from baldur_pro.services.postmortem.incident_group import (
            get_incident_group_manager,
        )
    except ImportError:
        get_incident_group_manager = None  # type: ignore[assignment,misc]

    manager = get_incident_group_manager()
    namespace = event.data.get("namespace", "default")

    group_id, is_new_group = manager.add_incident(
        service_name=service_name,
        event=event,
        namespace=namespace,
    )

    if is_new_group:
        # Schedule a close timer when a new group is created
        _schedule_group_close(group_id, namespace, settings)
        logger.info(
            "event_handler.new_incident_group_created",
            group_id=group_id,
            service_name=service_name,
        )
    else:
        logger.info(
            "event_handler.incident_added_existing_group",
            group_id=group_id,
            service_name=service_name,
        )


def _schedule_group_close(group_id: str, namespace: str, settings) -> None:
    """Schedule the group-close Celery task."""
    try:
        from baldur.adapters.celery.tasks import close_incident_group

        window_seconds = getattr(settings, "incident_group_window_seconds", 600)

        # Run the group-close task after the window ends
        close_incident_group.apply_async(
            kwargs={
                "group_id": group_id,
                "namespace": namespace,
            },
            countdown=window_seconds,
        )

        logger.debug(
            "event_handler.scheduled_group_close",
            group_id=group_id,
            window_seconds=window_seconds,
        )

    except ImportError:
        logger.debug("event_handler.celery_tasks_available_skipping")
    except Exception as e:
        logger.warning(
            "event_handler.schedule_group_close_failed",
            error=e,
        )


def _create_individual_postmortem(  # noqa: PLR0915
    event: BaldurEvent,
    service_name: str,
    settings,
    min_duration: int,
    history_limit: int,
) -> None:
    """Create an individual Post-mortem (when grouping is disabled or as a fallback)."""
    try:
        from baldur.api.django.views.xtest.base import (
            collect_system_snapshot,
            get_healing_events,
        )
        from baldur.services.circuit_breaker import (
            get_circuit_breaker_service,
        )

        try:
            from baldur_pro.services.postmortem.store import (
                build_timeline as _build_timeline,
            )
        except ImportError:
            _build_timeline = None  # type: ignore[assignment,misc]
        try:
            from baldur_pro.services.postmortem.store import (
                collect_service_states as _collect_service_states,
            )
        except ImportError:
            _collect_service_states = None  # type: ignore[assignment,misc]
        try:
            from baldur_pro.services.postmortem.store import (
                generate_postmortem_data as _generate_postmortem_data,
            )
        except ImportError:
            _generate_postmortem_data = None  # type: ignore[assignment,misc]

        from . import get_event_bus

        # Collect history and state
        bus = get_event_bus()
        history = bus.get_history(limit=history_limit)
        cb_service = get_circuit_breaker_service()
        affected, unaffected = _collect_service_states(cb_service)
        local_events = get_healing_events(20)
        timeline = _build_timeline(history, local_events)
        snapshot = collect_system_snapshot()

        # Fast fail count
        fast_fail_count = len(
            [e for e in history if e.get("data", {}).get("fast_fail")]
        )

        # Generate incident ID
        from baldur.utils.time import utc_now

        incident_id = f"AUTO-{service_name}-{utc_now().strftime('%Y%m%d-%H%M%S')}"

        # Generate the Post-mortem
        postmortem = _generate_postmortem_data(
            incident_id, timeline, affected, unaffected, fast_fail_count, snapshot
        )

        # Check minimum duration
        duration = postmortem.get("duration_seconds")
        if duration is not None and duration < min_duration:
            logger.debug(
                "event_handler.auto_postmortem_skipped_duration",
                service_name=service_name,
                duration=duration,
                min_duration=min_duration,
            )
            return

        # Integrity sealing
        try:
            from baldur_pro.services.postmortem.integrity_sealer import (
                get_integrity_sealer,
            )

            sealer = get_integrity_sealer()
            postmortem = sealer.seal(postmortem)
        except Exception as seal_error:
            logger.warning(
                "event_handler.integrity_seal_failed",
                seal_error=seal_error,
            )

        # Save
        add_healing_incident(postmortem)

        logger.info(
            "event_handler.auto_postmortem_generated",
            incident_id=incident_id,
            duration=duration,
        )

        # Send Post-mortem notification
        from ._cb_handlers import _send_postmortem_notification

        _send_postmortem_notification(
            settings, postmortem, incident_id, service_name, duration, affected
        )

        # WAL Audit record - automatic Post-mortem generation event
        try:
            from baldur_pro.services.audit.base import _write_to_wal

            _write_to_wal(
                event_type="POSTMORTEM_AUTO_GENERATED",
                source="EventHandler.Postmortem",
                details={
                    "incident_id": incident_id,
                    "service_name": service_name,
                    "duration_seconds": duration,
                    "affected_services": affected,
                    "trigger_event": event.event_type.value,
                },
                success=True,
                domain="baldur",
                target_id=incident_id,
            )
        except Exception as audit_error:
            logger.warning(
                "event_handler.log_postmortem_audit_failed",
                audit_error=audit_error,
            )

    except ImportError:
        logger.debug("event_handler.postmortem_module_available_skipping")
    except Exception as e:
        logger.exception(
            "event_handler.generate_auto_postmortem_failed",
            error=e,
        )


def _build_emergency_timeline(event_bus_history: list) -> list:
    """Build the Emergency-related timeline."""
    timeline = []
    emergency_event_types = [
        "emergency_activated",
        "emergency_recovery_started",
        "emergency_recovery_completed",
        "emergency_level_changed",
    ]

    for event in event_bus_history:
        event_type = event.get("event_type", "").lower()
        if any(etype in event_type for etype in emergency_event_types):
            timeline.append(
                {
                    "timestamp": event.get("timestamp"),
                    "event_type": event.get("event_type"),
                    "details": event.get("data", {}),
                }
            )

    # Also include CB events (those that occurred during the Emergency)
    cb_events = [
        e
        for e in event_bus_history
        if "circuit_breaker" in e.get("event_type", "").lower()
    ]
    for event in cb_events[:10]:
        timeline.append(
            {
                "timestamp": event.get("timestamp"),
                "event_type": event.get("event_type"),
                "details": event.get("data", {}),
            }
        )

    # Sort chronologically
    timeline.sort(key=lambda x: x.get("timestamp", ""), reverse=False)
    return timeline


def _build_recovery_steps(steps_executed: int) -> list:
    """Extract recovery step information."""
    step_types = ["BUDGET_RESET", "HEALTH_CHECK", "CANARY_RESUME", "GOVERNANCE_NORMAL"]
    recovery_steps = []
    for i in range(min(steps_executed, len(step_types))):
        recovery_steps.append(
            {
                "step_order": i + 1,
                "step_type": step_types[i] if i < len(step_types) else f"STEP_{i + 1}",
                "status": "COMPLETED",
            }
        )
    return recovery_steps


def _build_emergency_actions(
    trigger_level: str,
    steps_executed: int,
    requires_approval: bool,
    approved_by: str | None,
) -> tuple[list, list]:
    """Generate dynamic Emergency Action Items and recommendations."""
    auto_actions = []
    recommendations = []

    if trigger_level == "LEVEL_3":
        auto_actions.append(
            {
                "action": "GOVERNANCE_NORMALIZED",
                "description": "Re-enable automation (STRICT → NORMAL)",
                "status": "completed",
            }
        )
        recommendations.append(
            "Analyze the root cause of the LEVEL_3 outage and establish "
            "recurrence-prevention measures"
        )

    if steps_executed > 0:
        auto_actions.append(
            {
                "action": "BUDGET_RESET",
                "description": "Normalize Crisis Multiplier (1.0x)",
                "status": "completed",
            }
        )

    if requires_approval:
        auto_actions.append(
            {
                "action": "MANUAL_APPROVAL",
                "description": f"Manual approval completed (approver: {approved_by or 'unknown'})",
                "status": "completed",
            }
        )
        recommendations.append(
            "Review the manual approval process and assess whether it can be automated"
        )

    recommendations.append(f"Analyze the root cause of Emergency {trigger_level}")
    recommendations.append("Review ways to shorten the recovery-process time")

    return auto_actions, recommendations


def _collect_emergency_cascade_event_data(
    namespace: str,
) -> tuple[str | None, list[str], str | None]:
    """Collect Emergency CascadeEvent audit evidence."""
    cascade_event_id = None
    causation_chain: list[str] = []
    evidence_hash = None
    try:
        from baldur.audit.cascade_auditor import get_cascade_event_auditor

        auditor = get_cascade_event_auditor()
        recent_events = auditor.get_recent_events(namespace=namespace, limit=50)

        for event in recent_events:
            if "EMERGENCY" in event.trigger.trigger_type:
                cascade_event_id = event.id
                causation_chain = event.get_causation_chain()
                evidence_hash = event.current_hash
                break
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "event_handler.cascade_event_collect_failed",
            error=e,
        )
    return cascade_event_id, causation_chain, evidence_hash


def _build_emergency_deep_links(
    incident_id: str,
    namespace: str,
    started_at: str | None,
    completed_at: str | None,
    cascade_event_id: str | None,
    evidence_hash: str | None,
) -> dict:
    """Build Emergency deep links."""
    try:
        from baldur_pro.services.postmortem.deep_links import (
            get_postmortem_deep_link_builder,
        )

        deep_link_builder = get_postmortem_deep_link_builder()
        postmortem_links = deep_link_builder.build_postmortem_links(
            incident_id=incident_id,
            service_name=f"emergency-{namespace}",
            start_time=started_at,
            end_time=completed_at,
            namespace=namespace,
            cascade_event_id=cascade_event_id,
            evidence_hash=evidence_hash,
        )
        return postmortem_links.to_dict()
    except ImportError:
        pass
    except Exception as e:
        logger.debug(
            "event_handler.deep_links_build_failed",
            error=e,
        )
    return {}


def _generate_emergency_postmortem_data(
    session_data: dict,
    event_bus_history: list,
    snapshot: dict,
) -> dict:
    """
    Generate Postmortem data when Emergency recovery completes.

    Unlike CB Postmortems, an Emergency Postmortem is generated based on
    recovery-session information for a regional/global outage.

    Args:
        session_data: session info passed from the EMERGENCY_RECOVERY_COMPLETED event
        event_bus_history: EventBus history
        snapshot: system snapshot

    Returns:
        Emergency Postmortem data dictionary
    """
    from datetime import datetime

    # Extract session data
    session_id = session_data.get("session_id", "unknown")
    namespace = session_data.get("namespace", "global")
    trigger_level = session_data.get("trigger_level", "UNKNOWN")
    started_at = session_data.get("started_at")
    completed_at = session_data.get("completed_at")
    duration_seconds = session_data.get("duration_seconds")
    steps_executed = session_data.get("steps_executed", 0)
    total_steps = session_data.get("total_steps", 0)
    requires_approval = session_data.get("requires_approval", False)
    approved_by = session_data.get("approved_by")

    now = datetime.now(UTC)
    current_time = now.isoformat()
    incident_id = f"EMERGENCY-{namespace}-{now.strftime('%Y%m%d-%H%M%S')}"

    # Collect data using helper functions
    timeline = _build_emergency_timeline(event_bus_history)
    recovery_steps = _build_recovery_steps(steps_executed)
    auto_actions, recommendations = _build_emergency_actions(
        trigger_level, steps_executed, requires_approval, approved_by
    )
    cascade_event_id, causation_chain, evidence_hash = (
        _collect_emergency_cascade_event_data(namespace)
    )
    deep_links = _build_emergency_deep_links(
        incident_id,
        namespace,
        started_at,
        completed_at,
        cascade_event_id,
        evidence_hash,
    )

    return {
        "incident_id": incident_id,
        "generated_at": current_time,
        "started_at": started_at,
        "resolved_at": completed_at,
        "duration_seconds": duration_seconds,
        # Emergency-specific fields
        "recovery_type": "emergency",
        "namespace": namespace,
        "trigger_level": trigger_level,
        "recovery_session_id": session_id,
        "recovery_steps": recovery_steps,
        "requires_approval": requires_approval,
        "approved_by": approved_by,
        # Common fields
        "summary": {
            "affected_services": [],
            "unaffected_services": [],
            "fast_fail_count": 0,
            "total_events": len(timeline),
            "steps_executed": steps_executed,
            "total_steps": total_steps,
        },
        "timeline": timeline[:30],
        "system_snapshot": snapshot,
        "auto_actions": auto_actions,
        "recommendations": recommendations,
        "deep_links": deep_links,
        "cascade_event_id": cascade_event_id,
        "causation_chain": causation_chain,
        "evidence_hash": evidence_hash,
    }


def _on_emergency_recovery_completed_postmortem(event: BaldurEvent):
    """
    Delegate Postmortem generation to a Celery Task when Emergency recovery completes.

    When the RecoveryCoordinator completes recovery, the EMERGENCY_RECOVERY_COMPLETED
    event is published, and snapshot collection / DB save / WAL record / notification
    sending are delegated to a Celery Worker.

    Only Settings validation and the min_duration check are done synchronously (fast).
    In environments without Celery, it automatically falls back to the existing
    synchronous path.
    """
    session_id = event.data.get("session_id", "unknown")
    namespace = event.data.get("namespace", "global")
    event.data.get("trigger_level", "UNKNOWN")
    duration = event.data.get("duration_seconds")

    # Check whether auto-generation is enabled in Settings
    try:
        from baldur.settings.postmortem import get_postmortem_settings

        settings = get_postmortem_settings()

        if not settings.auto_enabled:
            logger.debug(
                "event_handler.auto_postmortem_disabled_skipping",
                session_id=session_id,
            )
            return

        min_duration = settings.auto_min_duration
        history_limit = settings.history_limit
    except Exception as e:
        logger.warning(
            "event_handler.get_postmortem_settings_failed",
            error=e,
        )
        return

    # Check minimum duration (fast check, no I/O)
    if duration is not None and duration < min_duration:
        logger.debug(
            "event_handler.emergency_postmortem_skipped_duration",
            session_id=session_id,
            duration=duration,
            min_duration=min_duration,
        )
        return

    # Delegate to a Celery Task
    try:
        from baldur.adapters.celery.tasks import process_individual_postmortem

        from . import get_event_bus
        from ._cb_handlers import _collect_web_server_metrics

        # bus.get_history() is process-local in-memory, so collect it here
        bus = get_event_bus()
        event_bus_history = bus.get_history(limit=history_limit)

        web_metrics = _collect_web_server_metrics()

        process_individual_postmortem.delay(
            service_name=f"emergency-{namespace}",
            event_data=event.to_dict(),
            event_type="emergency_recovery_completed",
            event_bus_history=event_bus_history,
            web_server_metrics=web_metrics,
        )
    except ImportError:
        # Environment without Celery: fall back to the existing synchronous path
        _create_emergency_postmortem_sync(event, namespace, history_limit)
    except Exception as e:
        logger.warning(
            "event_handler.enqueue_emergency_postmortem_failed",
            error=e,
        )


def _create_emergency_postmortem_sync(
    event: BaldurEvent,
    namespace: str,
    history_limit: int,
) -> None:
    """Generate an Emergency Postmortem synchronously (fallback without Celery)."""
    session_id = event.data.get("session_id", "unknown")
    trigger_level = event.data.get("trigger_level", "UNKNOWN")
    duration = event.data.get("duration_seconds")

    try:
        from baldur.api.django.views.xtest.base import collect_system_snapshot

        from . import get_event_bus

        # Collect history and snapshot
        bus = get_event_bus()
        history = bus.get_history(limit=history_limit)
        snapshot = collect_system_snapshot()

        # Generate Emergency Postmortem data
        postmortem = _generate_emergency_postmortem_data(
            session_data=event.data,
            event_bus_history=history,
            snapshot=snapshot,
        )

        # Save
        add_healing_incident(postmortem)

        incident_id = postmortem.get("incident_id")
        logger.info(
            "event_handler.emergency_postmortem_generated",
            incident_id=incident_id,
            session_id=session_id,
            trigger_level=trigger_level,
            duration=duration,
        )

        # WAL Audit record
        try:
            from baldur_pro.services.audit.base import _write_to_wal

            _write_to_wal(
                event_type="EMERGENCY_POSTMORTEM_AUTO_GENERATED",
                source="EventHandler.EmergencyPostmortem",
                details={
                    "incident_id": incident_id,
                    "session_id": session_id,
                    "namespace": namespace,
                    "trigger_level": trigger_level,
                    "duration_seconds": duration,
                    "requires_approval": event.data.get("requires_approval", False),
                    "approved_by": event.data.get("approved_by"),
                },
                success=True,
                domain="baldur",
                target_id=incident_id,
            )
        except Exception as audit_error:
            logger.warning(
                "event_handler.log_emergency_postmortem_failed",
                audit_error=audit_error,
            )

        # Send Postmortem notification
        try:
            from baldur.settings.postmortem import get_postmortem_settings

            from ._cb_handlers import _send_postmortem_notification

            settings = get_postmortem_settings()
            _send_postmortem_notification(
                settings=settings,
                postmortem=postmortem,
                incident_id=incident_id or "unknown",
                service_name=f"emergency-{namespace}",
                duration=duration,
                affected_services=[],
            )
        except Exception as notify_error:
            logger.warning(
                "event_handler.send_emergency_postmortem_failed",
                notify_error=notify_error,
            )

    except ImportError as e:
        logger.debug(
            "event_handler.module_available_emergency_postmortem",
            error=e,
        )
    except Exception as e:
        logger.exception(
            "event_handler.generate_emergency_postmortem_failed",
            error=e,
        )
