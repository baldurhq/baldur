"""
Celery Beat Schedule for Baldur Autonomous Tasks

Consolidates all autonomous task schedules from 3 lanes:
- Cleanup Lane (Cleanup & Expire)
- Intelligence Lane (Analyze & Learn)
- Compliance Lane (Compliance & Report)

Usage:
    # Option 1: One-line wrapper (recommended)
    from baldur.adapters.celery.beat_schedule import configure_baldur_celery
    configure_baldur_celery(app)

    # Option 2: Manual merge
    from baldur.adapters.celery.beat_schedule import get_baldur_beat_schedule
    app.conf.beat_schedule.update(get_baldur_beat_schedule())
"""

from __future__ import annotations

from typing import Any

import structlog
from kombu import Exchange, Queue

logger = structlog.get_logger()


# =============================================================================
# kombu Queue/Exchange Definitions (321 — Beat Internalization, Q4)
# =============================================================================

_baldur_exchange = Exchange("baldur", type="direct", durable=True)

_baldur_dlx = Exchange("baldur.dlx", type="direct", durable=True)

_QUEUE_DEFINITIONS: list[Queue] = [
    # Cleanup Lane
    Queue(
        "maintenance",
        exchange=_baldur_exchange,
        routing_key="maintenance",
        queue_arguments={
            "x-max-priority": 3,
            "x-queue-type": "quorum",
        },
    ),
    Queue(
        "critical_maintenance",
        exchange=_baldur_exchange,
        routing_key="critical_maintenance",
        queue_arguments={
            "x-max-priority": 10,
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": _baldur_dlx.name,
        },
    ),
    # Intelligence Lane
    Queue(
        "analysis",
        exchange=_baldur_exchange,
        routing_key="analysis",
        queue_arguments={
            "x-max-priority": 5,
            "x-queue-type": "quorum",
        },
    ),
    Queue(
        "realtime",
        exchange=_baldur_exchange,
        routing_key="realtime",
        queue_arguments={
            "x-max-priority": 10,
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": _baldur_dlx.name,
            "x-message-ttl": 30000,
        },
    ),
    Queue(
        "reports",
        exchange=_baldur_exchange,
        routing_key="reports",
        queue_arguments={
            "x-max-priority": 2,
            "x-queue-type": "quorum",
        },
    ),
    Queue(
        "metrics",
        exchange=_baldur_exchange,
        routing_key="metrics",
        queue_arguments={
            "x-max-priority": 1,
            "x-queue-type": "quorum",
        },
    ),
    # Audit Flush
    Queue(
        "audit_flush",
        exchange=_baldur_exchange,
        routing_key="audit_flush",
        queue_arguments={
            "x-max-priority": 4,
            "x-queue-type": "quorum",
        },
    ),
    # Compliance
    Queue(
        "compliance",
        exchange=_baldur_exchange,
        routing_key="compliance",
        queue_arguments={
            "x-max-priority": 7,
            "x-queue-type": "quorum",
        },
    ),
    # Chaos Engineering
    Queue(
        "chaos",
        exchange=_baldur_exchange,
        routing_key="chaos",
        queue_arguments={
            "x-max-priority": 5,
            "x-queue-type": "quorum",
        },
    ),
    Queue(
        "chaos_monitoring",
        exchange=_baldur_exchange,
        routing_key="chaos.monitoring",
        queue_arguments={
            "x-max-priority": 6,
            "x-queue-type": "quorum",
        },
    ),
    # Critical (Recovery)
    Queue(
        "baldur.critical",
        exchange=Exchange("baldur.critical", type="direct", durable=True),
        routing_key="baldur.critical",
        queue_arguments={
            "x-max-priority": 10,
            "x-queue-type": "quorum",
            "x-dead-letter-exchange": _baldur_dlx.name,
        },
    ),
]

# Backward-compatible dict (gradual migration support)
BALDUR_QUEUE_CONFIG = {
    q.name: {
        "exchange": q.exchange.name,
        "routing_key": q.routing_key,
        "queue_arguments": q.queue_arguments or {},
    }
    for q in _QUEUE_DEFINITIONS
}


# =============================================================================
# Queue Namespace Isolation (321, Q3)
# =============================================================================


