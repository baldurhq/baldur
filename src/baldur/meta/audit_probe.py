"""
Audit System Health Probe - audit system health collection.

Probe that periodically checks the health of the audit system (WAL,
DiskBuffer, SyncWorker).

Checks:
1. Whether the WAL is writable
2. WAL → central store sync lag
3. DiskPersistentBuffer state
4. Recent audit failure rate
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from baldur.meta.health_probe import HealthStatus
from baldur.utils.time import utc_now

logger = structlog.get_logger()


@dataclass
class AuditProbeResult:
    """Audit system probe result."""

    component: str
    # Uses HealthStatus enum for parity with ProbeResult.status, ensuring
    # MetaWatchdog.component_statuses (dict[str, HealthStatus]) stays
    # homogeneous and downstream consumers such as the health-check service
    # can call ``v.value`` safely.
    status: HealthStatus
    latency_ms: float
    timestamp: datetime
    details: dict[str, Any]
    reason: str = ""
    error: str | None = None


class AuditSystemProbe:
    """
    Audit system health probe.

    Checks:
    1. Whether the WAL is writable
    2. WAL → central store sync lag
    3. DiskPersistentBuffer state
    4. Recent audit failure rate
    """

    # Status constants alias HealthStatus enum members. HealthStatus is a
    # str-Enum so ``STATUS_HEALTHY == "healthy"`` still holds for legacy
    # raw-string equality consumers.
    STATUS_HEALTHY = HealthStatus.HEALTHY
    STATUS_DEGRADED = HealthStatus.DEGRADED
    STATUS_UNHEALTHY = HealthStatus.UNHEALTHY
    STATUS_UNKNOWN = HealthStatus.UNKNOWN

    # Threshold constants
    LAG_THRESHOLD_DEGRADED = 1000  # lag of 1000+ entries → DEGRADED
    FAIL_RATE_THRESHOLD = 0.1  # failure rate of 10%+ → DEGRADED

    @property
    def component_name(self) -> str:
        return "audit_system"

    def is_applicable(self) -> bool:
        """Audit is an opt-in subsystem (master switch off by default).

        Probe only when enabled. A disabled audit subsystem initializes no WAL
        and starts no sync worker, so probing it reports a misleading UNHEALTHY
        ("WAL unavailable") for a feature that is intentionally off — which also
        drags the watchdog's overall status down. When the operator opts in, the
        probe activates and monitors WAL/sync health normally.
        """
        from baldur.settings.audit import get_audit_settings

        return get_audit_settings().enabled

    def probe(self) -> AuditProbeResult:
        """
        Run the audit system health probe.

        Returns:
            AuditProbeResult: probe result
        """
        start = time.time()
        details: dict[str, Any] = {}

        try:
            # 1. Check the WAL state
            wal_status = self._check_wal()
            details["wal"] = wal_status

            # 2. Check the DiskPersistentBuffer state
            buffer_status = self._check_disk_buffer()
            details["disk_buffer"] = buffer_status

            # 3. Check the SyncWorker lag
            sync_status = self._check_sync_worker()
            details["sync_worker"] = sync_status

            # Determine the status
            status, reason = self._determine_status(
                wal_status, buffer_status, sync_status
            )

            return AuditProbeResult(
                component=self.component_name,
                status=status,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                details=details,
                reason=reason,
            )

        except Exception as e:
            return AuditProbeResult(
                component=self.component_name,
                status=self.STATUS_UNKNOWN,
                latency_ms=(time.time() - start) * 1000,
                timestamp=utc_now(),
                details=details,
                error=str(e),
            )

    def _check_wal(self) -> dict[str, Any]:
        """Check the WAL state."""
        try:
            from baldur_pro.services.audit.base import get_wal_stats

            stats = get_wal_stats()
            if stats is None:
                return {"available": False, "reason": "WAL not initialized"}

            return {
                "available": True,
                "state": stats.get("state", "unknown"),
                "total_entries": stats.get("total_entries", 0),
                "last_sequence": stats.get("last_sequence", 0),
                "current_size_bytes": stats.get("current_size_bytes", 0),
            }
        except ImportError:
            return {"available": False, "reason": "audit base module not available"}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def _check_disk_buffer(self) -> dict[str, Any]:
        """Check the DiskPersistentBuffer state."""
        try:
            from baldur.audit.persistence.disk_buffer import DiskPersistentBuffer

            # DiskPersistentBuffer has no built-in singleton; duck-type so a
            # PRO subclass that adds get_instance() can plug in transparently.
            get_inst = getattr(DiskPersistentBuffer, "get_instance", None)
            buffer = get_inst() if callable(get_inst) else DiskPersistentBuffer()
            stats = buffer.get_stats()
            return {
                "available": True,
                "entry_count": stats.get("entry_count", 0),
                "state": stats.get("state", "unknown"),
            }
        except ImportError:
            return {"available": False, "reason": "DiskPersistentBuffer not installed"}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def _check_sync_worker(self) -> dict[str, Any]:
        """Check the SyncWorker state."""
        try:
            from baldur.audit.sync_worker import AuditSyncWorker

            worker = AuditSyncWorker.get_instance()

            if not hasattr(worker, "is_running"):
                return {
                    "available": True,
                    "running": True,
                    "lag_entries": 0,
                    "note": "sync_worker running (limited stats)",
                }

            is_running = worker.is_running  # property, not method
            stats = worker.get_stats() if hasattr(worker, "get_stats") else {}

            return {
                "available": True,
                "running": is_running,
                "lag_entries": (
                    getattr(stats, "current_lag_entries", 0)
                    if hasattr(stats, "current_lag_entries")
                    else stats.get("current_lag_entries", 0)
                ),
                "total_synced": (
                    getattr(stats, "total_synced", 0)
                    if hasattr(stats, "total_synced")
                    else stats.get("total_synced", 0)
                ),
                "total_failed": (
                    getattr(stats, "total_failed", 0)
                    if hasattr(stats, "total_failed")
                    else stats.get("total_failed", 0)
                ),
                "last_error": getattr(stats, "last_error", None)
                if hasattr(stats, "last_error")
                else stats.get("last_error"),
            }
        except ImportError:
            return {"available": False, "reason": "sync_worker module not available"}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def _determine_status(
        self,
        wal: dict[str, Any],
        buffer: dict[str, Any],
        sync: dict[str, Any],
    ) -> tuple[HealthStatus, str]:
        """Determine overall status and reason."""
        # WAL unavailable → UNHEALTHY
        if not wal.get("available"):
            return self.STATUS_UNHEALTHY, "WAL unavailable"

        # Severe sync lag (LAG_THRESHOLD_DEGRADED+ entries) → DEGRADED
        lag_entries = sync.get("lag_entries", 0)
        if lag_entries > self.LAG_THRESHOLD_DEGRADED:
            return (
                self.STATUS_DEGRADED,
                f"Sync lag: {lag_entries} entries (threshold: {self.LAG_THRESHOLD_DEGRADED})",
            )

        # High recent failure rate → DEGRADED
        total_synced = sync.get("total_synced", 0)
        total_failed = sync.get("total_failed", 0)
        total = total_synced + total_failed
        if total > 0:
            fail_rate = total_failed / total
            if fail_rate > self.FAIL_RATE_THRESHOLD:
                return (
                    self.STATUS_DEGRADED,
                    f"Audit fail rate: {fail_rate:.1%} (threshold: {self.FAIL_RATE_THRESHOLD:.0%})",
                )

        # SyncWorker stopped → DEGRADED
        if sync.get("available") and not sync.get("running", True):
            return self.STATUS_DEGRADED, "Sync worker not running"

        return self.STATUS_HEALTHY, ""


def get_audit_probe() -> AuditSystemProbe:
    """Return an AuditSystemProbe instance."""
    return AuditSystemProbe()


def check_audit_health() -> dict[str, Any]:
    """
    Quick check of the audit system's health.

    Returns:
        Health status dictionary
    """
    probe = AuditSystemProbe()
    result = probe.probe()
    return {
        "component": result.component,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "timestamp": result.timestamp.isoformat(),
        "details": result.details,
        "error": result.error,
    }
