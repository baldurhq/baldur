"""
Circuit Breaker Celery Tasks

Consolidated tasks for managing circuit breaker states and recovery.
All tasks route through the service layer (get_circuit_breaker_service)
for full observability (EventBus, audit, Kill Switch, callbacks, metrics).

The canonical ``conditional_replay_on_circuit_close`` task lives in
``baldur.celery_tasks.dlq_tasks`` — it is dispatched by the EventBus
handler ``_on_circuit_breaker_closed`` on every CB CLOSED transition.
"""

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@shared_task(
    bind=True,
    name="baldur.celery_tasks.check_circuit_breaker_recovery",
    queue="maintenance",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
)
def check_circuit_breaker_recovery(self) -> dict:
    """
    Periodic task to check for circuit breaker state transitions.

    Checks if any circuit breakers in OPEN state should transition
    to HALF_OPEN based on recovery timeout.

    This task should be scheduled to run every minute.

    Returns:
        Dictionary with check results
    """
    from baldur.services import get_circuit_breaker_service

    logger.debug("circuit_check.transition_check_started")

    try:
        service = get_circuit_breaker_service()
        result = service.check_recovery_transitions()

        if result.get("count", 0) > 0:
            logger.info(
                "circuit_check.recovery_transitions_completed",
                transitioned_count=result["count"],
                transitioned=result.get("transitioned", []),
            )

        return result

    except Exception as e:
        logger.exception(
            "circuit_check.transition_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.expire_manual_overrides",
    queue="maintenance",
    max_retries=1,
    time_limit=60,
    soft_time_limit=55,
)
def expire_manual_overrides(self) -> dict:
    """
    Periodic task to expire manual circuit breaker overrides.

    Manual overrides have a TTL to prevent "forgotten" blocks.
    When expired:
    - OPEN circuits transition to HALF_OPEN for gradual recovery
    - The manually_controlled flag is cleared

    This ensures operators cannot accidentally leave services blocked
    indefinitely. Default TTL is 90 minutes.

    This task should be scheduled to run every 5 minutes.

    Returns:
        Dictionary with expiration results
    """
    from baldur.services import get_circuit_breaker_service

    logger.debug("circuit_breaker.expired_overrides_checked")

    try:
        service = get_circuit_breaker_service()
        expired = service.check_and_expire_manual_overrides()

        if expired:
            logger.warning(
                "circuit_breaker.manual_override_expired",
                expired=expired,
            )

        return {
            "success": True,
            "expired_services": expired,
            "count": len(expired),
        }

    except Exception as e:
        logger.exception(
            "circuit_breaker.expire_overrides_error",
            error=e,
        )
        return {
            "success": False,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.force_open_circuit_breaker",
    queue="critical",
    max_retries=0,
    time_limit=30,
    soft_time_limit=25,
)
def force_open_circuit_breaker(
    self,
    service_name: str,
    reason: str = "",
    actor_info: dict | None = None,
) -> dict:
    """
    Force open a circuit breaker (block all requests).

    This task can be triggered programmatically or via admin actions.
    Actor information is restored from actor_info parameter or Celery headers
    (auto-propagated by ActorContextHandler).

    Args:
        service_name: Name of the service to block
        reason: Reason for opening the circuit
        actor_info: Actor information dict (optional, for explicit override)

    Returns:
        Dictionary with operation result
    """
    from baldur.context.actor_context import restore_actor_from_celery
    from baldur.services import get_circuit_breaker_service

    logger.debug(
        "circuit_breaker.celery_force_open_dispatched",
        service_name=service_name,
        reason=reason,
    )

    try:
        with restore_actor_from_celery(actor_info or {}):
            service = get_circuit_breaker_service()
            result = service.force_open(
                service_name=service_name,
                reason=reason,
            )

        if result.success:
            return {
                "success": True,
                "service_name": service_name,
                "previous_state": result.previous_state,
                "new_state": result.new_state,
                "message": result.message,
            }
        return {
            "success": False,
            "service_name": service_name,
            "error": result.error,
        }

    except Exception as e:
        logger.exception(
            "circuit_breaker.celery_force_open_dispatch_error",
            error=e,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.force_close_circuit_breaker",
    queue="critical",
    max_retries=0,
    time_limit=30,
    soft_time_limit=25,
)
def force_close_circuit_breaker(
    self,
    service_name: str,
    reason: str = "",
    actor_info: dict | None = None,
    trigger_replay: bool = False,
) -> dict:
    """
    Force close a circuit breaker (allow all requests).

    This task can be triggered programmatically or via admin actions.
    Actor information is restored from actor_info parameter or Celery headers
    (auto-propagated by ActorContextHandler).

    Args:
        service_name: Name of the service to unblock
        reason: Reason for closing the circuit
        actor_info: Actor information dict (optional, for explicit override)
        trigger_replay: Whether to trigger conditional replay

    Returns:
        Dictionary with operation result
    """
    from baldur.context.actor_context import restore_actor_from_celery
    from baldur.services import get_circuit_breaker_service

    logger.debug(
        "circuit_breaker.celery_force_close_dispatched",
        service_name=service_name,
        reason=reason,
    )

    try:
        with restore_actor_from_celery(actor_info or {}):
            service = get_circuit_breaker_service()
            result = service.force_close(
                service_name=service_name,
                reason=reason,
                trigger_replay=trigger_replay,
            )

        if result.success:
            return {
                "success": True,
                "service_name": service_name,
                "previous_state": result.previous_state,
                "new_state": result.new_state,
                "message": result.message,
            }
        return {
            "success": False,
            "service_name": service_name,
            "error": result.error,
        }

    except Exception as e:
        logger.exception(
            "circuit_breaker.celery_force_close_dispatch_error",
            error=e,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


@shared_task(
    bind=True,
    name="baldur.celery_tasks.send_cb_open_notification",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def send_cb_open_notification(
    self,
    service_name: str,
    timestamp: str = "",
) -> dict:
    """
    Send a CB OPEN notification asynchronously.

    Offloads the external HTTP call (e.g. Slack Webhook) to a Celery worker
    so the EventBus publisher thread is never blocked.

    Args:
        service_name: service whose CB opened
        timestamp: CB OPEN event timestamp

    Returns:
        notification result dictionary
    """
    logger.info(
        "send_cb_open_notification.sending_notification_attempt",
        service_name=service_name,
        retry_attempt=self.request.retries + 1,
    )

    try:
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
            get_unified_notification_manager,
        )
    except ImportError:
        # OSS-without-PRO worker: deliver the OSS circuit-breaker push directly,
        # outside the registry seam. Constructs the Slack webhook adapter when
        # BALDUR_META_WATCHDOG_SLACK_WEBHOOK_URL is configured, else logs intent
        # ("OSS observes"). The OSS path omits the PRO-only actionable URLs /
        # dedup; those stay PRO differentiators. The OPEN payload literals live
        # in the wrapper, shared with the core-only EventBus handler fallback.
        from baldur.adapters.notification import _send_cb_open_notification_oss

        return _send_cb_open_notification_oss(
            service_name=service_name,
            timestamp=timestamp,
        )

    try:
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
        )

        # Build actionable URLs
        url_builder = get_actionable_alert_url_builder()
        actionable_urls = url_builder.build_cb_open_urls(
            service_name=service_name,
            trigger_time=timestamp,
        )

        manager = get_unified_notification_manager()
        manager.notify(
            NotificationPayload(
                title=f"\U0001f534 Circuit Breaker OPEN: {service_name}",
                message=f"Circuit Breaker opened for service '{service_name}'.",
                priority=NotificationPriority.HIGH,
                category=NotificationCategory.CIRCUIT_BREAKER,
                source="circuit_breaker_service",
                dedup_key=f"cb:{service_name}:open",
                metadata={
                    "service_name": service_name,
                    "event_type": "circuit_breaker_opened",
                    "trigger_time": timestamp,
                    # Actionable Alert URLs
                    "dashboard_url": actionable_urls.dashboard_url,
                    "admin_url": actionable_urls.admin_url,
                    "runbook_url": actionable_urls.runbook_url,
                },
            )
        )

        logger.info(
            "send_cb_open_notification.notification_sent",
            service_name=service_name,
        )

        return {
            "success": True,
            "service_name": service_name,
            "notification_sent": True,
        }

    except Exception as e:
        logger.exception(
            "send_cb_open_notification.failed",
            service_name=service_name,
            error=e,
        )
        raise