def get_baldur_queues(
    prefix: str = "",
    queue_type: str = "quorum",
    enable_dlx: bool = True,
) -> list[Queue]:
    """Return kombu.Queue list with optional namespace prefix.

    When prefix is specified, queue name, Exchange name, and routing key
    all get the prefix applied for broker-level message isolation.

    Args:
        prefix: Queue namespace prefix for multi-service isolation.
        queue_type: RabbitMQ queue type (classic/quorum/stream).
        enable_dlx: Whether to keep DLX bindings on critical queues.
    """
    result: list[Queue] = []
    for q in _QUEUE_DEFINITIONS:
        name = f"{prefix}.{q.name}" if prefix else q.name
        if prefix:
            exchange = Exchange(
                f"{prefix}.{q.exchange.name}",
                type=q.exchange.type,
                durable=q.exchange.durable,
            )
            routing_key = f"{prefix}.{q.routing_key}"
        else:
            exchange = q.exchange
            routing_key = q.routing_key

        args = dict(q.queue_arguments or {})
        args["x-queue-type"] = queue_type
        if not enable_dlx:
            args.pop("x-dead-letter-exchange", None)

        result.append(
            Queue(
                name,
                exchange=exchange,
                routing_key=routing_key,
                queue_arguments=args,
            )
        )
    return result


# =============================================================================
# Task Routes (321, Q3/Q6)
# =============================================================================

_CRITICAL_TASK_ROUTES = {
    "baldur.celery_tasks.execute_recovery_step": "baldur.critical",
    "baldur.celery_tasks.check_recovery_trigger": "baldur.critical",
    "baldur.celery_tasks.monitor_recovery_health": "baldur.critical",
    "baldur.celery_tasks.check_circuit_breaker_recovery": "baldur.critical",
}


def get_baldur_task_routes(prefix: str = "") -> dict[str, dict[str, str]]:
    """Return task routing configuration for critical baldur tasks.

    When prefix is specified, queue names and routing keys include the prefix.
    """
    routes: dict[str, dict[str, str]] = {}
    for task_name, queue_name in _CRITICAL_TASK_ROUTES.items():
        if prefix:
            routes[task_name] = {
                "queue": f"{prefix}.{queue_name}",
                "routing_key": f"{prefix}.{queue_name}",
            }
        else:
            routes[task_name] = {
                "queue": queue_name,
                "routing_key": queue_name,
            }
    return routes


# =============================================================================
# Consolidated Beat Schedule
# =============================================================================


# Module load config: (include_flag_name, module_path, getter_func_name, debug_message)
_SCHEDULE_MODULES = [
    (
        "cleanup",
        "baldur.tasks.cleanup_tasks",
        "get_cleanup_beat_schedule",
        "cleanup lane",
    ),
    (
        "intelligence",
        "baldur.tasks.intelligence_tasks",
        "get_intelligence_beat_schedule",
        "intelligence lane",
    ),
    (
        "compliance",
        "baldur.tasks.compliance_tasks",
        "get_compliance_beat_schedule",
        "compliance lane",
    ),
    # 599 D10 — relocated finops report rides the same reporting-lane flag
    # via a private-path module (saga precedent: absent wheel -> lane skipped).
    (
        "compliance",
        "baldur_pro.services.finops.tasks",
        "get_finops_beat_schedule",
        "FinOps report lane (PRO)",
    ),
    # 599 D10 — relocated compliance check rides the same flag via the
    # dormant private lane.
    (
        "compliance",
        "baldur_dormant.services.compliance.tasks",
        "get_compliance_check_beat_schedule",
        "compliance check lane (Dormant)",
    ),
    # 599 D10 — relocated cross-stage insight analysis rides the
    # intelligence-lane flag via the dormant private lane.
    (
        "intelligence",
        "baldur_dormant.services.learning.tasks",
        "get_learning_beat_schedule",
        "learning insights lane (Dormant)",
    ),
    (
        "traffic_aware",
        "baldur.tasks.traffic_aware_replay",
        "get_traffic_aware_beat_schedule",
        "traffic-aware replay",
    ),
    (
        "canary_watchdog",
        "baldur.tasks.canary_watchdog",
        "get_canary_watchdog_beat_schedule",
        "canary watchdog",
    ),
    (
        "governance",
        "baldur.tasks.governance",
        "get_governance_beat_schedule",
        "governance (emergency mode expiry)",
    ),
    (
        "xtest_cleanup",
        "baldur.tasks.xtest_cleanup_tasks",
        "get_xtest_cleanup_beat_schedule",
        "X-Test cleanup",
    ),
    # 600 D2 — canonical drain module (distributed-lock guarded,
    # Processing-Queue safe). @shared_task self-registers on this importlib
    # load, so beat injection <-> task registration stay structurally in sync.
    (
        "audit_flush",
        "baldur.celery_tasks.audit_flush_tasks",
        "get_audit_flush_beat_schedule",
        "Redis Audit flush",
    ),
    (
        "saga",
        "baldur_pro.services.saga.tasks",
        "get_saga_beat_schedule",
        "Saga orchestrator",
    ),
    (
        "chaos_scheduler",
        "baldur.tasks.chaos_scheduler",
        "get_beat_schedule_for_celery",
        "chaos scheduler (zombie hunter, scheduled experiments)",
    ),
    (
        "postmortem",
        "baldur.tasks.postmortem_tasks",
        "get_postmortem_beat_schedule",
        "postmortem (auto-seal)",
    ),
    (
        "dlq_maintenance",
        "baldur.celery_tasks.dlq_tasks",
        "get_dlq_maintenance_beat_schedule",
        "DLQ maintenance (eviction, expiry)",
    ),
    # 665 D1 — apply due DELAYED/GRACEFUL config changes (30s). @shared_task
    # self-registers on this importlib load, so beat injection <-> task
    # registration stay structurally in sync (audit_flush precedent).
    (
        "config_apply",
        "baldur.tasks.config_apply",
        "get_config_apply_beat_schedule",
        "config apply (pending DELAYED/GRACEFUL)",
    ),
]


