"""
Escalation Audit Trail.

Records the reasoning behind every override decision in the audit log.

Recorded events:
- Global -> Regional forced override
- Global ignored via Admin Override
- Safety-Max decision
- Cascade Escalation (chained escalation across multiple regions)
- Partition Fallback (local fallback caused by network isolation)

Makes "why did it end up in this state" 100% traceable.

Code reference:
    coordination/coordinator.py (DryRunAuditLogger pattern)
    CriticalPathFallback.append_audit_log
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Decision Types
# =============================================================================


class EscalationDecisionType:
    """
    Override decision types.

    Each type states "why it ended up in this state".
    """

    GLOBAL_OVERRIDE = "GLOBAL_OVERRIDE"
    """Global STRICT forcibly overrides Regional."""

    ADMIN_OVERRIDE = "ADMIN_OVERRIDE"
    """Admin manually ignores Global and applies Regional."""

    SAFETY_MAX = "SAFETY_MAX"
    """Safety-Max: pick the stricter of the two states."""

    REGIONAL_DEFAULT = "REGIONAL_DEFAULT"
    """Both NORMAL, so the Regional default is used."""

    CASCADE_ESCALATION = "CASCADE_ESCALATION"
    """Global escalation caused by a chained multi-region failure."""

    PARTITION_FALLBACK = "PARTITION_FALLBACK"
    """Local fallback caused by network isolation."""

    REGIONAL_STRICT = "REGIONAL_STRICT"
    """Regional STRICT activated (Global is NORMAL)."""

    FALLBACK = "FALLBACK"
    """Safe default used because the query failed."""


@dataclass
class EscalationAuditEntry(SerializableMixin):
    """
    Audit entry for an override decision.

    Beyond scope and namespace, it explicitly records **"why this decision
    was made"**.

    Attributes:
        event_id: Unique event ID (e.g. "esc-a1b2c3d4e5f6")
        decision_type: Decision type (EscalationDecisionType)
        decision_reason: Detailed reason for the decision
        namespace: Target namespace
        effective_state: State that was finally applied
        overridden_state: State that was overwritten (before snapshot)
        triggered_by: Actor that triggered the decision
        precedence: Command precedence
        timestamp: Recording time (ISO format)
        global_state_snapshot: Global state snapshot (at decision time)
        regional_state_snapshot: Regional state snapshot (at decision time)
        ttl_minutes: Admin Override TTL (minutes)
    """

    # Unique identifier
    event_id: str = field(default_factory=lambda: f"esc-{uuid.uuid4().hex[:12]}")

    # Decision information (the core!)
    decision_type: str = ""
    """Decision type (GLOBAL_OVERRIDE, ADMIN_OVERRIDE, etc.)."""

    decision_reason: str = ""
    """Decision reason (e.g. 'Global STRICT overrides regional seoul (NORMAL)')."""

    # State information
    namespace: str = ""
    """Target namespace."""

    effective_state: dict[str, Any] = field(default_factory=dict)
    """State that was finally applied."""

    overridden_state: dict[str, Any] | None = None
    """State that was overwritten (before snapshot)."""

    # Actor information
    triggered_by: str = ""
    """Actor that triggered the decision (user_id, 'system', 'AtomicStateQuery')."""

    precedence: str | None = None
    """Command precedence (for manual overrides)."""

    # Metadata
    timestamp: str = field(default_factory=lambda: utc_now().isoformat())

    # Global state snapshot (for comparison)
    global_state_snapshot: dict[str, Any] | None = None
    """Global state snapshot (at decision time)."""

    regional_state_snapshot: dict[str, Any] | None = None
    """Regional state snapshot (at decision time)."""

    # TTL information
    ttl_minutes: int | None = None
    """Admin Override TTL (minutes)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EscalationAuditEntry:
        """Build from a dictionary."""
        return cls(
            event_id=data.get("event_id", f"esc-{uuid.uuid4().hex[:12]}"),
            decision_type=data.get("decision_type", ""),
            decision_reason=data.get("decision_reason", ""),
            namespace=data.get("namespace", ""),
            effective_state=data.get("effective_state", {}),
            overridden_state=data.get("overridden_state"),
            triggered_by=data.get("triggered_by", ""),
            precedence=data.get("precedence"),
            timestamp=data.get("timestamp", utc_now().isoformat()),
            global_state_snapshot=data.get("global_state_snapshot"),
            regional_state_snapshot=data.get("regional_state_snapshot"),
            ttl_minutes=data.get("ttl_minutes"),
        )