@shared_task(
    bind=True,
    name="baldur.celery_tasks.send_cb_close_notification",
    queue="baldur",
    autoretry_for=(Exception,),
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    time_limit=60,
    soft_time_limit=55,
)
def send_cb_close_notification(
    self,
    service_name: str,
    timestamp: str = "",
    previous_state: str = "",
    trigger: str = "",
) -> dict:
    """
    Send a CB CLOSED (recovery) stand-down notification asynchronously.

    Symmetric to ``send_cb_open_notification``: offloads the external HTTP
    call (e.g. Slack Webhook) to a Celery worker so the EventBus publisher
    thread — here the app request thread on the ``record_success`` path — is
    never blocked.

    The notification is Slack-only and low-urgency: priority LOW with
    ``channels=["slack"]`` pinned, so UNM escalation cannot lift the resolve
    onto a paging channel. Single-send across multi-fire is bounded per
    worker process by the singleton manager's cooldown cache + the 300s
    CIRCUIT_BREAKER category cooldown (dedup_key ``cb:{service}:resolved``).
    Fires for every recovery trigger (auto / manual / manual_reset) — like
    Alertmanager's ``send_resolved``, the message states why.

    Args:
        service_name: service whose CB recovered
        timestamp: CB CLOSED event timestamp
        previous_state: CB state before recovery (e.g. "open", "half_open")
        trigger: recovery trigger (auto / manual / manual_reset)

    Returns:
        notification result dictionary
    """
    logger.info(
        "send_cb_close_notification.sending_notification_attempt",
        service_name=service_name,
        retry_attempt=self.request.retries + 1,
    )

    try:
        from baldur_pro.services.unified_notification import (
            NotificationCategory,
            NotificationPayload,
            NotificationPriority,
            get_unified_notification_manager,
        )
    except ImportError:
        # OSS-without-PRO worker: deliver the OSS circuit-breaker push directly,
        # outside the registry seam. Parity with send_cb_open_notification —
        # low-urgency recovery stand-down via the directly-constructed Slack
        # webhook adapter (or the logging fallback when the URL is unset). The
        # CLOSED payload literals live in the wrapper, shared with the core-only
        # EventBus handler fallback.
        from baldur.adapters.notification import _send_cb_close_notification_oss

        return _send_cb_close_notification_oss(
            service_name=service_name,
            timestamp=timestamp,
            previous_state=previous_state,
            trigger=trigger,
        )

    try:
        from baldur.services.circuit_breaker.actionable_alert_urls import (
            get_actionable_alert_url_builder,
        )

        # Build actionable URLs (dashboard + admin history; no runbook on
        # recovery). Unset base URLs yield None values, tolerated downstream.
        url_builder = get_actionable_alert_url_builder()
        actionable_urls = url_builder.build_cb_closed_urls(
            service_name=service_name,
            recovery_time=timestamp,
        )

        manager = get_unified_notification_manager()
        manager.notify(
            NotificationPayload(
                title=f"\U0001f7e2 Circuit Breaker recovered: {service_name}",
                message=(
                    f"Circuit Breaker recovered for service '{service_name}' "
                    f"(trigger: {trigger or 'unknown'})."
                ),
                priority=NotificationPriority.LOW,
                category=NotificationCategory.CIRCUIT_BREAKER,
                source="circuit_breaker_service",
                channels=["slack"],
                dedup_key=f"cb:{service_name}:resolved",
                metadata={
                    "service_name": service_name,
                    "event_type": "circuit_breaker_closed",
                    "trigger_time": timestamp,
                    "previous_state": previous_state,
                    "trigger": trigger,
                    # Actionable Alert URLs (history link, no runbook)
                    "dashboard_url": actionable_urls.dashboard_url,
                    "admin_url": actionable_urls.admin_url,
                    "runbook_url": actionable_urls.runbook_url,
                },
            )
        )

        logger.info(
            "send_cb_close_notification.notification_sent",
            service_name=service_name,
        )

        return {
            "success": True,
            "service_name": service_name,
            "notification_sent": True,
        }

    except Exception as e:
        logger.exception(
            "send_cb_close_notification.failed",
            service_name=service_name,
            error=e,
        )
        raise