def _load_schedule_module(
    module_path: str,
    getter_func_name: str,
    debug_message: str,
) -> dict[str, Any]:
    """Load a single schedule module dynamically."""
    try:
        import importlib

        module = importlib.import_module(module_path)
        getter_func = getattr(module, getter_func_name)
        schedule = getter_func()
        logger.debug(
            "beat_schedule.added_schedules",
            debug_message=debug_message,
        )
        return schedule
    except ImportError as e:
        logger.warning(
            "beat_schedule.load_tasks",
            debug_message=debug_message,
            error=e,
        )
    except AttributeError as e:
        logger.warning(
            "beat_schedule.getter_not_found",
            module_path=module_path,
            error=e,
        )
    return {}


def get_baldur_beat_schedule(
    include_cleanup: bool = True,
    include_intelligence: bool = True,
    include_compliance: bool = True,
    include_traffic_aware: bool = True,
    include_canary_watchdog: bool = True,
    include_governance: bool = True,
    include_xtest_cleanup: bool = True,
    include_audit_flush: bool | None = None,
    include_saga: bool = True,
    include_chaos_scheduler: bool = True,
    include_postmortem: bool = True,
    include_dlq_maintenance: bool = True,
    include_config_apply: bool = True,
    include_legacy: bool = True,
) -> dict[str, Any]:
    """Get consolidated Celery Beat schedule for all baldur tasks.

    Args:
        include_cleanup: Include Cleanup Lane tasks
        include_intelligence: Include Intelligence Lane tasks
        include_compliance: Include Compliance Lane tasks
        include_traffic_aware: Include Traffic-Aware Replay tasks
        include_canary_watchdog: Include Canary Watchdog tasks
        include_governance: Include Governance tasks (emergency mode expiry)
        include_xtest_cleanup: Include X-Test Artifact Cleanup tasks
        include_audit_flush: Include Redis Audit buffer drain tasks. None
            (default) resolves from the effective drain gate (master audit
            ``enabled`` AND ``buffer_redis_enabled``); an explicit bool is an
            operator override.
        include_saga: Include Saga Orchestrator tasks (orphan saga scan)
        include_chaos_scheduler: Include Chaos Scheduler tasks
        include_postmortem: Include Postmortem tasks (auto-seal)
        include_dlq_maintenance: Include DLQ maintenance tasks (eviction, expiry)
        include_config_apply: Include config-apply tasks (pending DELAYED/GRACEFUL)
        include_legacy: Include legacy tasks from adapters/celery/tasks.py

    Returns:
        Complete Celery Beat schedule configuration dict.

    Usage:
        from baldur.adapters.celery.beat_schedule import get_baldur_beat_schedule

        CELERY_BEAT_SCHEDULE = {
            **get_baldur_beat_schedule(),
            # ... your custom schedules
        }
    """
    # 600 D3 — None resolves the audit-flush injection from the effective
    # drain gate; an explicit bool remains an operator override.
    if include_audit_flush is None:
        from baldur.settings.audit import is_redis_drain_enabled

        include_audit_flush = is_redis_drain_enabled()

    include_flags = {
        "cleanup": include_cleanup,
        "intelligence": include_intelligence,
        "compliance": include_compliance,
        "traffic_aware": include_traffic_aware,
        "canary_watchdog": include_canary_watchdog,
        "governance": include_governance,
        "xtest_cleanup": include_xtest_cleanup,
        "audit_flush": include_audit_flush,
        "saga": include_saga,
        "chaos_scheduler": include_chaos_scheduler,
        "postmortem": include_postmortem,
        "dlq_maintenance": include_dlq_maintenance,
        "config_apply": include_config_apply,
    }

    schedule: dict[str, Any] = {}

    for flag_name, module_path, getter_func, debug_msg in _SCHEDULE_MODULES:
        if include_flags.get(flag_name, False):
            schedule.update(_load_schedule_module(module_path, getter_func, debug_msg))

    if include_legacy:
        schedule.update(_get_legacy_beat_schedule())
        logger.debug("beat_schedule.added_legacy_schedules")

    return schedule


