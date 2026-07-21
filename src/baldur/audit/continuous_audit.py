"""
Continuous Audit Recorder - enterprise-audit-style continuous auditing.

Records every automated decision in a tamper-evident way.
No report formatting is provided, only complete raw data.
(Each organization shapes the data into its own format.)

Design Philosophy:
- Complete, accurate raw data recording
- Tamper protection via hash chain
- Query/filter/export capabilities
- Formatting is the user's responsibility
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from baldur.audit.config import AuditConfig
from baldur.audit.integrity import HashChainManager, HashChainVerifier
from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)
from baldur.utils.serialization import fast_dumps_str, fast_loads
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from baldur.audit.checkpoint import (
        CheckpointStorageStrategy,
    )
    from baldur.audit.wal import WALConfig, WriteAheadLog

logger = structlog.get_logger()


class ContinuousAuditRecorder:
    """
    Continuous audit recorder.

    Characteristics:
    - Tamper protection via hash chain
    - Multiple storage backends supported
    - Immediate alerting on compliance violations
    - Raw data query/filter/export
    - No report formatting (users shape it themselves)

    FAIL-OPEN Design Policy:
    --------------------------
    An audit log write failure never blocks business processing.
    - Default: audit failure -> warning log + stdout fallback
    - Optional: fail_open=False enables Fail-Secure mode (PCI-DSS)

    Industry policies:
    - Netflix Zuul: Fail-Open (availability first)
    - Stripe: Fail-Open + retry
    - PCI-DSS: Fail-Secure recommended (availability exceptions allowed)
    - SOC2: Fail-Open allowed (a record of the failure is enough)

    Usage:
        config = AuditConfig.get_default()
        recorder = ContinuousAuditRecorder(
            audit_adapter=FileAuditLogAdapter("logs/audit.jsonl"),
            config=config,
            fail_open=True,  # default: Fail-Open
        )

        recorder.record_auto_tuning(
            parameter="timeout_ms",
            old_value=5000,
            new_value=6000,
            reason="P99 latency increase",
            confidence=0.85,
            metrics_snapshot={"p99_latency_ms": 4200},
            safety_check={"within_bounds": True},
        )
    """

    def __init__(
        self,
        audit_adapter: AuditLogAdapter,
        config: AuditConfig | None = None,
        alert_callback: Callable[[str, dict[str, Any]], None] | None = None,
        state_file: Path | None = None,
        # Fail-Open policy
        fail_open: bool = True,
        fallback_to_stdout: bool = True,
        # WAL integration
        wal_enabled: bool = False,
        wal_config: WALConfig | None = None,
        # Checkpoint Strategy integration
        checkpoint_strategy: CheckpointStorageStrategy | None = None,
        checkpoint_namespace: str = "default",
        # Checkpoint back-pressure settings
        checkpoint_save_interval: int = 10,
        checkpoint_save_max_seconds: float = 30.0,
    ):
        """
        Initialize ContinuousAuditRecorder.

        Args:
            audit_adapter: Audit log storage adapter
            config: Audit settings (loaded from env vars when None)
            alert_callback: Alert callback (channel, data) -> None
            state_file: Hash chain state file path
            fail_open: Fail-Open policy (default: True)
            fallback_to_stdout: Print to stdout on failure (default: True)
            wal_enabled: Enable WAL (default: False)
            wal_config: WAL settings
            checkpoint_strategy: Checkpoint save strategy (when None, the
                default is used if WAL is enabled)
            checkpoint_namespace: Checkpoint namespace
            checkpoint_save_interval: Save a checkpoint every N records
                (default: 10)
            checkpoint_save_max_seconds: Max save interval in seconds
                (default: 30.0)
        """
        self.audit_adapter = audit_adapter
        self.config = config or AuditConfig.get_default()
        self.alert_callback = alert_callback

        # Fail-Open policy
        self._fail_open = fail_open
        self._fallback_to_stdout = fallback_to_stdout
        self._failed_write_count = 0

        # Hash chain manager
        self._hash_manager = HashChainManager(state_file=state_file)
        self._lock = threading.RLock()

        # WAL initialization (optional)
        self._wal_enabled = wal_enabled
        self._wal: WriteAheadLog | None = None

        if wal_config is not None and not wal_enabled:
            # wal_config is consumed only inside the wal_enabled branch, so
            # supplying one without enabling the WAL silently discards it -
            # including wal_dir_operator_set, which is what makes an
            # unwritable operator-chosen directory fail loud.
            logger.warning(
                "continuous_audit.wal_config_ignored",
                wal_dir=wal_config.wal_dir,
            )

        if wal_enabled:
            try:
                from baldur.audit.wal import WALConfig as WALConfigClass
                from baldur.audit.wal import WriteAheadLog

                self._wal = WriteAheadLog(config=wal_config or WALConfigClass())
                logger.info("continuous_audit.wal_enabled")
            except Exception as e:
                logger.warning(
                    "continuous_audit.wal_initialization_failed",
                    error=e,
                )
                self._wal_enabled = False

        # Checkpoint strategy initialization
        self._checkpoint_strategy: CheckpointStorageStrategy | None = (
            checkpoint_strategy
        )
        self._checkpoint_namespace = checkpoint_namespace

        # Checkpoint back-pressure state (caller-responsibility pattern)
        self._checkpoint_save_interval = checkpoint_save_interval
        self._checkpoint_save_max_seconds = checkpoint_save_max_seconds
        self._records_since_checkpoint: int = 0
        self._last_checkpoint_time: float = time.time()

        if checkpoint_strategy is None and wal_enabled:
            # Auto-configure the default strategy when WAL is enabled
            try:
                from baldur.audit.checkpoint import (
                    get_default_checkpoint_strategy,
                )

                self._checkpoint_strategy = get_default_checkpoint_strategy()
                logger.info("continuous_audit.checkpoint_strategy_initialized")
            except Exception as e:
                logger.warning(
                    "continuous_audit.checkpoint_strategy_init_failed",
                    error=e,
                )

        # Environment information
        self._environment = os.environ.get("ENVIRONMENT", "development")
        self._service_name = os.environ.get("SERVICE_NAME", "unknown")
        self._service_version = os.environ.get("SERVICE_VERSION", "unknown")

    # ─────────────────────────────────────────────────────────────
    # Record methods (Auto Tuning)
    # ─────────────────────────────────────────────────────────────

    def record_auto_tuning(
        self,
        parameter: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        confidence: float,
        metrics_snapshot: dict[str, Any],
        safety_check: dict[str, Any],
        actor_id: str = "runtime_feedback_loop",
    ) -> str:
        """
        Record an autonomous adjustment.

        Args:
            parameter: Name of the adjusted parameter
            old_value: Previous value
            new_value: New value
            reason: Reason for the adjustment
            confidence: Confidence level (0.0 ~ 1.0)
            metrics_snapshot: Metrics at decision time
            safety_check: Safety check result
            actor_id: Actor that performed the adjustment

        Returns:
            Audit log ID
        """
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_ADJUSTMENT,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id=actor_id,
            service_name=self._service_name,
            reason=reason,
            details={
                "adjustment_type": "automatic",
                "parameter": parameter,
                "before": {"value": old_value},
                "after": {"value": new_value, "confidence": confidence},
                "reason": reason,
                "metrics_snapshot": metrics_snapshot,
                "safety_check": safety_check,
                "environment": self._environment,
                "service_version": self._service_version,
            },
        )

        audit_id = self._record_with_integrity(entry)

        # Send alert
        self._send_alert(
            "auto_tuning",
            {
                "parameter": parameter,
                "old_value": old_value,
                "new_value": new_value,
                "reason": reason,
            },
        )

        return audit_id

    def record_auto_tuning_rejected(
        self,
        parameter: str,
        requested_value: Any,
        current_value: Any,
        rejection_reason: str,
        safety_bounds: dict[str, Any],
    ) -> str:
        """Autonomous adjustment rejected for exceeding safety bounds."""
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_REJECTED,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id="safety_guard",
            service_name=self._service_name,
            reason=rejection_reason,
            success=False,
            details={
                "parameter": parameter,
                "requested_value": requested_value,
                "current_value": current_value,
                "rejection_reason": rejection_reason,
                "safety_bounds": safety_bounds,
            },
        )

        audit_id = self._record_with_integrity(entry)

        self._send_alert(
            "auto_tuning_rejected",
            {
                "parameter": parameter,
                "requested_value": requested_value,
                "rejection_reason": rejection_reason,
                "severity": "warning",
            },
        )

        return audit_id

    def record_auto_tuning_rollback(
        self,
        parameter: str,
        rolled_back_value: Any,
        target_value: Any,
        rollback_reason: str,
        strategy: str,  # last_known_good, dna_declared, system_defaults
    ) -> str:
        """Record an autonomous adjustment rollback."""
        entry = AuditEntry(
            action=AuditAction.AUTO_TUNING_ROLLBACK,
            target_type="runtime_config",
            target_id=parameter,
            actor_type="system",
            actor_id="auto_rollback_guard",
            service_name=self._service_name,
            reason=rollback_reason,
            details={
                "parameter": parameter,
                "rolled_back_value": rolled_back_value,
                "target_value": target_value,
                "rollback_reason": rollback_reason,
                "recovery_strategy": strategy,
            },
        )

        return self._record_with_integrity(entry)

    # ─────────────────────────────────────────────────────────────
    # Record methods (DNA Drift)
    # ─────────────────────────────────────────────────────────────

    def record_drift_detected(
        self,
        resource_id: str,
        declared: dict[str, Any],
        actual: dict[str, Any],
        drifted_fields: list[str],
        severity: str,  # low, medium, high, critical
    ) -> str:
        """
        Record a DNA drift detection.

        Args:
            resource_id: ID of the resource that drifted
            declared: Value declared in the DNA
            actual: Actual runtime value
            drifted_fields: List of drifted fields
            severity: Severity level
        """
        entry = AuditEntry(
            action=AuditAction.DNA_DRIFT_DETECTED,
            target_type="stage_dna",
            target_id=resource_id,
            actor_type="system",
            actor_id="dna_drift_detector",
            service_name=self._service_name,
            reason=f"Configuration drift detected in {len(drifted_fields)} field(s)",
            details={
                "drift_type": "configuration_mismatch",
                "declared": declared,
                "actual": actual,
                "drifted_fields": drifted_fields,
                "severity": severity,
                "auto_remediation": False,
            },
        )

        audit_id = self._record_with_integrity(entry)

        # Alert according to severity
        if severity in ("high", "critical"):
            self._send_alert(
                "drift_critical",
                {
                    "resource_id": resource_id,
                    "drifted_fields": drifted_fields,
                    "severity": severity,
                },
            )

        return audit_id

    def record_drift_resolved(
        self,
        resource_id: str,
        resolved_fields: list[str],
        resolution_method: str,  # manual, auto_sync, config_update
    ) -> str:
        """Record a DNA drift resolution."""
        entry = AuditEntry(
            action=AuditAction.DNA_DRIFT_RESOLVED,
            target_type="stage_dna",
            target_id=resource_id,
            actor_type="system",
            actor_id="drift_resolver",
            service_name=self._service_name,
            reason=f"Drift resolved via {resolution_method}",
            details={
                "resolved_fields": resolved_fields,
                "resolution_method": resolution_method,
            },
        )

        return self._record_with_integrity(entry)

    # ─────────────────────────────────────────────────────────────
    # Record methods (Compliance)
    # ─────────────────────────────────────────────────────────────

    def record_compliance_check(
        self,
        standards_checked: list[str],
        results: dict[str, Any],
        overall_status: str,  # compliant, compliant_with_warnings, non_compliant
    ) -> str:
        """
        Record a compliance check result.

        Args:
            standards_checked: Standards checked (e.g. ["DORA", "PCI-DSS"])
            results: Per-standard check results
            overall_status: Overall compliance status
        """
        entry = AuditEntry(
            action=AuditAction.COMPLIANCE_CHECK,
            target_type="baldur_system",
            target_id="global",
            actor_type="system",
            actor_id="compliance_checker",
            service_name=self._service_name,
            reason=f"Compliance check: {overall_status}",
            success=overall_status != "non_compliant",
            details={
                "standards_checked": standards_checked,
                "results": results,
                "overall_status": overall_status,
                "checked_at": utc_now().isoformat(),
            },
        )

        audit_id = self._record_with_integrity(entry)

        # Alert on violation
        if overall_status == "non_compliant":
            self._send_alert(
                "compliance_violation",
                {
                    "standards_checked": standards_checked,
                    "results": results,
                    "severity": "critical",
                },
            )

        return audit_id

    def record_compliance_violation(
        self,
        standard: str,
        violation_type: str,
        description: str,
        remediation_required: bool = True,
    ) -> str:
        """Record a compliance violation."""
        entry = AuditEntry(
            action=AuditAction.COMPLIANCE_VIOLATION,
            target_type="compliance",
            target_id=standard,
            actor_type="system",
            actor_id="compliance_checker",
            service_name=self._service_name,
            reason=description,
            success=False,
            details={
                "standard": standard,
                "violation_type": violation_type,
                "description": description,
                "remediation_required": remediation_required,
            },
        )

        audit_id = self._record_with_integrity(entry)

        self._send_alert(
            "compliance_violation",
            {
                "standard": standard,
                "violation_type": violation_type,
                "description": description,
                "severity": "critical",
            },
        )

        return audit_id

    # ─────────────────────────────────────────────────────────────
    # Query methods (Raw Data)
    # ─────────────────────────────────────────────────────────────

    def query(
        self,
        action: AuditAction | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query audit logs (raw data).

        Args:
            action: Action type filter
            target_type: Target type filter
            target_id: Target ID filter
            start_time: Start time
            end_time: End time
            limit: Maximum number of results

        Returns:
            List of audit log dictionaries
        """
        entries = self.audit_adapter.query(
            action=action,
            target_type=target_type,
            target_id=target_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        return [e.to_dict() for e in entries]

    def query_auto_tuning_history(
        self,
        parameter: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query autonomous adjustment history.

        Args:
            parameter: Parameter name filter
            start_time: Start time
            end_time: End time
            limit: Maximum number of results

        Returns:
            List of autonomous adjustment logs
        """
        return self.query(
            action=AuditAction.AUTO_TUNING_ADJUSTMENT,
            target_type="runtime_config",
            target_id=parameter,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    def query_drift_history(
        self,
        resource_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query DNA drift history.

        Returns:
            List of drift detection/resolution logs
        """
        # Query both DNA_DRIFT_DETECTED and DNA_DRIFT_RESOLVED
        detected = self.query(
            action=AuditAction.DNA_DRIFT_DETECTED,
            target_id=resource_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit // 2,
        )

        resolved = self.query(
            action=AuditAction.DNA_DRIFT_RESOLVED,
            target_id=resource_id,
            start_time=start_time,
            end_time=end_time,
            limit=limit // 2,
        )

        # Sort chronologically
        all_entries = detected + resolved
        all_entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

        return all_entries[:limit]

    def query_compliance_history(
        self,
        standard: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query compliance check history.

        Returns:
            List of compliance check logs
        """
        return self.query(
            action=AuditAction.COMPLIANCE_CHECK,
            target_id=standard or "global",
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

    # ─────────────────────────────────────────────────────────────
    # Export methods (Raw Data)
    # ─────────────────────────────────────────────────────────────

    def export_jsonl(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        action_filter: list[AuditAction] | None = None,
        limit: int = 50000,
    ) -> Iterator[str]:
        """
        JSON Lines streaming export.

        Yields:
            JSON string per entry
        """
        entries = self.query(
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )

        for entry in entries:
            if action_filter:
                entry_action = entry.get("action", "")
                if not any(a.value == entry_action for a in action_filter):
                    continue
            yield fast_dumps_str(entry, default=str)

    def export_csv_compatible(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Streaming CSV-compatible flattened data.

        Yields:
            Flattened dict per entry (fixed audit fields + details_* keys)
        """
        from baldur.audit.constants import FIXED_AUDIT_FIELDS

        for line in self.export_jsonl(start_time=start_time, end_time=end_time):
            entry = fast_loads(line)
            flat = {k: entry.get(k) for k in FIXED_AUDIT_FIELDS}

            details = entry.get("details", {})
            for key, value in details.items():
                if isinstance(value, (dict, list)):
                    flat[f"details_{key}"] = fast_dumps_str(value)
                else:
                    flat[f"details_{key}"] = value

            yield flat

    # ─────────────────────────────────────────────────────────────
    # Integrity verification
    # ─────────────────────────────────────────────────────────────

    def verify_integrity(self) -> dict[str, Any]:
        """
        Verify audit log integrity.

        Returns:
            Verification result dictionary
        """
        entries = self.query(limit=10000)

        verifier = HashChainVerifier()

        # Verify entries that carry an integrity field
        entries_with_integrity = [
            e for e in entries if "integrity" in e.get("details", {})
        ]

        if not entries_with_integrity:
            return {
                "verified": True,
                "total_entries": len(entries),
                "verified_entries": 0,
                "message": "No entries with integrity information found",
            }

        # Lift integrity information to the top level
        for entry in entries_with_integrity:
            entry["integrity"] = entry.get("details", {}).get("integrity", {})

        is_valid, error_msg = verifier.verify_chain(entries_with_integrity)
        issues = verifier.find_tampering(entries_with_integrity) if not is_valid else []

        result = {
            "verified": is_valid,
            "total_entries": len(entries),
            "verified_entries": len(entries_with_integrity),
            "chain_state": self._hash_manager.get_state(),
        }

        if not is_valid:
            result["error"] = error_msg
            result["issues"] = issues

        return result

    def get_chain_state(self) -> dict[str, Any]:
        """Return the current hash chain state."""
        return self._hash_manager.get_state()

    # ─────────────────────────────────────────────────────────────
    # Internal methods
    # ─────────────────────────────────────────────────────────────

    def _record_with_integrity(self, entry: AuditEntry) -> str:
        """
        Record together with the hash chain.

        FAIL-OPEN policy:
        - A write failure never blocks business logic
        - With fallback_to_stdout enabled, a minimal record goes to stdout
        - fail_open=False enables Fail-Secure mode

        Args:
            entry: Audit entry

        Returns:
            Audit log ID
        """
        with self._lock:
            # Convert the entry to a dictionary
            entry_dict = entry.to_dict()

            # Add hash chain integrity information
            entry_dict = self._hash_manager.add_integrity(entry_dict)

            # Include integrity information in details
            entry.details["integrity"] = entry_dict.get("integrity", {})

            # WAL write (when enabled)
            wal_seq = None
            if self._wal_enabled and self._wal:
                try:
                    wal_seq = self._wal.write(entry_dict)
                except Exception as e:
                    logger.warning(
                        "continuous_audit.wal_write_failed",
                        error=e,
                    )

            # Record with the Fail-Open pattern
            try:
                self.audit_adapter.log(entry)

                # WAL commit (on success) — `mark_processed` is a PRO-impl
                # extension; OSS WriteAheadLog is write-only, so duck-type
                # the call.
                if wal_seq is not None and self._wal:
                    try:
                        mark_processed = getattr(self._wal, "mark_processed", None)
                        if mark_processed is not None:
                            mark_processed(wal_seq)
                    except Exception as e:
                        logger.warning(
                            "continuous_audit.wal_commit_failed",
                            error=e,
                        )

                # Checkpoint save (back-pressure applied)
                if wal_seq is not None and self._checkpoint_strategy:
                    self._maybe_save_checkpoint(wal_seq, entry_dict)

            except Exception as e:
                self._failed_write_count += 1

                if self._fallback_to_stdout:
                    # Fallback: write a minimal record to stdout
                    import sys

                    print(
                        f"[FALLBACK_AUDIT_LOG] {entry.action}: {entry.to_json()}",
                        file=sys.stderr,
                    )

                if not self._fail_open:
                    # Fail-Secure mode: propagate the exception
                    raise

                logger.warning(
                    "continuous_audit.write_failed_fail_open",
                    error=e,
                    failed_write_count=self._failed_write_count,
                )

            # Generate the ID (timestamp + sequence)
            integrity = entry_dict.get("integrity", {})
            audit_id = f"audit-{entry.timestamp.strftime('%Y%m%d%H%M%S')}-{integrity.get('sequence', 0):06d}"

            logger.debug(
                "continuous_audit.recorded",
                entry_action=entry.action,
                audit_id=audit_id,
            )

            return audit_id

    def get_stats(self) -> dict[str, Any]:
        """Return audit recorder statistics."""
        return {
            "failed_write_count": self._failed_write_count,
            "fail_open": self._fail_open,
            "fallback_to_stdout": self._fallback_to_stdout,
            "wal_enabled": self._wal_enabled,
            "chain_state": self._hash_manager.get_state(),
            "records_since_checkpoint": self._records_since_checkpoint,
            "checkpoint_save_interval": self._checkpoint_save_interval,
        }

    def _maybe_save_checkpoint(self, wal_seq: int, entry_dict: dict[str, Any]) -> None:
        """
        Save a checkpoint with back-pressure applied.

        Saves only every N records or once the max save interval is exceeded.
        Same back-pressure pattern as the sync worker.
        """
        self._records_since_checkpoint += 1

        should_save = (
            self._records_since_checkpoint >= self._checkpoint_save_interval
            or time.time() - self._last_checkpoint_time
            >= self._checkpoint_save_max_seconds
        )

        if not should_save:
            return

        try:
            from baldur.audit.checkpoint import UnifiedCheckpointData

            checkpoint_data = UnifiedCheckpointData(
                wal_sequence=wal_seq,
                checksum=entry_dict.get("integrity", {}).get("hash"),
            )
            assert self._checkpoint_strategy is not None  # caller-side truthy guard
            self._checkpoint_strategy.save(
                self._checkpoint_namespace,
                checkpoint_data,
            )

            # Reset counters on a successful save
            self._records_since_checkpoint = 0
            self._last_checkpoint_time = time.time()

            logger.debug(
                "continuous_audit.checkpoint_saved",
                wal_seq=wal_seq,
            )

        except Exception as e:
            logger.warning(
                "continuous_audit.checkpoint_save_failed",
                error=e,
            )

    def force_save_checkpoint(self, wal_seq: int | None = None) -> None:
        """
        Force a checkpoint save (ignoring back-pressure).

        Use when an immediate save is required, e.g. on a shutdown signal or
        during error recovery.
        """
        if not self._checkpoint_strategy:
            return

        try:
            from baldur.audit.checkpoint import UnifiedCheckpointData

            checkpoint_data = UnifiedCheckpointData(
                wal_sequence=wal_seq or 0,
            )
            self._checkpoint_strategy.save(
                self._checkpoint_namespace,
                checkpoint_data,
            )

            self._records_since_checkpoint = 0
            self._last_checkpoint_time = time.time()

            logger.info(
                "continuous_audit.checkpoint_force_saved",
                wal_seq=wal_seq,
            )

        except Exception as e:
            logger.warning(
                "continuous_audit.checkpoint_force_save_failed",
                error=e,
            )

    def _send_alert(self, channel: str, data: dict[str, Any]) -> None:
        """Send an alert."""
        if self.alert_callback:
            try:
                self.alert_callback(channel, data)
            except Exception as e:
                logger.warning(
                    "continuous_audit.alert_callback_failed",
                    error=e,
                )

        # Alert on the configured channels (extensible)
        if channel in self.config.alert_channels or "all" in self.config.alert_channels:
            logger.info(
                "continuous_audit.alert",
                channel=channel,
                data=data,
            )


# =============================================================================
# Singleton Management
# =============================================================================

_recorder_instance: ContinuousAuditRecorder | None = None
_recorder_lock = threading.Lock()


def get_continuous_audit_recorder() -> ContinuousAuditRecorder:
    """
    ContinuousAuditRecorder singleton instance.

    Uses double-check locking for thread safety.
    Adapter is resolved via get_audit_adapter() singleton
    (priority: set → Registry → File → Null).

    Returns:
        ContinuousAuditRecorder instance
    """
    global _recorder_instance

    if _recorder_instance is not None:
        return _recorder_instance

    with _recorder_lock:
        if _recorder_instance is not None:
            return _recorder_instance

        from baldur.adapters.audit.singleton import get_audit_adapter

        adapter = get_audit_adapter()
        config = AuditConfig.get_default()
        _recorder_instance = ContinuousAuditRecorder(
            audit_adapter=adapter,
            config=config,
        )
        logger.debug("continuous_audit.recorder_initialized")
        return _recorder_instance


def reset_continuous_audit_recorder() -> None:
    """
    Reset ContinuousAuditRecorder singleton (for testing).

    Ensures test isolation by clearing the cached instance.
    """
    global _recorder_instance

    with _recorder_lock:
        _recorder_instance = None
        logger.debug("continuous_audit.recorder_reset")