@shared_task(
    bind=True,
    name="baldur.celery_tasks.collect_cb_open_snapshot",
    queue="baldur",
    max_retries=1,
    default_retry_delay=10,
    acks_late=True,
    time_limit=30,
    soft_time_limit=25,
)
def collect_cb_open_snapshot(
    self,
    service_name: str,
    event_timestamp: str,
    web_server_metrics: dict | None = None,
) -> dict:
    """
    Collect a system snapshot at CB OPEN time and store it in Redis.

    Offloads the blocking psutil.cpu_percent(interval=0.1) and the Redis
    HSET to a Celery worker so the publisher thread is never blocked.

    When web_server_metrics is provided, the primary CPU/Memory fields are
    replaced with the web-server values and the worker originals are kept
    under worker_* prefixed fields.

    Args:
        service_name: service whose CB opened
        event_timestamp: CB OPEN event timestamp (ISO format)
        web_server_metrics: cached system metrics from the web server
            (passed by the EventBus handler)

    Returns:
        snapshot collection result dictionary
    """
    logger.info(
        "collect_cb_open_snapshot.collecting_snapshot",
        service_name=service_name,
    )

    try:
        from baldur.api.django.views.xtest.base import collect_system_snapshot

        try:
            from baldur_pro.services.postmortem.snapshot_builder import (
                save_open_snapshot_to_redis,
            )
        except ImportError:
            save_open_snapshot_to_redis = None  # type: ignore[assignment,misc]

        # Collect the system snapshot
        snapshot = collect_system_snapshot()
        snapshot["captured_at"] = "open"
        snapshot["service"] = service_name
        snapshot["event_timestamp"] = event_timestamp

        if web_server_metrics:
            # Preserve the worker originals under separate fields
            snapshot["worker_cpu_percent"] = snapshot.get("cpu_percent")
            snapshot["worker_memory_percent"] = snapshot.get("memory_percent")
            snapshot["worker_memory_used_mb"] = snapshot.get("memory_used_mb")
            snapshot["worker_memory_available_mb"] = snapshot.get("memory_available_mb")
            # Replace the primary fields with the web-server cached values
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
                "Primary CPU/Memory from web-server cache; worker_* from Celery worker."
            )
        else:
            snapshot["snapshot_source"] = "celery_worker"
            snapshot["snapshot_note"] = (
                "CPU/Memory from the worker node. May differ from the web server."
            )

        # Add CB state information (Redis-based, so readable from the worker)
        try:
            from baldur.services.circuit_breaker import (
                get_circuit_breaker_service,
            )

            cb_service = get_circuit_breaker_service()
            # `get_all_services` / `get_status(name)` are PRO-impl extensions
            # (the OSS `CircuitBreakerService` exposes `get_state(name)` only).
            # Duck-type so the snapshot stays best-effort in OSS deployments.
            get_all = getattr(cb_service, "get_all_services", None)
            get_status = getattr(cb_service, "get_status", None)
            cb_states: dict[str, str] = {}
            if callable(get_all) and callable(get_status):
                for name in get_all():
                    status = get_status(name)
                    cb_states[name] = (
                        status.get("state", "UNKNOWN") if status else "UNKNOWN"
                    )
            snapshot["cb_states"] = str(cb_states)  # Redis HASH stores strings only
        except Exception as e:
            logger.debug(
                "collect_cb_open_snapshot.get_cb_states_failed",
                error=e,
            )

        # Save to Redis
        success = save_open_snapshot_to_redis(service_name, snapshot)

        if success:
            logger.info(
                "collect_cb_open_snapshot.snapshot_saved",
                service_name=service_name,
            )
        else:
            logger.warning(
                "collect_cb_open_snapshot.save_snapshot_failed",
                service_name=service_name,
            )

        return {
            "success": success,
            "service_name": service_name,
            "snapshot_source": "celery_worker",
        }

    except Exception as e:
        logger.exception(
            "collect_cb_open_snapshot.failed",
            service_name=service_name,
            error=e,
        )
        return {
            "success": False,
            "service_name": service_name,
            "error": str(e),
        }


# check_mesh_override_renewals moved to baldur_pro.services.circuit_mesh.tasks
# (599 D10 - the circuit_mesh feature relocated to the private distribution).