def _get_legacy_beat_schedule() -> dict[str, Any]:
    """Legacy tasks from existing adapters/celery/tasks.py.

    These will be gradually migrated to lane-based tasks.
    """
    from celery.schedules import crontab

    return {
        "check-circuit-breaker-recovery-legacy": {
            "task": "baldur.celery_tasks.check_circuit_breaker_recovery",
            "schedule": crontab(minute="*/2"),
            "options": {"queue": "realtime"},
        },
        "expire-manual-overrides": {
            "task": "baldur.celery_tasks.expire_manual_overrides",
            "schedule": crontab(minute="*/10"),
            "options": {"queue": "maintenance"},
        },
    }


# =============================================================================
# Consumer Integration Wrapper (321, Q6)
# =============================================================================

_celery_configured = False


def configure_baldur_celery(
    app,
    *,
    include_cleanup: bool = True,
    include_intelligence: bool = True,
    include_compliance: bool = True,
    include_traffic_aware: bool = True,
    include_canary_watchdog: bool = True,
    include_governance: bool = True,
    include_xtest_cleanup: bool = True,
    include_audit_flush: bool | None = None,
    include_saga: bool = True,
    include_chaos_scheduler: bool = True,
    include_postmortem: bool = True,
    include_dlq_maintenance: bool = True,
    include_config_apply: bool = True,
    include_legacy: bool = True,
    queue_prefix: str = "",
    queue_type: str = "quorum",
    enable_dlx: bool = True,
) -> None:
    """Inject baldur Beat schedule, queues, routes, and tasks into a Celery app.

    Symmetric with configure_baldur(namespace=globals()) for Django settings.
    Idempotent — second call is a no-op with a warning log.

    Args:
        app: Celery application instance.
        include_*: Module-level Beat task inclusion flags.
        queue_prefix: Queue namespace prefix for multi-service isolation.
        queue_type: RabbitMQ queue type (classic/quorum/stream).
        enable_dlx: Whether to enable DLX bindings on critical queues.
    """
    global _celery_configured
    if _celery_configured:
        logger.warning("beat_schedule.celery_already_configured")
        return

    # 1. Beat Schedule merge
    schedule = get_baldur_beat_schedule(
        include_cleanup=include_cleanup,
        include_intelligence=include_intelligence,
        include_compliance=include_compliance,
        include_traffic_aware=include_traffic_aware,
        include_canary_watchdog=include_canary_watchdog,
        include_governance=include_governance,
        include_xtest_cleanup=include_xtest_cleanup,
        include_audit_flush=include_audit_flush,
        include_saga=include_saga,
        include_chaos_scheduler=include_chaos_scheduler,
        include_postmortem=include_postmortem,
        include_dlq_maintenance=include_dlq_maintenance,
        include_config_apply=include_config_apply,
        include_legacy=include_legacy,
    )

    # 1b. Apply queue_prefix to beat schedule queue options
    if queue_prefix:
        for entry in schedule.values():
            opts = entry.get("options", {})
            if "queue" in opts:
                opts["queue"] = f"{queue_prefix}.{opts['queue']}"

    if not hasattr(app.conf, "beat_schedule") or app.conf.beat_schedule is None:
        app.conf.beat_schedule = {}
    app.conf.beat_schedule.update(schedule)

    # 2. Queue definitions merge (kombu.Queue objects)
    queues = get_baldur_queues(
        prefix=queue_prefix,
        queue_type=queue_type,
        enable_dlx=enable_dlx,
    )
    existing = list(app.conf.task_queues or [])
    app.conf.task_queues = existing + queues

    # 3. Task Routes merge
    existing_routes = dict(app.conf.task_routes or {})
    existing_routes.update(get_baldur_task_routes(prefix=queue_prefix))
    app.conf.task_routes = existing_routes

    # 4. Task registration
    register_all_tasks_with_celery(app)

    _celery_configured = True
    logger.info(
        "beat_schedule.celery_configured",
        queue_prefix=queue_prefix or "(none)",
    )


