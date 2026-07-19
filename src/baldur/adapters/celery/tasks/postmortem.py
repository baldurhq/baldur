"""
Postmortem Celery Tasks.

Celery tasks for IncidentGroup closing and Notification aggregation.

Tasks:
- close_incident_group: close an incident group and create a consolidated
  Postmortem. Triggered on demand, either by the emergency-postmortem event
  path or by check_stale_incident_groups — do not schedule it directly.
- check_stale_incident_groups: find groups that meet the close condition and
  trigger close_incident_group for them
- flush_aggregated_notifications: send aggregated notifications

These tasks are registered but are NOT part of the composed beat schedule
that configure_baldur_celery() injects. To run the periodic ones, add them
to your own beat config:

    CELERY_BEAT_SCHEDULE.update({
        "check-stale-incident-groups": {
            "task": "baldur.adapters.celery.tasks.check_stale_incident_groups",
            "schedule": 60.0,  # Every minute
        },
        "flush-aggregated-notifications": {
            "task": "baldur.adapters.celery.tasks.flush_aggregated_notifications",
            "schedule": 30.0,  # Every 30 seconds
        },
    })
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

from baldur.dlq.helpers import add_healing_incident
from baldur.utils.time import utc_now

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.close_incident_group",
    queue="baldur",
    max_retries=2,
    time_limit=120,
    soft_time_limit=110,
    acks_late=True,
)
def close_incident_group(
    self,
    group_id: str,
    namespace: str = "default",
) -> dict[str, Any]:
    """
    Close an incident group and create a consolidated Postmortem.

    Closes the group in IncidentGroupManager and creates a consolidated
    Postmortem based on the contained incidents.

    If the incidents in the group are fewer than min_count, individual Postmortems are created.

    Args:
        group_id: ID of the group to close
        namespace: namespace

    Returns:
        Result dictionary
    """
    logger.info(
        "close_incident_group.starting_group",
        group_id=group_id,
    )

    try:
        from baldur.settings.postmortem import get_postmortem_settings

        try:
            from baldur_pro.services.postmortem.incident_group import (
                IncidentGroupStatus,
                get_incident_group_manager,
            )
        except ImportError:
            IncidentGroupStatus = None  # type: ignore[assignment,misc]
            get_incident_group_manager = None  # type: ignore[assignment,misc]

        settings = get_postmortem_settings()
        manager = get_incident_group_manager()

        # Look up the group
        group = manager.get_active_group(namespace)

        if not group:
            logger.info(
                "close_incident_group.no_active_group",
                namespace=namespace,
            )
            return {
                "success": True,
                "message": "No active group",
                "group_id": group_id,
            }

        if group.group_id != group_id:
            logger.warning(
                "close_incident_group.group_mismatch",
                group_id=group_id,
                group=group.group_id,
            )
            return {
                "success": False,
                "message": "Group ID mismatch",
                "group_id": group_id,
            }

        if group.status != IncidentGroupStatus.OPEN:
            logger.info(
                "close_incident_group.group_already",
                group_id=group_id,
                group_status=group.status.value,
            )
            return {
                "success": True,
                "message": f"Group already {group.status.value}",
                "group_id": group_id,
            }

        # Check the close condition
        if not manager.should_close_group(group_id, namespace):
            logger.info(
                "close_incident_group.group_ready_close",
                group_id=group_id,
            )
            return {
                "success": True,
                "message": "Group not ready to close",
                "group_id": group_id,
            }

        # Close the group
        closed_group = manager.close_group(group_id, namespace)
        if not closed_group:
            return {
                "success": False,
                "message": "Failed to close group",
                "group_id": group_id,
            }

        # Decide on Postmortem creation
        incident_count = closed_group.incident_count
        min_count = settings.incident_group_min_count

        if incident_count < min_count:
            # Create individual Postmortems
            logger.info(
                "close_incident_group.group_incidents_min_creating",
                group_id=group_id,
                incident_count=incident_count,
                min_count=min_count,
            )
            result = _create_individual_postmortems(closed_group)
        else:
            # Create a group Postmortem
            logger.info(
                "close_incident_group.group_incidents_creating_group",
                group_id=group_id,
                incident_count=incident_count,
            )
            result = _create_group_postmortem(closed_group)

        # Mark the group as completed
        manager.mark_completed(group_id, namespace)

        return {
            "success": True,
            "message": "Group closed and postmortem created",
            "group_id": group_id,
            "incident_count": incident_count,
            **result,
        }

    except Exception as e:
        logger.exception(
            "close_incident_group.error_closing_group",
            group_id=group_id,
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
            "group_id": group_id,
        }


def _create_individual_postmortems(group) -> dict[str, Any]:
    """Create individual Postmortems for the incidents in the group."""
    try:
        from baldur_pro.services.postmortem.integrity_sealer import (
            get_integrity_sealer,
        )

        sealer = get_integrity_sealer()
        created_ids = []

        for entry in group.entries:
            # Build the basic Postmortem data
            postmortem = {
                "incident_id": f"AUTO-{entry.service_name}-{entry.closed_at[:19].replace(':', '').replace('-', '')}",
                "generated_at": entry.closed_at,
                "started_at": entry.opened_at,
                "resolved_at": entry.closed_at,
                "duration_seconds": entry.duration_seconds,
                "summary": {
                    "affected_services": [entry.service_name],
                    "unaffected_services": [],
                    "fast_fail_count": 0,
                    "total_events": 1,
                },
                "is_group": False,
                "recommendations": [
                    f"Root cause analysis for service '{entry.service_name}'",
                    "Establish recurrence prevention measures",
                ],
            }

            # Integrity seal
            sealed = sealer.seal(postmortem)

            # Store
            add_healing_incident(sealed)
            created_ids.append(postmortem["incident_id"])

            logger.info(
                "close_incident_group.individual_postmortem_created",
                postmortem=postmortem["incident_id"],
            )

        return {
            "postmortem_type": "individual",
            "postmortem_ids": created_ids,
        }

    except Exception as e:
        logger.exception(
            "close_incident_group.error_creating_individual_postmortems",
            error=e,
        )
        return {
            "postmortem_type": "individual",
            "error": str(e),
        }


def _create_group_postmortem(group) -> dict[str, Any]:
    """Create a group Postmortem."""
    try:
        from baldur_pro.services.postmortem.integrity_sealer import (
            get_integrity_sealer,
        )

        sealer = get_integrity_sealer()
        now_iso = utc_now().isoformat()

        # Service list
        affected_services = list({e.service_name for e in group.entries})

        # Total downtime
        total_duration = sum(e.duration_seconds for e in group.entries)

        # Build the timeline
        services_timeline = []
        for entry in group.entries:
            services_timeline.append(
                {
                    "service_name": entry.service_name,
                    "opened_at": entry.opened_at,
                    "closed_at": entry.closed_at,
                    "duration_seconds": entry.duration_seconds,
                }
            )

        # Cascading pattern
        cascading_pattern = group.get_cascading_pattern()

        # Build the group Postmortem data
        postmortem = {
            "incident_id": group.group_id,
            "generated_at": now_iso,
            "started_at": group.created_at,
            "resolved_at": group.closed_at or now_iso,
            "duration_seconds": total_duration,
            "summary": {
                "affected_services": affected_services,
                "unaffected_services": [],
                "fast_fail_count": 0,
                "total_events": group.incident_count,
            },
            # Group-specific fields
            "is_group": True,
            "group_id": group.group_id,
            "incident_count": group.incident_count,
            "services_timeline": services_timeline,
            "cascading_pattern": cascading_pattern,
            "primary_service": group.primary_service,
            "namespace": group.namespace,
            # Recommended actions
            "recommendations": _generate_group_recommendations(
                cascading_pattern,
                affected_services,
                group.incident_count,
            ),
        }

        # Integrity seal
        sealed = sealer.seal(postmortem)

        # Store
        add_healing_incident(sealed)

        logger.info(
            "close_incident_group.group_postmortem_created",
            group=group.group_id,
            cascading_pattern=cascading_pattern,
            affected_services_count=len(affected_services),
        )

        return {
            "postmortem_type": "group",
            "postmortem_id": group.group_id,
            "cascading_pattern": cascading_pattern,
        }

    except Exception as e:
        logger.exception(
            "close_incident_group.error_creating_group_postmortem",
            error=e,
        )
        return {
            "postmortem_type": "group",
            "error": str(e),
        }


def _generate_group_recommendations(
    cascading_pattern: str,
    affected_services: list[str],
    incident_count: int,
) -> list[str]:
    """Generate recommended actions for the group Postmortem."""
    recommendations = []

    if cascading_pattern == "simultaneous":
        recommendations.append("Simultaneous failures - common cause analysis required")
        recommendations.append(
            "Review infrastructure/network level failure possibility"
        )
    elif cascading_pattern == "cascading":
        recommendations.append(
            "Cascading failure pattern detected - dependency chain analysis required"
        )
        recommendations.append(
            "Circuit Breaker configuration review (Fast Fail optimization)"
        )
    else:
        recommendations.append(
            "Independent failures coincidentally overlapping - analyze each individually"
        )

    if incident_count >= 5:
        recommendations.append(
            f"Multiple services affected ({incident_count}) - "
            f"system-wide stability review required"
        )

    if len(affected_services) > 3:
        recommendations.append(f"Affected services: {', '.join(affected_services[:5])}")

    recommendations.append("Review incident response process improvements")

    return recommendations


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.flush_aggregated_notifications",
    queue="baldur",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
    acks_late=True,
)
def flush_aggregated_notifications(
    self,
    namespace: str = "default",
) -> dict[str, Any]:
    """
    Send aggregated notifications.

    Summarizes the notifications aggregated by NotificationAggregator and sends them as a single notification.

    Args:
        namespace: namespace

    Returns:
        Result dictionary
    """
    logger.info(
        "flush_notifications.starting",
        namespace=namespace,
    )

    try:
        from baldur.settings.postmortem import get_postmortem_settings

        try:
            from baldur_pro.services.postmortem.notification_aggregator import (
                get_notification_aggregator,
            )
        except ImportError:
            get_notification_aggregator = None  # type: ignore[assignment,misc]

        settings = get_postmortem_settings()

        if not settings.notification_aggregation_enabled:
            logger.debug("flush_notifications.aggregation_disabled")
            return {
                "success": True,
                "message": "Aggregation disabled",
            }

        aggregator = get_notification_aggregator()

        # Check the flush condition
        if not aggregator.should_flush(namespace):
            pending_count = aggregator.get_pending_count(namespace)
            if pending_count > 0:
                logger.debug(
                    "flush_notifications.ready_flush_pending",
                    pending_count=pending_count,
                )
            return {
                "success": True,
                "message": "Not ready to flush",
                "pending_count": pending_count,
            }

        # Create the summary and send
        summary = aggregator.flush_and_create_summary(namespace)
        if not summary:
            return {
                "success": True,
                "message": "No pending notifications",
            }

        # Send the notification
        _send_aggregated_notification(summary, settings)

        return {
            "success": True,
            "message": "Notifications flushed",
            "total_incidents": summary.total_incidents,
            "affected_services": summary.affected_services,
        }

    except Exception as e:
        logger.exception(
            "flush_notifications.error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


def _send_aggregated_notification(summary, settings) -> None:
    """Send the aggregated notification."""
    try:
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
            get_unified_notification_manager,
        )

        # Determine the priority
        if summary.total_incidents >= 5 or len(summary.affected_services) >= 3:
            priority = NotificationPriority.HIGH
        elif summary.total_incidents >= 2:
            priority = NotificationPriority.MEDIUM
        else:
            priority = NotificationPriority.LOW

        # Build the message
        services_str = ", ".join(summary.affected_services[:5])
        if len(summary.affected_services) > 5:
            services_str += f" and {len(summary.affected_services) - 5} more"

        duration_minutes = int(summary.total_downtime_seconds / 60)

        message = (
            f"Aggregated incidents: {summary.total_incidents}\n"
            f"Affected services: {services_str}\n"
            f"Total downtime: {duration_minutes} min\n"
        )

        if summary.group_id:
            message += f"Group ID: {summary.group_id}\n"

        title = f"📊 Incident Summary: {summary.total_incidents} incidents occurred"

        payload = NotificationPayload(
            title=title,
            message=message,
            priority=priority,
            category=NotificationCategory.OPERATIONS,
            source="NotificationAggregator",
            metadata={
                "total_incidents": summary.total_incidents,
                "affected_services": summary.affected_services,
                "total_downtime_seconds": summary.total_downtime_seconds,
                "group_id": summary.group_id,
                "postmortem_links": summary.postmortem_links,
            },
            dedup_key=f"incident_summary:{summary.created_at[:16]}",
        )

        manager = get_unified_notification_manager()
        result = manager.notify(payload)

        if result.success:
            logger.info(
                "flush_notifications.summary_notification_sent_incidents",
                summary=summary.total_incidents,
            )
        else:
            logger.warning(
                "flush_notifications.notification_failed",
                result_error=result.error,
            )

    except ImportError as e:
        logger.debug(
            "flush_notifications.notification_module_available",
            error=e,
        )
    except Exception as e:
        logger.warning(
            "flush_notifications.send_notification_failed",
            error=e,
        )


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.check_stale_incident_groups",
    queue="baldur",
    max_retries=0,
    time_limit=60,
    soft_time_limit=55,
)
def check_stale_incident_groups(
    self,
    namespace: str = "default",
) -> dict[str, Any]:
    """
    Check for stale incident groups and schedule their closing.

    If there is an active group that meets the close condition, triggers the
    close_incident_group task.

    Args:
        namespace: namespace

    Returns:
        Result dictionary
    """
    logger.debug(
        "check_stale_groups.checking",
        namespace=namespace,
    )

    try:
        from baldur.settings.postmortem import get_postmortem_settings

        try:
            from baldur_pro.services.postmortem.incident_group import (
                get_incident_group_manager,
            )
        except ImportError:
            get_incident_group_manager = None  # type: ignore[assignment,misc]

        settings = get_postmortem_settings()

        if not settings.incident_group_enabled:
            return {
                "success": True,
                "message": "Incident grouping disabled",
            }

        manager = get_incident_group_manager()
        group = manager.get_active_group(namespace)

        if not group:
            return {
                "success": True,
                "message": "No active group",
            }

        if manager.should_close_group(group.group_id, namespace):
            # Trigger the close task
            close_incident_group.delay(
                group_id=group.group_id,
                namespace=namespace,
            )
            logger.info(
                "check_stale_groups.scheduled_close_group",
                group=group.group_id,
            )
            return {
                "success": True,
                "message": "Close task scheduled",
                "group_id": group.group_id,
            }

        return {
            "success": True,
            "message": "Group not ready to close",
            "group_id": group.group_id,
            "incident_count": group.incident_count,
        }

    except Exception as e:
        logger.exception(
            "check_stale_groups.error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.adapters.celery.tasks.process_individual_postmortem",
    queue="baldur",
    max_retries=2,
    time_limit=120,
    soft_time_limit=110,
    acks_late=True,
)
def process_individual_postmortem(
    self,
    service_name: str,
    event_data: dict,
    event_type: str,
    event_bus_history: list[dict] | None = None,
    web_server_metrics: dict | None = None,
) -> dict[str, Any]:
    """
    Create an individual Postmortem asynchronously.

    Performs snapshot collection, Timeline build, DB INSERT, WAL recording, and
    notification sending all in the Celery Worker to free the EventBus publisher thread.

    Args:
        service_name: target service name
        event_data: event data serialized via BaldurEvent.to_dict().
                    Must be a dict type for Celery JSON serializer compatibility.
        event_type: event type for branching
                    - "circuit_breaker_closed": create an individual Postmortem on CB recovery
                    - "emergency_recovery_completed": create a Postmortem on Emergency recovery
        event_bus_history: EventBus history pre-collected in the publisher (Web Server) process.
                          bus.get_history() is process-local in-memory, so
                          it returns an empty list in the Celery Worker.
                          The handler must collect and pass it before .delay().

    Worker execution environment caution:
    - The CPU/Memory of collect_system_snapshot() are Worker node values
    - get_healing_events(use_redis=True) queries healing events from Redis (also possible in the Worker)
    - CB state lookup — Redis-based, so it works correctly in the Worker too

    Returns:
        Postmortem creation result dictionary
    """
    logger.info(
        "process_individual_postmortem.starting_attempt",
        service_name=service_name,
        event_type=event_type,
        retry_attempt=self.request.retries + 1,
    )

    if event_bus_history is None:
        event_bus_history = []

    try:
        if event_type == "circuit_breaker_closed":
            return _process_cb_closed_postmortem(
                service_name=service_name,
                event_data=event_data,
                event_bus_history=event_bus_history,
                web_server_metrics=web_server_metrics,
            )
        if event_type == "emergency_recovery_completed":
            return _process_emergency_postmortem(
                service_name=service_name,
                event_data=event_data,
                event_bus_history=event_bus_history,
                web_server_metrics=web_server_metrics,
            )
        logger.warning(
            "process_individual_postmortem.unknown",
            event_type=event_type,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": f"Unknown event_type: {event_type}",
        }

    except Exception as e:
        logger.exception(
            "process_individual_postmortem.failed",
            service_name=service_name,
            error=e,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


def _process_cb_closed_postmortem(  # noqa: PLR0915
    service_name: str,
    event_data: dict,
    event_bus_history: list[dict],
    web_server_metrics: dict | None = None,
) -> dict[str, Any]:
    """Create an individual Postmortem on CB recovery (runs in the Celery Worker)."""
    from baldur.api.django.views.xtest.base import (
        collect_system_snapshot,
        get_healing_events,
    )
    from baldur.services.circuit_breaker import (
        get_circuit_breaker_service,
    )
    from baldur.settings.postmortem import get_postmortem_settings

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

    settings = get_postmortem_settings()
    min_duration = settings.auto_min_duration

    # Collect state (use the event_bus_history received from the publisher)
    cb_service = get_circuit_breaker_service()
    affected, unaffected = _collect_service_states(cb_service)
    local_events = get_healing_events(20, use_redis=True)
    timeline = _build_timeline(event_bus_history, local_events)
    snapshot = collect_system_snapshot()

    if web_server_metrics:
        # Preserve the Worker's original values in separate fields
        snapshot["worker_cpu_percent"] = snapshot.get("cpu_percent")
        snapshot["worker_memory_percent"] = snapshot.get("memory_percent")
        snapshot["worker_memory_used_mb"] = snapshot.get("memory_used_mb")
        snapshot["worker_memory_available_mb"] = snapshot.get("memory_available_mb")
        # Replace the main fields with Web Server values
        snapshot["cpu_percent"] = web_server_metrics.get(
            "cpu_percent", snapshot["cpu_percent"]
        )
        snapshot["memory_percent"] = web_server_metrics.get(
            "memory_percent", snapshot["memory_percent"]
        )
        snapshot["memory_used_mb"] = web_server_metrics.get(
            "memory_used_mb", snapshot.get("memory_used_mb", 0)
        )
        snapshot["memory_available_mb"] = web_server_metrics.get(
            "memory_available_mb", snapshot.get("memory_available_mb", 0)
        )
        snapshot["snapshot_source"] = "web_server_cache+worker"
        snapshot["snapshot_note"] = (
            "Main CPU/Memory=Web Server cache, worker_*=Celery Worker measurements."
        )
    else:
        snapshot["snapshot_source"] = "celery_worker"
        snapshot["snapshot_note"] = (
            "CPU/Memory of the Worker node. May differ from the Web Server."
        )

    # Fast fail count
    fast_fail_count = len(
        [e for e in event_bus_history if e.get("data", {}).get("fast_fail")]
    )

    # Generate the incident ID
    from django.utils import timezone

    incident_id = f"AUTO-{service_name}-{timezone.now().strftime('%Y%m%d-%H%M%S')}"

    # Create the Postmortem
    postmortem = _generate_postmortem_data(
        incident_id, timeline, affected, unaffected, fast_fail_count, snapshot
    )

    # Check the minimum duration
    duration = postmortem.get("duration_seconds")
    if duration is not None and duration < min_duration:
        logger.debug(
            "process_individual_postmortem.skipped_duration_min",
            service_name=service_name,
            duration=duration,
            min_duration=min_duration,
        )
        return {
            "success": True,
            "service_name": service_name,
            "skipped": True,
            "reason": "duration_below_minimum",
        }

    # Integrity seal
    try:
        from baldur_pro.services.postmortem.integrity_sealer import (
            get_integrity_sealer,
        )

        sealer = get_integrity_sealer()
        postmortem = sealer.seal(postmortem)
    except Exception as seal_error:
        logger.warning(
            "process_individual_postmortem.integrity_seal_failed",
            seal_error=seal_error,
        )

    # Store
    add_healing_incident(postmortem)

    logger.info(
        "process_individual_postmortem.cb_postmortem_generated",
        incident_id=incident_id,
        duration=duration,
    )

    # Send the notification
    _send_postmortem_notification_from_task(
        settings=settings,
        postmortem=postmortem,
        incident_id=incident_id,
        service_name=service_name,
        duration=duration,
        affected_services=affected,
    )

    # WAL Audit record
    try:
        from baldur_pro.services.audit.base import _write_to_wal

        _write_to_wal(
            event_type="POSTMORTEM_AUTO_GENERATED",
            source="CeleryTask.ProcessIndividualPostmortem",
            details={
                "incident_id": incident_id,
                "service_name": service_name,
                "duration_seconds": duration,
                "affected_services": affected,
                "trigger_event": event_data.get("event_type", "circuit_breaker_closed"),
            },
            success=True,
            domain="baldur",
            target_id=incident_id,
        )
    except Exception as audit_error:
        logger.warning(
            "process_individual_postmortem.log_audit_failed",
            audit_error=audit_error,
        )

    return {
        "success": True,
        "service_name": service_name,
        "incident_id": incident_id,
        "duration_seconds": duration,
    }


def _process_emergency_postmortem(
    service_name: str,
    event_data: dict,
    event_bus_history: list[dict],
    web_server_metrics: dict | None = None,
) -> dict[str, Any]:
    """Create a Postmortem on Emergency recovery completion (runs in the Celery Worker)."""
    from baldur.api.django.views.xtest.base import collect_system_snapshot
    from baldur.services.event_bus.bus import (
        _generate_emergency_postmortem_data,
    )
    from baldur.settings.postmortem import get_postmortem_settings

    settings = get_postmortem_settings()
    session_id = event_data.get("data", {}).get("session_id", "unknown")
    namespace = event_data.get("data", {}).get("namespace", "global")
    trigger_level = event_data.get("data", {}).get("trigger_level", "UNKNOWN")
    duration = event_data.get("data", {}).get("duration_seconds")

    # Collect the snapshot
    snapshot = collect_system_snapshot()

    if web_server_metrics:
        # Preserve the Worker's original values in separate fields
        snapshot["worker_cpu_percent"] = snapshot.get("cpu_percent")
        snapshot["worker_memory_percent"] = snapshot.get("memory_percent")
        snapshot["worker_memory_used_mb"] = snapshot.get("memory_used_mb")
        snapshot["worker_memory_available_mb"] = snapshot.get("memory_available_mb")
        # Replace the main fields with Web Server values
        snapshot["cpu_percent"] = web_server_metrics.get(
            "cpu_percent", snapshot["cpu_percent"]
        )
        snapshot["memory_percent"] = web_server_metrics.get(
            "memory_percent", snapshot["memory_percent"]
        )
        snapshot["memory_used_mb"] = web_server_metrics.get(
            "memory_used_mb", snapshot.get("memory_used_mb", 0)
        )
        snapshot["memory_available_mb"] = web_server_metrics.get(
            "memory_available_mb", snapshot.get("memory_available_mb", 0)
        )
        snapshot["snapshot_source"] = "web_server_cache+worker"
        snapshot["snapshot_note"] = (
            "Main CPU/Memory=Web Server cache, worker_*=Celery Worker measurements."
        )
    else:
        snapshot["snapshot_source"] = "celery_worker"
        snapshot["snapshot_note"] = (
            "CPU/Memory of the Worker node. May differ from the Web Server."
        )

    # Generate the Emergency Postmortem data
    postmortem = _generate_emergency_postmortem_data(
        session_data=event_data.get("data", {}),
        event_bus_history=event_bus_history,
        snapshot=snapshot,
    )

    # Store
    add_healing_incident(postmortem)

    incident_id = postmortem.get("incident_id")
    logger.info(
        "process_individual_postmortem.emergency_postmortem_generated",
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
            source="CeleryTask.ProcessIndividualPostmortem",
            details={
                "incident_id": incident_id,
                "session_id": session_id,
                "namespace": namespace,
                "trigger_level": trigger_level,
                "duration_seconds": duration,
                "requires_approval": event_data.get("data", {}).get(
                    "requires_approval", False
                ),
                "approved_by": event_data.get("data", {}).get("approved_by"),
            },
            success=True,
            domain="baldur",
            target_id=incident_id,
        )
    except Exception as audit_error:
        logger.warning(
            "process_individual_postmortem.log_emergency_audit_failed",
            audit_error=audit_error,
        )

    # Send the notification
    try:
        _send_postmortem_notification_from_task(
            settings=settings,
            postmortem=postmortem,
            incident_id=incident_id,
            service_name=service_name,
            duration=duration,
            affected_services=[],
        )
    except Exception as notify_error:
        logger.warning(
            "process_individual_postmortem.send_emergency_notification_failed",
            notify_error=notify_error,
        )

    return {
        "success": True,
        "service_name": service_name,
        "incident_id": incident_id,
        "duration_seconds": duration,
        "trigger_level": trigger_level,
    }


def _send_postmortem_notification_from_task(
    settings,
    postmortem: dict,
    incident_id: str,
    service_name: str,
    duration: int | None,
    affected_services: list[str],
) -> None:
    """Send a Postmortem notification from within a Celery Task."""
    try:
        if not settings.notification_enabled:
            logger.debug(
                "process_individual_postmortem.notification_disabled",
                incident_id=incident_id,
            )
            return

        notification_min_duration = settings.notification_min_duration
        if duration is not None and duration < notification_min_duration:
            logger.debug(
                "process_individual_postmortem.notification_skipped_duration_min",
                incident_id=incident_id,
                duration=duration,
                notification_min_duration=notification_min_duration,
            )
            return

        try:
            from baldur_pro.services.unified_notification import (
                NotificationCategory,
                NotificationPayload,
                NotificationPriority,
                UnifiedNotificationManager,
            )
        except ImportError:
            NotificationCategory = None  # type: ignore[assignment,misc]
            NotificationPayload = None  # type: ignore[assignment,misc]
            NotificationPriority = None  # type: ignore[assignment,misc]
            UnifiedNotificationManager = None  # type: ignore[assignment,misc]

        # Determine the priority: 5 minutes or more, or 3+ services affected → HIGH
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
            f"Incident resolved: {resolved_at}\n"
            f"Duration: {duration}s\n"
            f"Affected services: {', '.join(affected_services) if affected_services else 'None'}\n"
            f"Recommendations: {recommendations_summary}"
        )

        payload = NotificationPayload(
            title=f"📋 Post-mortem Generated: {incident_id}",
            message=message,
            priority=priority,
            category=NotificationCategory.OPERATIONS,
            source="CeleryTask.ProcessIndividualPostmortem",
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

        manager = UnifiedNotificationManager()
        result = manager.notify(payload)

        if result.success and not result.suppressed:
            logger.info(
                "process_individual_postmortem.notification_sent",
                incident_id=incident_id,
            )
        elif result.suppressed:
            logger.debug(
                "process_individual_postmortem.notification_suppressed",
                incident_id=incident_id,
                suppression_reason=result.suppression_reason,
            )

    except Exception as e:
        logger.warning(
            "process_individual_postmortem.send_notification_failed",
            error=e,
        )
