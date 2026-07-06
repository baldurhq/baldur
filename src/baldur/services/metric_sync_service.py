"""
Metric Sync Service - metric synchronization service layer.

Manual synchronization service for non-invasive Drift management.
Eliminates periodic DB polling and accepts only explicit operator requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from baldur.adapters.metrics.factory import get_metric_adapter
from baldur.metrics.reconciler import (
    MetricReconciler,
    get_reconciler,
)
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.adapters.metrics.base import MetricSourceAdapter

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================


def _layered_metrics_settings():
    """Read MetricsSettings via the layered provider (686 D1/D5) so a console
    edit of the metrics domain's drift thresholds takes effect; env base when no
    RuntimeConfigManager is registered."""
    from baldur.settings.layered_provider import get_layered_settings
    from baldur.settings.metrics import MetricsSettings

    return get_layered_settings(MetricsSettings, "metrics")


class _DriftThresholdsMeta(type):
    """Metaclass for lazy Settings-backed drift thresholds."""

    @property
    def WARNING(cls) -> float:
        return _layered_metrics_settings().drift_warning_threshold * 100

    @property
    def CRITICAL(cls) -> float:
        return _layered_metrics_settings().drift_critical_threshold * 100

    @property
    def INCIDENT(cls) -> float:
        return _layered_metrics_settings().drift_incident_threshold * 100


class DriftThresholds(metaclass=_DriftThresholdsMeta):
    """Drift severity thresholds (percent). Loaded from MetricsSettings."""


# =============================================================================
# Service Layer
# =============================================================================


class MetricSyncService:
    """
    Metric synchronization service.

    Wraps the Reconciler and shapes its results into the form needed by the API.
    """

    def __init__(
        self,
        reconciler: MetricReconciler | None = None,
        adapter: MetricSourceAdapter | None = None,
    ):
        self.reconciler = reconciler or get_reconciler(adapter)
        self.adapter = adapter or get_metric_adapter()

    def sync_metrics(
        self,
        domains: list[str] | None = None,
        dry_run: bool = False,
        actor: str = "unknown",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        Run metric synchronization.

        Args:
            domains: List of domains to synchronize (None for all)
            dry_run: If True, only generate a report
            actor: Synchronization actor
            reason: Synchronization reason

        Returns:
            Synchronization result dictionary
        """
        now = utc_now()

        # Capture current state (before sync)
        before_state = self._capture_current_state(domains)

        if dry_run:
            # Dry run: report only, no actual synchronization
            after_state = self._get_actual_state(domains)
            results = self._build_results(before_state, after_state, domains)
            summary = self._calculate_summary(results)

            return {
                "status": "dry_run",
                "synced_at": now.isoformat(),
                "actor": actor,
                "dry_run": True,
                "results": results,
                "summary": summary,
            }

        # Perform actual synchronization
        if domains:
            for domain in domains:
                self.reconciler.sync_domain_gauges(domain)
        else:
            self.reconciler.sync_all_gauges()

        # Post-sync state
        after_state = self._get_actual_state(domains)
        results = self._build_results(before_state, after_state, domains)
        summary = self._calculate_summary(results)

        # Audit logging
        self._log_sync_action(actor, domains, dry_run, reason, summary)

        return {
            "status": "completed",
            "synced_at": now.isoformat(),
            "actor": actor,
            "dry_run": False,
            "results": results,
            "summary": summary,
        }

    def get_drift_report(self) -> dict[str, Any]:
        """
        Read the current Drift state (read-only).

        Performs DB queries but does not modify Gauge values.

        Returns:
            Drift report dictionary
        """
        now = utc_now()

        # In-memory state
        in_memory_state = self._capture_current_state(None)

        # Actual DB state
        actual_state = self._get_actual_state(None)

        # Drift calculation
        metrics = self._calculate_drift_metrics(in_memory_state, actual_state)
        max_drift_percent = self._get_max_drift_percent(metrics)
        overall_health = self._classify_health(max_drift_percent)
        recommendation = self._get_recommendation(overall_health)

        return {
            "generated_at": now.isoformat(),
            "metrics": metrics,
            "overall_health": overall_health,
            "max_drift_percent": max_drift_percent,
            "recommendation": recommendation,
        }

    def _capture_current_state(
        self, domains: list[str] | None
    ) -> dict[str, dict[str, Any]]:
        """Capture current in-memory (Gauge) state."""
        result: dict[str, dict[str, Any]] = {
            "dlq_pending": {},
            "circuit_breaker": {},
            "retry_rate": {},
        }

        target_domains = domains or self._get_all_domains()

        # Read current in-memory Gauge values via the cross-backend recorder
        # read accessor (works on both prometheus and OTEL backends).
        try:
            from baldur.metrics.prometheus import get_metrics

            metrics = get_metrics()
            recorder = getattr(metrics, "dlq", None)

            if recorder is not None:
                for domain in target_domains:
                    try:
                        result["dlq_pending"][domain] = recorder.get_pending_count(
                            domain
                        )
                    except Exception as e:
                        # Sibling-parity with _get_actual_state: surface read
                        # failures instead of silently masking them as a
                        # drift=0 (false-healthy).
                        logger.warning(
                            "metric_sync.dlq_gauge_read_failed",
                            healing_domain=domain,
                            error=e,
                        )
                        result["dlq_pending"][domain] = 0
        except ImportError:
            # If the prometheus module is unavailable, assume 0
            for domain in target_domains:
                result["dlq_pending"][domain] = 0

        return result

    def _get_actual_state(self, domains: list[str] | None) -> dict[str, dict[str, Any]]:
        """Query the actual state from the DB."""
        result: dict[str, dict[str, Any]] = {
            "dlq_pending": {},
            "circuit_breaker": {},
            "retry_rate": {},
        }

        target_domains = domains or self._get_all_domains()

        for domain in target_domains:
            try:
                result["dlq_pending"][domain] = self.adapter.get_dlq_pending_count(
                    domain
                )
            except Exception as e:
                logger.warning(
                    "metric_sync.dlq_pending_get_failed",
                    healing_domain=domain,
                    error=e,
                )
                result["dlq_pending"][domain] = 0

            try:
                result["retry_rate"][domain] = self.adapter.get_retry_success_rate(
                    domain
                )
            except Exception as e:
                logger.warning(
                    "metric_sync.retry_rate_get_failed",
                    healing_domain=domain,
                    error=e,
                )
                result["retry_rate"][domain] = 0.0

        return result

    def _get_all_domains(self) -> list[str]:
        """Return the list of all registered domains."""
        from baldur.metrics.registry import get_registered_domains

        return get_registered_domains()

    def _build_results(
        self,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        domains: list[str] | None,
    ) -> dict[str, dict[str, Any]]:
        """Build synchronization results."""
        results: dict[str, dict[str, Any]] = {}
        target_domains = domains or self._get_all_domains()

        for domain in target_domains:
            before_dlq = before.get("dlq_pending", {}).get(domain, 0)
            after_dlq = after.get("dlq_pending", {}).get(domain, 0)

            results[domain] = {
                "dlq_pending": {
                    "before": before_dlq,
                    "after": after_dlq,
                    "drift": after_dlq - before_dlq,
                }
            }

        return results

    def _calculate_summary(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Compute the synchronization summary."""
        total_drifts = 0
        max_drift_percent = 0.0

        for _domain, domain_result in results.items():
            dlq = domain_result.get("dlq_pending", {})
            drift = abs(dlq.get("drift", 0))
            before = dlq.get("before", 0)

            if drift > 0:
                total_drifts += 1
                if before > 0:
                    drift_pct = (drift / before) * 100
                    max_drift_percent = max(max_drift_percent, drift_pct)
                elif dlq.get("after", 0) > 0:
                    max_drift_percent = max(max_drift_percent, 100.0)

        return {
            "total_drifts_detected": total_drifts,
            "total_drifts_corrected": total_drifts,  # After sync, all are corrected
            "max_drift_percent": round(max_drift_percent, 2),
        }

    def _calculate_drift_metrics(
        self,
        in_memory: dict[str, dict[str, Any]],
        actual: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Compute Drift metrics."""
        metrics: dict[str, dict[str, Any]] = {
            "dlq_pending_count": {},
        }

        for domain in in_memory.get("dlq_pending", {}):
            in_mem = in_memory["dlq_pending"].get(domain, 0)
            act = actual.get("dlq_pending", {}).get(domain, 0)
            drift = act - in_mem

            # Drift percent calculation
            if in_mem > 0:
                drift_percent = abs(drift / in_mem) * 100
            elif act > 0:
                drift_percent = 100.0
            else:
                drift_percent = 0.0

            metrics["dlq_pending_count"][domain] = {
                "in_memory": in_mem,
                "actual": act,
                "drift": drift,
                "drift_percent": round(drift_percent, 2),
                "is_critical": drift_percent >= DriftThresholds.CRITICAL,
            }

        return metrics

    def _get_max_drift_percent(self, metrics: dict[str, dict[str, Any]]) -> float:
        """Extract the maximum Drift percent."""
        max_pct = 0.0

        for _metric_type, domains in metrics.items():
            for _domain, info in domains.items():
                pct = info.get("drift_percent", 0.0)
                max_pct = max(max_pct, pct)

        return round(max_pct, 2)

    def _classify_health(self, max_drift_percent: float) -> str:
        """Classify health based on Drift."""
        if max_drift_percent >= DriftThresholds.INCIDENT:
            return "incident"
        if max_drift_percent >= DriftThresholds.CRITICAL:
            return "critical"
        if max_drift_percent >= DriftThresholds.WARNING:
            return "warning"
        return "healthy"

    def _get_recommendation(self, health: str) -> str:
        """Recommended action based on health."""
        recommendations = {
            "healthy": "",
            "warning": "Drift detected. monitoring is recommended.",
            "critical": "Critical Drift. Recommend running POST /api/baldur/metrics/sync/ to sync immediately.",
            "incident": "Incident-level Drift. Sync immediately and check for event loss.",
        }
        return recommendations.get(health, "")

    def _log_sync_action(
        self,
        actor: str,
        domains: list[str] | None,
        dry_run: bool,
        reason: str | None,
        summary: dict[str, Any],
    ) -> None:
        """Audit logging."""
        try:
            from baldur.audit.logger import (
                AuditConfigChangeEvent,
                AuditLogger,
                ConfigAuditAction,
            )

            audit_logger = AuditLogger.get_instance()
            event = AuditConfigChangeEvent(
                config_type="metric_sync",
                config_key="manual_sync",
                action=ConfigAuditAction.APPLY,
                old_value=None,
                new_value={
                    "domains": domains or "all",
                    "dry_run": dry_run,
                    "drifts_corrected": summary.get("total_drifts_corrected", 0),
                },
                reason=reason or "Manual metric synchronization",
                user=actor,
                source="api",
                metadata={
                    "category": "metric_reconciliation",
                    "summary": summary,
                },
            )
            log_fn = getattr(audit_logger, "log", None)
            if callable(log_fn):
                log_fn(event)
            logger.info(
                "metric_sync.audit_logged",
                actor_id=actor,
                summary=summary,
            )
        except Exception as e:
            # Continue syncing even if audit fails
            logger.warning(
                "metric_sync.log_audit_failed",
                error=e,
            )


# =============================================================================
# Singleton Service Instance
# =============================================================================

from baldur.utils.singleton import make_singleton_factory

get_metric_sync_service, configure_metric_sync_service, reset_metric_sync_service = (
    make_singleton_factory("metric_sync_service", MetricSyncService)
)

__all__ = [
    "DriftThresholds",
    "MetricSyncService",
    "get_metric_sync_service",
    "configure_metric_sync_service",
    "reset_metric_sync_service",
]
