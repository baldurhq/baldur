"""
Shadow Logger Module

Records state changes locally during an L2 outage.
The Shadow Log is used for resynchronization after L2 recovery and for
forensic analysis.

Version: 6.4.0 - Added Drift Detection metrics
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from baldur.utils.time import utc_now

# Drift Detection metrics
try:
    from baldur.metrics.drift_metrics import (
        record_shadow_log_recovered,
        record_shadow_log_sync_failure,
        update_shadow_log_affected_services,
        update_shadow_log_oldest_unsynced_age,
        update_shadow_log_unsynced_count,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False


logger = structlog.get_logger()


@dataclass
class L2SyncFailureRecord:
    """
    L2 sync failure record.

    Records state changes that occurred during an L2 outage to support
    forensic analysis and resynchronization after recovery.
    """

    service_name: str
    intended_state: str
    failure_time: datetime
    error_message: str
    l1_state_at_failure: str
    adapter_type: str = "unknown"
    operation: str = "sync"  # sync, update, delete
    synced_after_recovery: bool = False
    recovery_time: datetime | None = None


class ShadowLogger:
    """
    Records state changes locally during an L2 outage.

    The Shadow Log captures every state change that occurs while L2 is
    in a failed state into memory, supporting resynchronization after L2
    recovery and forensic analysis.

    Thread-safe implementation safe under concurrent access.
    """

    _instance: ShadowLogger | None = None
    _lock_class = None

    def __new__(cls) -> ShadowLogger:
        """Singleton pattern."""
        if cls._instance is None:
            cls._lock_class = threading.Lock()
            with cls._lock_class:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init()
                    cls._instance = instance
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for test isolation."""
        cls._instance = None

    def _init(self) -> None:
        """Initialize shadow logger."""
        self._failure_log: list[L2SyncFailureRecord] = []
        self._lock = threading.RLock()
        self._max_entries = 1000  # Default; can be changed at runtime

    def set_max_entries(self, max_entries: int) -> None:
        """Set maximum entries to keep."""
        with self._lock:
            self._max_entries = max_entries
            # Trim if over limit
            if len(self._failure_log) > max_entries:
                self._failure_log = self._failure_log[-max_entries:]

    def record_sync_failure(
        self,
        service_name: str,
        intended_state: str,
        error: Exception,
        adapter_type: str = "unknown",
        operation: str = "sync",
    ) -> None:
        """
        Record an L2 sync failure.

        Args:
            service_name: Service name
            intended_state: The state we tried to sync
            error: Raised exception
            adapter_type: L2 adapter type (redis, django, etc.)
            operation: Operation kind (sync, update, delete)
        """
        with self._lock:
            record = L2SyncFailureRecord(
                service_name=service_name,
                intended_state=intended_state,
                failure_time=utc_now(),
                error_message=str(error),
                l1_state_at_failure=intended_state,
                adapter_type=adapter_type,
                operation=operation,
            )
            self._failure_log.append(record)

            # Trim old entries if over limit
            if len(self._failure_log) > self._max_entries:
                self._failure_log = self._failure_log[-self._max_entries :]

            # Record Drift Detection metrics
            if HAS_DRIFT_METRICS:
                record_shadow_log_sync_failure(adapter_type, operation)
                self._update_drift_metrics()

            # Audit record
            self._record_audit_event(
                event_type="SHADOW_LOG_SYNC_FAILED",
                service_name=service_name,
                details={
                    "intended_state": intended_state,
                    "error_message": str(error),
                    "adapter_type": adapter_type,
                    "operation": operation,
                },
            )

            logger.warning(
                "shadow_log.sync_failed",
                service_name=service_name,
                intended_state=intended_state,
                adapter_type=adapter_type,
                error=error,
            )

    def get_unsynced_records(self) -> list[L2SyncFailureRecord]:
        """List records that have not yet been synced."""
        with self._lock:
            return [r for r in self._failure_log if not r.synced_after_recovery]

    def get_all_records(self) -> list[L2SyncFailureRecord]:
        """List all records."""
        with self._lock:
            return list(self._failure_log)

    def mark_as_synced(self, service_name: str) -> int:
        """
        Mark records as synced after recovery.

        Args:
            service_name: Service name

        Returns:
            Number of records marked
        """
        count = 0
        recovery_time = utc_now()
        with self._lock:
            for record in self._failure_log:
                if (
                    record.service_name == service_name
                    and not record.synced_after_recovery
                ):
                    record.synced_after_recovery = True
                    record.recovery_time = recovery_time
                    count += 1
        if count > 0:
            # Record Drift Detection metrics
            if HAS_DRIFT_METRICS:
                record_shadow_log_recovered(service_name, count)
                with self._lock:
                    self._update_drift_metrics()
            # Audit record
            self._record_audit_event(
                event_type="SHADOW_LOG_RECOVERED",
                service_name=service_name,
                details={
                    "recovered_count": count,
                    "recovery_time": recovery_time.isoformat(),
                },
            )
            logger.info(
                "shadow_log.marked_records_synced",
                synced_count=count,
                service_name=service_name,
            )
        return count

    def mark_all_as_synced(self) -> int:
        """Mark every unsynced record as synced."""
        count = 0
        with self._lock:
            now_time = utc_now()
            for record in self._failure_log:
                if not record.synced_after_recovery:
                    record.synced_after_recovery = True
                    record.recovery_time = now_time
                    count += 1
            # Update Drift Detection metrics
            if HAS_DRIFT_METRICS and count > 0:
                self._update_drift_metrics()
        if count > 0:
            logger.info(
                "shadow_log.marked_all_records_synced",
                synced_count=count,
            )
        return count

    def _update_drift_metrics(self) -> None:
        """
        Update Drift Detection metrics.

        Note: this method must be called while _lock is already held.
        """
        if not HAS_DRIFT_METRICS:
            return

        unsynced = [r for r in self._failure_log if not r.synced_after_recovery]
        services = {r.service_name for r in self._failure_log}

        # Number of unsynced records
        update_shadow_log_unsynced_count(len(unsynced))

        # Number of affected services
        update_shadow_log_affected_services(len(services))

        # Age of the oldest unsynced record
        if unsynced:
            oldest = min(r.failure_time for r in unsynced)
            age_seconds = (utc_now() - oldest).total_seconds()
            update_shadow_log_oldest_unsynced_age(age_seconds)
        else:
            update_shadow_log_oldest_unsynced_age(0)

    def get_stats(self) -> dict[str, Any]:
        """Read Shadow Log statistics."""
        with self._lock:
            unsynced = [r for r in self._failure_log if not r.synced_after_recovery]
            services = {r.service_name for r in self._failure_log}

            # Update Drift Detection metrics
            if HAS_DRIFT_METRICS:
                self._update_drift_metrics()

            return {
                "total_records": len(self._failure_log),
                "unsynced_count": len(unsynced),
                "affected_services": list(services),
                "max_entries": self._max_entries,
                "oldest_record": (
                    self._failure_log[0].failure_time.isoformat()
                    if self._failure_log
                    else None
                ),
                "newest_record": (
                    self._failure_log[-1].failure_time.isoformat()
                    if self._failure_log
                    else None
                ),
            }

    def clear(self) -> None:
        """Clear all records (for testing)."""
        with self._lock:
            self._failure_log.clear()
            self._max_entries = 1000

    def analyze_l2_failures(self) -> dict[str, Any]:
        """
        Analyze state changes during the L2 outage window.

        Integrates with the Forensic Advisor to analyze state changes
        that occurred during the L2 outage as a timeline.

        Returns:
            Analysis result dictionary
        """
        with self._lock:
            unsynced = [r for r in self._failure_log if not r.synced_after_recovery]
            all_records = list(self._failure_log)

        if not all_records:
            return {
                "unsynced_count": 0,
                "affected_services": [],
                "failure_timeline": [],
                "by_adapter": {},
                "by_operation": {},
                "time_range": None,
                "recommendations": ["No L2 failures recorded."],
            }

        # Per-service aggregation
        affected_services = list({r.service_name for r in unsynced})

        # Build the timeline
        sorted_records = sorted(all_records, key=lambda x: x.failure_time)
        failure_timeline = [
            {
                "service": r.service_name,
                "state": r.intended_state,
                "time": r.failure_time.isoformat(),
                "error": r.error_message,
                "adapter": r.adapter_type,
                "operation": r.operation,
                "synced": r.synced_after_recovery,
            }
            for r in sorted_records
        ]

        # Per-adapter statistics
        by_adapter: dict[str, int] = {}
        for r in all_records:
            by_adapter[r.adapter_type] = by_adapter.get(r.adapter_type, 0) + 1

        # Per-operation statistics
        by_operation: dict[str, int] = {}
        for r in all_records:
            by_operation[r.operation] = by_operation.get(r.operation, 0) + 1

        # Time range
        time_range = None
        if sorted_records:
            time_range = {
                "start": sorted_records[0].failure_time.isoformat(),
                "end": sorted_records[-1].failure_time.isoformat(),
                "duration_seconds": (
                    sorted_records[-1].failure_time - sorted_records[0].failure_time
                ).total_seconds(),
            }

        # Generate recommendations
        recommendations = self._generate_recommendations(
            unsynced_count=len(unsynced),
            affected_services=affected_services,
            by_adapter=by_adapter,
            total_records=len(all_records),
        )

        return {
            "unsynced_count": len(unsynced),
            "affected_services": affected_services,
            "failure_timeline": failure_timeline,
            "by_adapter": by_adapter,
            "by_operation": by_operation,
            "time_range": time_range,
            "recommendations": recommendations,
        }

    def _generate_recommendations(
        self,
        unsynced_count: int,
        affected_services: list[str],
        by_adapter: dict[str, int],
        total_records: int,
    ) -> list[str]:
        """Generate recommendations."""
        recommendations = []

        if unsynced_count > 0:
            recommendations.append(
                f"Sync {unsynced_count} unsynced records to L2 using "
                f"POST /api/baldur/l2-storage/sync/to-l2"
            )

        if len(affected_services) > 3:
            recommendations.append(
                f"Multiple services affected ({len(affected_services)}). "
                f"Consider checking L2 infrastructure health."
            )

        if total_records > 100:
            recommendations.append(
                "High failure count detected. Consider increasing L2 timeout "
                "or optimizing L2 storage performance."
            )

        # Per-adapter recommendations
        for adapter, count in by_adapter.items():
            if count > 50:
                recommendations.append(
                    f"Adapter '{adapter}' has {count} failures. "
                    f"Check {adapter} connectivity and performance."
                )

        if not recommendations:
            recommendations.append("No critical issues detected.")

        return recommendations

    def get_records_by_service(self, service_name: str) -> list[L2SyncFailureRecord]:
        """List failure records for a given service."""
        with self._lock:
            return [r for r in self._failure_log if r.service_name == service_name]

    def get_records_by_time_range(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[L2SyncFailureRecord]:
        """List failure records within the given time range."""
        with self._lock:
            return [
                r for r in self._failure_log if start_time <= r.failure_time <= end_time
            ]

    def _record_audit_event(
        self,
        event_type: str,
        service_name: str,
        details: dict[str, Any],
    ) -> None:
        """
        Record an Audit event.

        Audit integration improvements:
        - Direct _write_to_wal() call automatically combines ActorContext/TraceContext
        - Enables tracking of "which operator's operation caused a sync failure during L2 outage"
        """
        try:
            from baldur_pro.services.audit.base import _write_to_wal

            _write_to_wal(
                event_type=event_type,
                source="ShadowLogger",
                details={
                    "service_name": service_name,
                    **details,
                },
            )
            # actor_id, actor_roles, and trace_id are included automatically
        except ImportError:
            # When _write_to_wal is unavailable: fall back to the logger
            logger.debug("shadow_logger.audit_recording_skipped_available")
        except Exception as e:
            # Audit failures must not interfere with the main logic
            logger.debug(
                "shadow_logger.audit_recording_failed",
                error=e,
            )


def get_shadow_logger() -> ShadowLogger:
    """Get the singleton ShadowLogger instance."""
    return ShadowLogger()