def _reset_celery_configured() -> None:
    """Reset idempotency guard (testing only)."""
    global _celery_configured
    _celery_configured = False


# =============================================================================
# Schedule Helpers
# =============================================================================


def get_schedule_summary() -> dict[str, Any]:
    """Get human-readable summary of all scheduled tasks.

    Useful for documentation and debugging.
    """
    schedule = get_baldur_beat_schedule()

    summary: dict[str, Any] = {
        "total_tasks": len(schedule),
        "by_lane": {
            "cleanup": [],
            "intelligence": [],
            "compliance": [],
            "legacy": [],
        },
        "by_queue": {},
    }

    lane_prefixes = {
        "cleanup": ["cleanup-", "archive-", "expire-", "purge-"],
        "intelligence": ["check-sla", "analyze-", "check-recovery"],
        "compliance": ["generate-", "collect-baldur"],
    }

    for name, config in schedule.items():
        queue = config.get("options", {}).get("queue", "default")

        if queue not in summary["by_queue"]:
            summary["by_queue"][queue] = []
        summary["by_queue"][queue].append(name)

        categorized = False
        for lane, prefixes in lane_prefixes.items():
            if any(name.startswith(prefix) for prefix in prefixes):
                summary["by_lane"][lane].append(name)
                categorized = True
                break

        if not categorized:
            summary["by_lane"]["legacy"].append(name)

    return summary


def validate_schedule() -> dict[str, Any]:
    """Validate schedule configuration.

    Returns:
        Dict with validation results.
    """
    schedule = get_baldur_beat_schedule()
    errors = []
    warnings = []

    for name, config in schedule.items():
        if "task" not in config:
            errors.append(f"{name}: missing 'task' field")

        if "schedule" not in config:
            errors.append(f"{name}: missing 'schedule' field")

        queue = config.get("options", {}).get("queue")
        if (
            queue
            and queue not in BALDUR_QUEUE_CONFIG
            and queue != "default"
            and queue != "dlq_processing"
        ):
            warnings.append(f"{name}: queue '{queue}' not in BALDUR_QUEUE_CONFIG")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "task_count": len(schedule),
    }


# =============================================================================
# Registration Helper
# =============================================================================


def register_all_tasks_with_celery(app) -> None:
    """Register all baldur tasks with a Celery application.

    Args:
        app: Celery application instance.
    """
    from baldur.tasks.chaos_scheduler import (
        register_celery_tasks as register_chaos_tasks,
    )
    from baldur.tasks.compliance_tasks import register_compliance_tasks_with_celery
    from baldur.tasks.intelligence_tasks import (
        register_intelligence_tasks_with_celery,
    )
    from baldur.tasks.traffic_aware_replay import (
        register_traffic_aware_tasks_with_celery,
    )

    register_chaos_tasks(app)
    register_intelligence_tasks_with_celery(app)
    register_compliance_tasks_with_celery(app)
    register_traffic_aware_tasks_with_celery(app)

    # 599 D10 — private-lane class-based task registration (saga precedent
    # for the lane shape; @shared_task modules self-register on import and
    # need no entry here). Absent private wheel -> lane silently skipped.
    try:
        from baldur_pro.services.finops.tasks import (
            register_finops_tasks_with_celery,
        )
    except ImportError:
        logger.debug("beat_schedule.private_finops_lane_unavailable")
    else:
        register_finops_tasks_with_celery(app)

    try:
        from baldur_dormant.services.compliance.tasks import (
            register_compliance_check_tasks_with_celery,
        )
    except ImportError:
        logger.debug("beat_schedule.private_compliance_lane_unavailable")
    else:
        register_compliance_check_tasks_with_celery(app)

    try:
        from baldur_dormant.services.learning.tasks import (
            register_learning_tasks_with_celery,
        )
    except ImportError:
        logger.debug("beat_schedule.private_learning_lane_unavailable")
    else:
        register_learning_tasks_with_celery(app)

    logger.info("beat_schedule.all_baldur_tasks")


__all__ = [
    "get_baldur_beat_schedule",
    "get_baldur_queues",
    "get_baldur_task_routes",
    "configure_baldur_celery",
    "get_schedule_summary",
    "validate_schedule",
    "register_all_tasks_with_celery",
    "BALDUR_QUEUE_CONFIG",
]
