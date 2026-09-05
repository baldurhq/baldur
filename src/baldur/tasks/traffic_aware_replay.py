"""
🚦 Traffic-Aware Replay Task

Traffic-state-aware DLQ Replay

Performs DLQ Replay only when traffic has normalized.
Runs every minute via a Beat Schedule, and replays only when all of the
following conditions are met:

Health Checks:
1. Every circuit projecting onto the entry's own stored domain is CLOSED,
   read from a freshly refreshed snapshot of the shared circuit store
2. Governance checks pass (Kill Switch, Emergency Mode)

The lane picks the domains it may drain before it selects any entry, because
the check is per entry-domain: replaying "everything pending" while gating on
one task argument gates nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.audit.helpers import log_traffic_aware_replay_audit
from baldur.services.replay_service.handlers import has_replay_handler
from baldur.tasks.base import BaseNotifyingTask
from baldur.tasks.notification_policy import (
    NotificationPolicy,
    NotificationTiming,
)

logger = structlog.get_logger()


# =============================================================================
# Traffic Health Status
# =============================================================================


_CLOSED_STATE = "closed"

# Why a pending domain was left out of a pass. Logged by name, because a
# domain this lane will never drain otherwise looks exactly like a domain with
# no pending work — and "no circuit projects onto it" is the permanent one.
DROP_REASON_CIRCUIT_OPEN = "circuit_open"
DROP_REASON_NO_CIRCUIT_PROJECTS = "no_circuit_projects"
DROP_REASON_NO_REPLAY_HANDLER = "no_replay_handler_registered"


@dataclass
class CircuitProjection:
    """Every known circuit, grouped by the stored domain its name projects onto.

    Circuits are keyed by the raw ``protect()`` name while DLQ entries are
    stored under the normalized domain, and that projection is many-to-one —
    it has no inverse. So the map is built in the one direction that is
    well-defined: project each circuit name forward and group. A stored domain
    is drainable only when every circuit projecting onto it is CLOSED; a domain
    no circuit projects onto is *unknown*, therefore not drainable.
    """

    by_domain: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    store_refreshed: bool = True

    def drop_reason(self, domain: str) -> str | None:
        """Why this domain may not be drained, or None when it may."""
        circuits = self.by_domain.get(domain)
        if not circuits:
            return DROP_REASON_NO_CIRCUIT_PROJECTS
        for _name, state in circuits:
            if state != _CLOSED_STATE:
                return DROP_REASON_CIRCUIT_OPEN
        return None


def build_circuit_projection() -> CircuitProjection:
    """Snapshot the shared circuit store, projected into stored-domain space.

    The read is preceded by a whole-store L2 restore where the repository has
    one: ``get_all_states()`` on the layered repository returns L1 only, and a
    worker's L1 holds what it hydrated at boot plus what that process itself
    touched — so a circuit the web process created afterwards (every
    middleware circuit, for a worker that never served HTTP) is simply absent.

    Nothing on this path raises: the restore reports failure by returning
    False, and both the layered and the plain Redis reads swallow their own
    errors into a partial answer. ``store_refreshed`` therefore carries the
    distinction the caller needs — False means an L2 is configured and could
    not be read, which is a reason to replay nothing this pass rather than to
    drain against state that may be stale.
    """
    from baldur.services.circuit_breaker import get_circuit_breaker_service
    from baldur.utils.domain_validation import resolve_stored_domain

    cb_service = get_circuit_breaker_service()
    repository = cb_service.repository

    store_refreshed = True
    force_sync = getattr(repository, "force_sync_from_l2", None)
    if callable(force_sync) and not force_sync():
        # False means either "no L2 configured" — the single-layer in-memory
        # store, where L1 IS the store — or "the load failed". Only the second
        # is a reason to stop.
        health = getattr(repository, "get_l2_health", None)
        if callable(health) and (health() or {}).get("adapter_type") is not None:
            store_refreshed = False

    by_domain: dict[str, list[tuple[str, str]]] = {}
    for row in cb_service.get_all_states():
        raw_name = row.get("service_name", "")
        by_domain.setdefault(resolve_stored_domain(raw_name), []).append(
            (raw_name, row.get("state", ""))
        )
    return CircuitProjection(by_domain=by_domain, store_refreshed=store_refreshed)


@dataclass
class TrafficHealthStatus:
    """Traffic health status result."""

    is_healthy: bool
    reason: str
    checks: dict[str, bool] = field(default_factory=dict)
    # The circuit snapshot the check built, so the pass that follows reuses it
    # instead of scanning the store a second time.
    circuits: CircuitProjection | None = None

    @classmethod
    def healthy(
        cls,
        checks: dict[str, bool],
        circuits: CircuitProjection | None = None,
    ) -> TrafficHealthStatus:
        """Healthy-status factory."""
        return cls(
            is_healthy=True,
            reason="All checks passed",
            checks=checks,
            circuits=circuits,
        )

    @classmethod
    def unhealthy(
        cls,
        reason: str,
        checks: dict[str, bool],
        circuits: CircuitProjection | None = None,
    ) -> TrafficHealthStatus:
        """Unhealthy-status factory."""
        return cls(is_healthy=False, reason=reason, checks=checks, circuits=circuits)


def check_traffic_health(domain: str | None = None) -> TrafficHealthStatus:  # noqa: C901
    """
    Check the traffic health status.

    Checks:
    1. Circuit Breaker state, read from a fresh snapshot of the shared store
       and projected into the namespace DLQ entries are stored under
    2. Governance (Kill Switch, Emergency Mode)

    There is no Error Budget leg. The gate it consulted resolves to nothing on
    every install, and the ``RuntimeError`` that raised landed in the generic
    handler and fail-opened — so the check reported a pass it had never made.
    Removing it makes this report's coverage what it actually is.

    Args:
        domain: additionally require this domain's circuits to be affirmed
            CLOSED (optional). Without it the snapshot is still built, because
            the caller fans out over domains and needs both the snapshot and
            the store's readability.

    Returns:
        TrafficHealthStatus with is_healthy flag, check results and the
        circuit snapshot the caller reuses.
    """
    checks: dict[str, bool] = {}
    circuits: CircuitProjection | None = None

    # Check 1: Circuit Breaker state
    try:
        circuits = build_circuit_projection()
        if not circuits.store_refreshed:
            checks["circuit_breaker"] = False
            return TrafficHealthStatus.unhealthy(
                reason="Circuit store could not be refreshed from L2",
                checks=checks,
                circuits=circuits,
            )
        drop_reason = circuits.drop_reason(domain) if domain else None
        checks["circuit_breaker"] = drop_reason is None
        if drop_reason is not None:
            return TrafficHealthStatus.unhealthy(
                reason=f"Domain '{domain}' is not drainable: {drop_reason}",
                checks=checks,
                circuits=circuits,
            )
    except ImportError:
        logger.debug("traffic_health.circuitbreakerservice_available_skipping_cb")
        checks["circuit_breaker"] = True  # pass if unavailable
    except Exception as e:
        logger.warning(
            "traffic_health.cb_check_failed",
            error=e,
        )
        checks["circuit_breaker"] = True  # fail-open on exception

    # Check 2: Governance (Kill Switch, Emergency Mode)
    try:
        from baldur.factory.registry import ProviderRegistry
        from baldur.settings.governance import get_governance_settings

        governance_settings = get_governance_settings()
        governance = ProviderRegistry.governance.get().check_all_governance(
            check_kill_switch=True,
            check_emergency=True,
            emergency_min_level=governance_settings.emergency_min_level,
            # Governance's own error-budget gate, distinct from the removed
            # health leg — left off, as it has been on this lane throughout.
            check_error_budget=False,
            operation_name="traffic_aware_replay",
            service_name="TrafficAwareReplayTask",
            domain=domain or "dlq",
            audit_on_block=False,  # batch schedule, so skip audit
        )
        checks["governance"] = governance.allowed

        if not checks["governance"]:
            return TrafficHealthStatus.unhealthy(
                reason=governance.block_message,
                checks=checks,
                circuits=circuits,
            )
    except ImportError:
        logger.debug("traffic_health.governancechecks_available_skipping")
        checks["governance"] = True
    except Exception as e:
        logger.warning(
            "traffic_health.governance_check_failed",
            error=e,
        )
        checks["governance"] = True  # fail-open on exception

    return TrafficHealthStatus.healthy(checks, circuits=circuits)


# =============================================================================
# Traffic-Aware Replay Task
# =============================================================================


class TrafficAwareReplayTask(BaseNotifyingTask):
    """
    Traffic-state-aware DLQ Replay.

    Performs DLQ Replay only when traffic is normal.
    Enabled/disabled according to the traffic_aware_enabled setting in RuntimeConfig.

    Audit record:
    - Records the DLQ_REPLAY event (together with the execution result)

    Schedule: every minute
    Queue: dlq_processing
    Notification: on replay success (ON_SUCCESS)

    Args:
        domain: replay only a specific domain (optional)
        max_items: maximum number of items to replay (optional, RuntimeConfig takes precedence)

    Returns:
        dict: {
            "status": "completed" | "skipped" | "disabled",
            "reason": str,
            "total": int,
            "success": int,
            "failed": int,
            "checks": dict,
        }
    """

    name = "baldur.traffic_aware_replay"

    @property
    def notification_policy(self) -> NotificationPolicy:  # type: ignore[override]
        """Dynamically create the notification_policy from Settings."""
        cooldown = self._get_cooldown_seconds()
        return NotificationPolicy(
            timing=NotificationTiming.AFTER,
            threshold=1,  # notify when 1 or more are replayed
            threshold_field="success",
            default_severity="info",
            cooldown_seconds=cooldown,
        )

    @staticmethod
    def _get_cooldown_seconds() -> int:
        """Look up cooldown_seconds from Settings."""
        try:
            # Keep the default 5 minutes (300s), but allow lookup from settings
            return 300
        except Exception:
            return 300  # default

    def run(
        self,
        domain: str | None = None,
        max_items: int | None = None,
    ) -> dict[str, Any]:
        """
        Run Traffic-Aware Replay.

        1. Load traffic-aware settings from RuntimeConfig
        2. Perform the Traffic Health Check
        3. Run Replay if all checks pass
        4. Record audit

        Args:
            domain: replay only a specific domain
            max_items: maximum number of items to replay

        Returns:
            dict with status, counts, and check results
        """
        logger.info(
            "traffic_aware_replay.starting_check",
            healing_domain=domain,
        )

        task_id = (
            getattr(self.request, "id", None) if hasattr(self, "request") else None
        )

        # 1. Load traffic-aware settings from RuntimeConfig
        config = self._get_replay_automation_config()

        if not config.get("traffic_aware_enabled", False):
            logger.debug("traffic_aware_replay.track_disabled")
            result = {
                "status": "disabled",
                "reason": "Traffic-aware replay is disabled in RuntimeConfig",
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": {},
            }
            self._log_audit(result, domain, task_id)
            return result

        effective_max_items = max_items or config.get("traffic_aware_max_items", 30)

        # 2. Traffic Health Check
        health_status = check_traffic_health(domain)

        if not health_status.is_healthy:
            logger.info(
                "traffic_aware_replay.skipping_traffic_unhealthy",
                health_status=health_status.reason,
            )
            result = {
                "status": "skipped",
                "reason": health_status.reason,
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": health_status.checks,
            }
            self._log_audit(result, domain, task_id)
            return result

        # 3. Run Replay
        logger.info(
            "traffic_aware_replay.health_ok_executing_replay",
            effective_max_items=effective_max_items,
        )

        try:
            replay_result: dict[str, Any] = dict(
                self._execute_replay(
                    domain, effective_max_items, health_status.circuits
                )
            )
            result = replay_result

            logger.info(
                "traffic_aware_replay.completed",
                replay_total=result["total"],
                success=result["success"],
                failed=result["failed"],
            )

            final_result = {
                "status": "completed",
                "reason": "Replay executed successfully",
                "total": result["total"],
                "success": result["success"],
                "failed": result["failed"],
                "checks": health_status.checks,
            }
            self._log_audit(final_result, domain, task_id)
            return final_result

        except Exception as e:
            logger.exception(
                "traffic_aware_replay.replay_failed",
                error=e,
            )
            error_result = {
                "status": "error",
                "reason": str(e),
                "total": 0,
                "success": 0,
                "failed": 0,
                "checks": health_status.checks,
            }
            self._log_audit(error_result, domain, task_id, error_message=str(e))
            return error_result

    def _log_audit(
        self,
        result: dict[str, Any],
        domain: str | None,
        task_id: str | None,
        error_message: str | None = None,
    ) -> None:
        """Audit log entry recording."""
        log_traffic_aware_replay_audit(
            domain=domain,
            status=result.get("status", "unknown"),
            total=result.get("total", 0),
            success_count=result.get("success", 0),
            failed_count=result.get("failed", 0),
            skipped_reason=(
                result.get("reason") if result.get("status") == "skipped" else None
            ),
            health_checks=result.get("checks"),
            error_message=error_message,
            task_id=task_id,
        )

    def _get_replay_automation_config(self) -> dict[str, Any]:
        """Load the replay_automation settings from RuntimeConfig.

        Absent (no PRO RuntimeConfigManager) is the OSS-normal state — DEBUG at
        most once per task instance; a read failure is genuinely abnormal —
        WARNING every occurrence. Uses the public ``get_config`` accessor,
        never the private getter.
        """
        from baldur.factory.registry import ProviderRegistry

        try:
            manager = ProviderRegistry.runtime_config_manager.safe_get()
            if manager is None:
                if not getattr(self, "_runtime_config_absent_logged", False):
                    logger.debug("traffic_aware_replay.runtime_config_absent")
                    self._runtime_config_absent_logged = True
                return {}
            return manager.get_config("replay_automation")
        except Exception as e:
            logger.warning(
                "traffic_aware_replay.runtime_config_read_failed",
                error=e,
            )
            return {}

    def _execute_replay(
        self,
        domain: str | None,
        max_items: int,
        circuits: CircuitProjection | None = None,
    ) -> dict[str, int]:
        """Perform the actual replay via ReplayService.

        With a domain named, this is one scoped batch — the health check has
        already affirmed that domain's circuits.

        Without one, the lane chooses its domains before it selects entries.
        The alternative — one ``replay_batch(domain=None)``, which is what the
        shipped Beat entry produces — selects across every domain with the
        circuit check skipped entirely. Pushing the filter into ``replay_batch``
        is not available either: that method also serves the operator console,
        and a manual replay must not be silently narrowed by circuit state.
        """
        try:
            from baldur.interfaces.repositories import ResolutionTrigger
            from baldur.services.replay_service import ReplayService

            service = ReplayService()
            if domain is not None:
                return self._replay_one_domain(
                    service, domain, max_items, ResolutionTrigger.TRAFFIC_AWARE
                )

            drainable = self._select_drainable_domains(service, circuits)
            if not drainable:
                return {"total": 0, "success": 0, "failed": 0}

            totals = {"total": 0, "success": 0, "failed": 0}
            for target, quota in self._split_across_domains(drainable, max_items):
                counts = self._replay_one_domain(
                    service, target, quota, ResolutionTrigger.TRAFFIC_AWARE
                )
                for key in totals:
                    totals[key] += counts[key]
            return totals
        except ImportError as err:
            logger.exception("traffic_aware_replay.replayservice_available")
            raise RuntimeError("ReplayService not available") from err

    @staticmethod
    def _replay_one_domain(
        service: Any,
        domain: str | None,
        max_items: int,
        trigger: Any,
    ) -> dict[str, int]:
        """One scoped batch replay, reduced to the three counts this task reports."""
        batch_result = service.replay_batch(
            domain=domain,
            max_items=max_items,
            trigger=trigger,
        )
        return {
            "total": batch_result.total,
            "success": batch_result.success_count,
            "failed": batch_result.failed_count,
        }

    @staticmethod
    def _select_drainable_domains(
        service: Any,
        circuits: CircuitProjection | None,
    ) -> list[str]:
        """Domains with pending work whose circuits are affirmed CLOSED.

        The enumeration is a fail-open partial primitive by documentation — a
        Redis below 7.0 falls back to a bounded scan, and degraded mode buckets
        in memory — so this claims only "the pending domains it can see", never
        "everything pending". A domain it misses is drained on a later pass.

        Every drop is logged with its domain and its reason. Without that, a
        domain no circuit projects onto — the permanent exclusion — is
        indistinguishable from a domain with nothing to drain.
        """
        if circuits is None:
            return []
        facets = service.repository.get_facet_counts(status="pending")
        drainable: list[str] = []
        for candidate in sorted((facets or {}).get("by_domain", {})):
            drop_reason = circuits.drop_reason(candidate)
            if drop_reason is None and not has_replay_handler(candidate):
                # An unregistered domain still gets a handler — one whose
                # replay always fails — so replaying it would burn a retry per
                # entry every minute and walk the domain to requires_review.
                drop_reason = DROP_REASON_NO_REPLAY_HANDLER
            if drop_reason is not None:
                logger.debug(
                    "traffic_aware_replay.domain_skipped",
                    healing_domain=candidate,
                    reason=drop_reason,
                )
                continue
            drainable.append(candidate)
        return drainable

    @staticmethod
    def _split_across_domains(
        domains: list[str], max_items: int
    ) -> list[tuple[str, int]]:
        """Share the pass budget across domains, rotating which one leads.

        The same divmod fairness rule the circuit-close sweep uses for its
        lanes. When there are more domains than items the base share is 0 and
        the tail gets nothing — which, on a lane that runs every minute, would
        starve the same tail forever. Rotating the starting index by the
        wall-clock minute gives every domain the head within ``len(domains)``
        passes. Keyed on the clock rather than on a counter because the lane
        runs on whichever worker picks the message up, so process-local state
        would restart per worker and re-create the starvation it removes.
        """
        base, extra = divmod(max_items, len(domains))
        start = int(time.time() // 60) % len(domains)
        rotated = domains[start:] + domains[:start]
        return [
            (name, base + (1 if i < extra else 0))
            for i, name in enumerate(rotated)
            if base + (1 if i < extra else 0) > 0
        ]

    def _get_severity(self, result: dict[str, Any]) -> str:
        """Determine the severity based on the result."""
        status = result.get("status", "")
        if status == "error" or result.get("failed", 0) > result.get("success", 0):
            return "warning"
        return "info"

    def _get_summary_message(self, result: dict[str, Any]) -> str:
        """Generate the notification message."""
        status = result.get("status", "")

        if status == "disabled":
            return "⏸️ Traffic-aware replay disabled - skipped"
        if status == "skipped":
            return f"⏭️ Traffic-Aware Replay skipped: {result.get('reason', '')}"
        if status == "error":
            return f"❌ Traffic-Aware Replay error: {result.get('reason', '')}"
        if status == "completed":
            total = result.get("total", 0)
            success = result.get("success", 0)
            failed = result.get("failed", 0)
            if total == 0:
                return "✅ Traffic-Aware Replay completed - no pending items"
            return f"✅ Traffic-Aware Replay: {success}/{total} done, {failed} failed"

        return "Traffic-Aware Replay completed"


# =============================================================================
# Task Registry for Celery
# =============================================================================


# Task list (used by register_with_celery)
TRAFFIC_AWARE_TASKS = [
    TrafficAwareReplayTask,
]


def register_traffic_aware_tasks_with_celery(app) -> None:
    """Register the Traffic-Aware tasks with the Celery app."""
    for task_class in TRAFFIC_AWARE_TASKS:
        # Mix in app.Task like the intelligence and compliance registrars do:
        # a bare BaseNotifyingTask instance is not a Celery task, so
        # register_task fails on the missing bind().
        wrapped = type(
            task_class.__name__,
            (task_class, app.Task),
            {"name": task_class.name},
        )
        app.register_task(wrapped())
        logger.debug(
            "cell_registry.bulkheads_registered",
            task_class=task_class.name,
        )


def get_traffic_aware_beat_schedule() -> dict[str, Any]:
    """
    Return the Traffic-Aware Replay Beat schedule.

    Returns:
        dict: Celery Beat schedule configuration
    """
    from celery.schedules import crontab

    return {
        # Traffic-Aware Replay - every minute
        "traffic-aware-replay": {
            "task": "baldur.traffic_aware_replay",
            "schedule": crontab(minute="*"),  # every minute
            "options": {"queue": "dlq_processing"},
            "kwargs": {},  # dynamically loaded from RuntimeConfig
        },
    }


__all__ = [
    "CircuitProjection",
    "TrafficHealthStatus",
    "build_circuit_projection",
    "check_traffic_health",
    "TrafficAwareReplayTask",
    "TRAFFIC_AWARE_TASKS",
    "register_traffic_aware_tasks_with_celery",
    "get_traffic_aware_beat_schedule",
]