class EscalationAuditTrail:
    """
    Audit trail for override decisions.

    Records every state decision so that "why it ended up in this state" is
    100% traceable.

    Features:
    - Memory buffer + CriticalPathFallback integration
    - Thread-safe (RLock)
    - Convenience methods per decision type

    Code reference:
        CriticalPathFallback.append_audit_log

    Usage:
        audit = EscalationAuditTrail()

        # Record a Global override
        event_id = audit.log_global_override(
            namespace="seoul",
            global_state={"governance_mode": "STRICT", ...},
            regional_state={"governance_mode": "NORMAL", ...},
        )

        # Query recent decisions
        decisions = audit.get_recent_decisions(namespace="seoul", limit=10)
    """

    def __init__(self, max_buffer_size: int | None = None):
        """
        Initialize EscalationAuditTrail.

        Args:
            max_buffer_size: Max memory buffer size (loaded from Settings if None)
        """
        self._lock = threading.RLock()
        self._memory_buffer: list[EscalationAuditEntry] = []
        self._max_buffer_size = (
            max_buffer_size
            if max_buffer_size is not None
            else self._get_max_buffer_size()
        )

    @staticmethod
    def _get_max_buffer_size() -> int:
        """Load max_buffer_size from Settings."""
        try:
            from baldur.settings.regional_emergency import (
                get_regional_emergency_settings,
            )

            return get_regional_emergency_settings().max_buffer_size
        except ImportError:
            return 1000  # default

    def log_decision(
        self,
        decision_type: str,
        decision_reason: str,
        namespace: str,
        effective_state: dict[str, Any],
        overridden_state: dict[str, Any] | None = None,
        triggered_by: str = "system",
        precedence: str | None = None,
        global_state: dict[str, Any] | None = None,
        regional_state: dict[str, Any] | None = None,
        ttl_minutes: int | None = None,
    ) -> str:
        """
        Record a decision.

        Args:
            decision_type: Decision type (EscalationDecisionType)
            decision_reason: Decision reason (be specific!)
            namespace: Target namespace
            effective_state: State that was finally applied
            overridden_state: State that was overwritten (before snapshot)
            triggered_by: Actor that triggered the decision
            precedence: Command precedence
            global_state: Global state snapshot
            regional_state: Regional state snapshot
            ttl_minutes: Admin Override TTL

        Returns:
            The generated event_id
        """
        entry = EscalationAuditEntry(
            decision_type=decision_type,
            decision_reason=decision_reason,
            namespace=namespace,
            effective_state=effective_state,
            overridden_state=overridden_state,
            triggered_by=triggered_by,
            precedence=precedence,
            global_state_snapshot=global_state,
            regional_state_snapshot=regional_state,
            ttl_minutes=ttl_minutes,
        )

        with self._lock:
            self._memory_buffer.append(entry)
            # Enforce the buffer size limit
            if len(self._memory_buffer) > self._max_buffer_size:
                self._memory_buffer = self._memory_buffer[-self._max_buffer_size :]

        # CriticalPathFallback integration (durable storage)
        self._persist_to_fallback(entry)

        # Emit the log line
        log_level = (
            logging.WARNING
            if decision_type
            in (
                EscalationDecisionType.GLOBAL_OVERRIDE,
                EscalationDecisionType.ADMIN_OVERRIDE,
                EscalationDecisionType.CASCADE_ESCALATION,
                EscalationDecisionType.PARTITION_FALLBACK,
            )
            else logging.INFO
        )

        logger.log(
            log_level,
            f"[EscalationAudit] {decision_type}: {decision_reason} "  # noqa: G004
            f"(namespace={namespace}, by={triggered_by})",
        )

        return entry.event_id

    def log_global_override(
        self,
        namespace: str,
        global_state: dict[str, Any],
        regional_state: dict[str, Any],
        triggered_by: str = "system",
    ) -> str:
        """
        Record a Global -> Regional forced override.

        Called when Global STRICT forcibly overwrites the Regional state.

        Args:
            namespace: Target namespace
            global_state: Global state (applied)
            regional_state: Regional state (ignored)
            triggered_by: Triggering actor

        Returns:
            The generated event_id
        """
        reason = (
            f"Global STRICT ({global_state.get('emergency_level', 'N/A')}) "
            f"overrides regional {namespace} "
            f"({regional_state.get('governance_mode', 'NORMAL')})"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.GLOBAL_OVERRIDE,
            decision_reason=reason,
            namespace=namespace,
            effective_state=global_state,
            overridden_state=regional_state,
            triggered_by=triggered_by,
            global_state=global_state,
            regional_state=regional_state,
        )

    def log_admin_override(
        self,
        namespace: str,
        regional_state: dict[str, Any],
        global_state: dict[str, Any],
        triggered_by: str,
        precedence: str,
        ttl_minutes: int | None = None,
    ) -> str:
        """
        Record an Admin Override (Global ignored).

        Called when an administrator explicitly ignores Global and applies the
        Regional state.

        Args:
            namespace: Target namespace
            regional_state: Regional state (applied)
            global_state: Global state (ignored)
            triggered_by: Administrator ID
            precedence: Command precedence ("ADMIN_OVERRIDE" or "KILL_SWITCH")
            ttl_minutes: Override TTL

        Returns:
            The generated event_id
        """
        reason = (
            f"Admin override ({precedence}) by {triggered_by}: "
            f"using Regional {namespace} ({regional_state.get('governance_mode', 'NORMAL')}) "
            f"instead of Global ({global_state.get('governance_mode', 'NORMAL')})"
        )

        if ttl_minutes:
            reason += f" [TTL: {ttl_minutes}m]"

        return self.log_decision(
            decision_type=EscalationDecisionType.ADMIN_OVERRIDE,
            decision_reason=reason,
            namespace=namespace,
            effective_state=regional_state,
            overridden_state=global_state,
            triggered_by=triggered_by,
            precedence=precedence,
            global_state=global_state,
            regional_state=regional_state,
            ttl_minutes=ttl_minutes,
        )

    def log_regional_strict(
        self,
        namespace: str,
        regional_state: dict[str, Any],
        global_state: dict[str, Any],
        triggered_by: str = "system",
    ) -> str:
        """
        Record a Regional STRICT activation.

        Called when Global is NORMAL but Regional is STRICT.

        Args:
            namespace: Target namespace
            regional_state: Regional state (STRICT)
            global_state: Global state (NORMAL)
            triggered_by: Triggering actor

        Returns:
            The generated event_id
        """
        reason = (
            f"Regional STRICT active for {namespace} "
            f"(level={regional_state.get('emergency_level', 'N/A')}), "
            f"Global is NORMAL"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.REGIONAL_STRICT,
            decision_reason=reason,
            namespace=namespace,
            effective_state=regional_state,
            overridden_state=None,
            triggered_by=triggered_by,
            global_state=global_state,
            regional_state=regional_state,
        )

    def log_cascade_escalation(
        self,
        affected_regions: list[str],
        triggered_by: str = "system",
    ) -> str:
        """
        Record a Cascade Escalation (chained multi-region escalation).

        Called when several regions become STRICT at once and escalate to
        Global.

        Args:
            affected_regions: List of affected regions
            triggered_by: Triggering actor

        Returns:
            The generated event_id
        """
        reason = (
            f"Cascade escalation to Global STRICT: "
            f"{len(affected_regions)} regions affected ({', '.join(affected_regions)})"
        )

        return self.log_decision(
            decision_type=EscalationDecisionType.CASCADE_ESCALATION,
            decision_reason=reason,
            namespace="global",
            effective_state={"governance_mode": "STRICT", "scope": "global"},
            overridden_state=None,
            triggered_by=triggered_by,
        )

    def log_fallback(
        self,
        namespace: str,
        error: str,
        triggered_by: str = "AtomicStateQuery",
    ) -> str:
        """
        Record a fallback (safe default used because the query failed).

        Args:
            namespace: Target namespace
            error: Failure cause
            triggered_by: Triggering actor

        Returns:
            The generated event_id
        """
        reason = f"Query failed for {namespace}, using safe default: {error}"

        return self.log_decision(
            decision_type=EscalationDecisionType.FALLBACK,
            decision_reason=reason,
            namespace=namespace,
            effective_state={"governance_mode": "NORMAL", "is_active": False},
            triggered_by=triggered_by,
        )

    def get_recent_decisions(
        self,
        namespace: str | None = None,
        decision_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query recent decisions.

        Args:
            namespace: Namespace to filter by (all if None)
            decision_type: Decision type to filter by (all if None)
            limit: Maximum number of entries to return

        Returns:
            List of decisions (newest first)
        """
        with self._lock:
            entries = self._memory_buffer[-limit:]

            if namespace:
                entries = [e for e in entries if e.namespace == namespace]

            if decision_type:
                entries = [e for e in entries if e.decision_type == decision_type]

            return [e.to_dict() for e in reversed(entries)]

    def get_decision_by_id(self, event_id: str) -> dict[str, Any] | None:
        """
        Query a specific decision.

        Args:
            event_id: Event ID

        Returns:
            Decision dictionary, or None
        """
        with self._lock:
            for entry in self._memory_buffer:
                if entry.event_id == event_id:
                    return entry.to_dict()
        return None

    def get_stats(self) -> dict[str, Any]:
        """
        Return audit trail statistics.

        Returns:
            Statistics dictionary
        """
        with self._lock:
            by_type: dict[str, int] = {}
            by_namespace: dict[str, int] = {}

            for entry in self._memory_buffer:
                by_type[entry.decision_type] = by_type.get(entry.decision_type, 0) + 1
                by_namespace[entry.namespace] = by_namespace.get(entry.namespace, 0) + 1

            return {
                "total_entries": len(self._memory_buffer),
                "by_type": by_type,
                "by_namespace": by_namespace,
                "buffer_capacity": self._max_buffer_size,
            }

    def clear(self) -> None:
        """Clear the buffer (for tests)."""
        with self._lock:
            self._memory_buffer.clear()

    def _persist_to_fallback(self, entry: EscalationAuditEntry) -> None:
        """Persist durably to CriticalPathFallback."""
        try:
            from baldur_pro.services.coordination.critical_path_fallback import (
                CriticalPathFallback,
            )

            fallback = CriticalPathFallback()
            fallback.append_audit_log(entry.to_dict())
        except Exception as e:
            logger.debug(
                "escalation_audit.fallback_persist_skipped",
                error=e,
            )


# =============================================================================
# Singleton
# =============================================================================

_audit_trail: EscalationAuditTrail | None = None
_audit_trail_lock = threading.Lock()


def get_escalation_audit_trail() -> EscalationAuditTrail:
    """
    Return the EscalationAuditTrail singleton.

    Returns:
        EscalationAuditTrail instance
    """
    global _audit_trail
    if _audit_trail is None:
        with _audit_trail_lock:
            if _audit_trail is None:
                _audit_trail = EscalationAuditTrail()
    return _audit_trail


def reset_escalation_audit_trail() -> None:
    """Reset the singleton (for tests)."""
    global _audit_trail
    _audit_trail = None
