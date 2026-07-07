"""
Audit Log Adapter Interface

Provides an abstraction for audit logging, allowing users to choose
where and how audit logs are stored without being tied to any specific
storage backend.

Design Philosophy:
- No forced dependencies on user's system (no DB tables, no external services)
- User chooses: file, stdout, their own DB, Grafana/Loki, or custom solution
- Default is non-invasive (file or stdout)

Usage:
    # Use default file adapter
    from baldur.adapters.audit import FileAuditLogAdapter
    adapter = FileAuditLogAdapter("logs/audit.log")

    # Or implement your own
    class MyGrafanaAdapter(AuditLogAdapter):
        def log(self, entry: AuditEntry) -> None:
            loki_client.push(entry.to_dict())
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


class AuditAction(str, Enum):
    """Standard audit action types."""

    # Circuit Breaker
    CB_FORCE_OPEN = "cb_force_open"
    CB_FORCE_CLOSE = "cb_force_close"
    CB_AUTO_OPEN = "cb_auto_open"
    CB_AUTO_CLOSE = "cb_auto_close"
    CB_HALF_OPEN = "cb_half_open"

    # DLQ
    DLQ_STORE = "dlq_store"
    DLQ_REPLAY_START = "dlq_replay_start"
    DLQ_REPLAY_SUCCESS = "dlq_replay_success"
    DLQ_REPLAY_FAILED = "dlq_replay_failed"
    DLQ_ESCALATE = "dlq_escalate"
    DLQ_RESOLVE = "dlq_resolve"
    DLQ_REJECT = "dlq_reject"
    DLQ_FORCE_REDRIVE = "dlq_force_redrive"
    """Operator cap-override re-drive of an at-cap (REQUIRES_REVIEW) entry —
    a privileged, audited action distinct from a normal replay."""

    # Retry
    RETRY_ATTEMPT = "retry_attempt"
    RETRY_SUCCESS = "retry_success"
    RETRY_EXHAUSTED = "retry_exhausted"

    # Security
    SECURITY_INCIDENT = "security_incident"
    SECURITY_ALERT = "security_alert"

    # Governance (automation block tracking)
    GOVERNANCE_BLOCKED = "governance_blocked"
    GOVERNANCE_KILL_SWITCH = "governance_kill_switch"
    GOVERNANCE_EMERGENCY = "governance_emergency"
    GOVERNANCE_ERROR_BUDGET = "governance_error_budget"

    # System
    CONFIG_CHANGE = "config_change"
    MANUAL_OVERRIDE = "manual_override"

    # Auto Tuning (autonomous adjustment)
    AUTO_TUNING_ADJUSTMENT = "auto_tuning_adjustment"
    AUTO_TUNING_ENABLED = "auto_tuning_enabled"
    AUTO_TUNING_DISABLED = "auto_tuning_disabled"
    AUTO_TUNING_BOUNDS_CHANGED = "auto_tuning_bounds_changed"
    AUTO_TUNING_REJECTED = "auto_tuning_rejected"  # Safety bound exceeded
    AUTO_TUNING_ROLLBACK = "auto_tuning_rollback"

    # DNA Drift (configuration drift)
    DNA_DRIFT_DETECTED = "dna_drift_detected"
    DNA_DRIFT_RESOLVED = "dna_drift_resolved"

    # Compliance
    COMPLIANCE_CHECK = "compliance_check"
    COMPLIANCE_VIOLATION = "compliance_violation"

    # API exceptions (used by the DRF exception handler)
    API_ERROR = "api_error"
    """Error raised while handling an API request."""

    VALIDATION_FAILED = "validation_failed"
    """Input validation failed."""

    AUTHORIZATION_DENIED = "authorization_denied"
    """Authorization denied (no permission)."""

    # Forensic context capture (post-incident investigation)
    FORENSIC_CAPTURE_COMPLETED = "forensic_capture_completed"
    """Forensic context recorded after a captured exception."""


class ContextType(str, Enum):
    """
    Context type in which the audit event was emitted.

    Distinguishes middleware vs Celery Task vs system automation,
    enabling consistent filtering during analysis.

    Industry precedent:
    - AWS CloudTrail: eventSource + eventType
    - Datadog APM: trace.origin
    - OpenTelemetry: SpanKind
    """

    REQUEST = "request"  # During HTTP request handling (middleware)
    TASK = "task"  # Background tasks (Celery, RQ)
    SYSTEM = "system"  # System automation (scheduler, auto-recovery)
    WEBHOOK = "webhook"  # External webhook handling
    CLI = "cli"  # CLI command execution
    UNKNOWN = "unknown"  # Unknown (fallback)


def _get_default_actor() -> tuple[str | None, str, list[str]]:
    """
    Get default actor from ActorContext if available.

    Returns (actor_id, actor_type, roles) tuple.
    Falls back to (None, "system", []) if ActorContext not available.
    """
    try:
        from baldur.context.actor_context import ActorContext

        if ActorContext.is_set():
            actor = ActorContext.get_current()
            return actor.actor_id, actor.actor_type, actor.roles
    except ImportError:
        pass
    return None, "system", []


@dataclass
class AuditEntry:
    """
    Audit log entry containing all relevant context.

    Captures:
    - What happened (action)
    - Who did it (actor_id, actor_type) - sourced automatically from ActorContext
    - What was affected (target_type, target_id)
    - Why (reason)
    - Additional context (details)

    Note:
        When actor_id and actor_type are not set explicitly, they are pulled
        from ActorContext automatically.
        This auto-tracks "who set this and when".
    """

    action: AuditAction | str
    timestamp: datetime = field(default_factory=lambda: utc_now())

    # Actor information - sourced automatically from ActorContext
    actor_id: str | None = field(default=None)
    actor_type: str = field(default="system")
    actor_roles: list[str] = field(default_factory=list)

    # Context type - environment in which the event was emitted (middleware/task/system)
    context_type: ContextType = field(default=ContextType.UNKNOWN)

    # Target information
    target_type: str | None = None  # circuit_breaker, dlq_entry, etc.
    target_id: str | None = None

    # Context
    service_name: str | None = None
    domain: str | None = None
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    # Result
    success: bool = True
    error_message: str | None = None

    def __post_init__(self) -> None:
        """
        Post-init: auto-fill actor information from ActorContext.

        When actor_id is not explicitly set, pulls it from ActorContext.
        This auto-tracks "who changed this configuration".
        """
        # When actor_id is None and actor_type is the default "system", auto-fill
        if self.actor_id is None and self.actor_type == "system":
            auto_actor_id, auto_actor_type, auto_roles = _get_default_actor()
            if auto_actor_id is not None:
                # Use object.__setattr__ for frozen-like behavior compatibility
                object.__setattr__(self, "actor_id", auto_actor_id)
                object.__setattr__(self, "actor_type", auto_actor_type)
                if auto_roles and not self.actor_roles:
                    object.__setattr__(self, "actor_roles", auto_roles)

        # If actor_id is set but actor_roles is empty, pull from ActorContext
        if not self.actor_roles:
            try:
                from baldur.context.actor_context import ActorContext

                if ActorContext.is_set():
                    actor = ActorContext.get_current()
                    if actor.roles:
                        object.__setattr__(self, "actor_roles", actor.roles)
            except ImportError:
                pass

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "action": (
                self.action.value
                if isinstance(self.action, AuditAction)
                else self.action
            ),
            "timestamp": self.timestamp.isoformat(),
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "actor_roles": self.actor_roles,
            "context_type": (
                self.context_type.value
                if isinstance(self.context_type, ContextType)
                else self.context_type
            ),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "service_name": self.service_name,
            "domain": self.domain,
            "reason": self.reason,
            "details": self.details,
            "success": self.success,
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEntry:
        """Inverse of ``to_dict()`` — round-trip safe.

        Unknown fields (e.g. ``integrity``, ``checksum``, ``audit_id`` added
        by ``ResilientContinuousAuditRecorder``) are preserved in
        ``details`` under their original keys to maintain forensic
        completeness.

        Forward-compatibility design (intentional, no schema_version
        field needed):

        1. **New first-class field added later**: old ``from_dict()``
           running on new data — the unknown key falls into ``details``,
           no exception. New ``from_dict()`` running on old data —
           ``data.get(key)`` returns ``None``, default applies.
           Round-trip is preserved at every point in time.
        2. **Field promoted from details to first-class**: requires a
           one-time migration script. A schema_version field cannot
           solve this automatically.
        3. **Field semantic change**: cannot be auto-detected without a
           version field, but also cannot be auto-handled. Always
           requires coordinated reader/writer updates.

        Conclusion: a ``schema_version`` meta field would add ceremony
        without solving the cases that actually need solving. The
        ``known``-set + ``details``-overflow design covers the only
        case that benefits from automation, and does so without
        ceremony.

        Args:
            data: Dict produced by ``to_dict()`` (or a superset).

        Returns:
            ``AuditEntry`` reconstructed from the dict.
        """
        action_str = data.get("action", "")
        try:
            action: AuditAction | str = AuditAction(action_str)
        except ValueError:
            action = action_str

        ts = data.get("timestamp")
        if isinstance(ts, str):
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif isinstance(ts, datetime):
            timestamp = ts
        else:
            timestamp = utc_now()

        ctx_str = data.get("context_type", "unknown")
        try:
            context_type = ContextType(ctx_str)
        except ValueError:
            context_type = ContextType.UNKNOWN

        known = {
            "action",
            "timestamp",
            "actor_id",
            "actor_type",
            "actor_roles",
            "context_type",
            "target_type",
            "target_id",
            "service_name",
            "domain",
            "reason",
            "details",
            "success",
            "error_message",
        }
        details = dict(data.get("details", {}))
        for k, v in data.items():
            if k not in known:
                details[k] = v  # integrity, checksum, audit_id, ...

        return cls(
            action=action,
            timestamp=timestamp,
            actor_id=data.get("actor_id"),
            actor_type=data.get("actor_type", "system"),
            actor_roles=list(data.get("actor_roles", [])),
            context_type=context_type,
            target_type=data.get("target_type"),
            target_id=data.get("target_id"),
            service_name=data.get("service_name"),
            domain=data.get("domain"),
            reason=data.get("reason"),
            details=details,
            success=bool(data.get("success", True)),
            error_message=data.get("error_message"),
        )

    @classmethod
    def from_wal_dict(cls, data: dict[str, Any]) -> AuditEntry:
        """Convert a Write-Ahead-Log entry payload into an ``AuditEntry``.

        The audit WAL drained by the background sync worker (the
        recovery-replay path) holds the **native WAL schema** written by the
        ``log_*_audit`` helpers, not the ``to_dict()`` schema. That native
        shape carries ``event_type`` (not ``action``) and a float-epoch
        ``timestamp`` (not an ISO string), so a plain ``from_dict()`` would
        drop the action and reset the timestamp to now — corrupting the two
        most audit-critical fields for a compliance trail. This converter
        maps the native schema faithfully and delegates the ``to_dict()``
        shape to ``from_dict()`` as defense-in-depth for an injected or
        future-wired ``to_dict()``-shape WAL.

        The native-shape conversion is **total**: it never raises for a dict
        input — missing *or* malformed fields (a non-ISO/out-of-range
        timestamp, a non-mapping ``details``) fall back to defaults — so a
        corrupted WAL entry can never become a poison entry that stalls the
        sync cursor.

        Args:
            data: A WAL entry payload — the native ``log_*_audit`` shape (has
                ``event_type``) or a ``to_dict()`` superset (has ``action``).

        Returns:
            ``AuditEntry`` reconstructed from the WAL payload.
        """
        # Native WAL shape: ``event_type`` present with no top-level
        # ``action``. The ``to_dict()`` shape always carries ``action``, so
        # this disjunction routes each schema to its own path unambiguously.
        if "event_type" in data and "action" not in data:
            return cls._from_native_wal_dict(data)
        return cls.from_dict(data)

    @classmethod
    def _from_native_wal_dict(cls, data: dict[str, Any]) -> AuditEntry:
        """Map the native ``_write_to_wal`` schema onto an ``AuditEntry``.

        Field map: ``event_type``->``action`` (verbatim string — native event
        types are upper-cased and miss the enum, so the recorded string is
        kept when no enum member matches); float/int-epoch ``timestamp``
        ->``datetime`` (original time preserved, never reset to now); direct
        maps for actor/target/domain/result fields; native-only keys folded
        into ``details`` without clobbering the inner ``details`` payload.
        """
        event_type = data.get("event_type", "")
        try:
            action: AuditAction | str = AuditAction(event_type)
        except ValueError:
            action = event_type

        ts = data.get("timestamp")
        try:
            if isinstance(ts, bool):
                timestamp = utc_now()
            elif isinstance(ts, (int, float)):
                timestamp = datetime.fromtimestamp(ts, tz=UTC)
            elif isinstance(ts, str):
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            elif isinstance(ts, datetime):
                timestamp = ts
            else:
                timestamp = utc_now()
        except (ValueError, OverflowError, OSError):
            # A corrupted WAL timestamp (non-ISO string, out-of-range or
            # NaN/inf epoch) must not raise — totality keeps a malformed entry
            # from becoming a poison entry that stalls the sync cursor. Fall
            # back to now.
            timestamp = utc_now()

        # Fold the native-only keys into ``details`` without overwriting any
        # key the helper already recorded in the inner ``details`` payload. A
        # non-mapping ``details`` (corrupted WAL) degrades to empty rather than
        # raising, preserving totality.
        raw_details = data.get("details")
        details = dict(raw_details) if isinstance(raw_details, dict) else {}
        for native_key in (
            "record_id",
            "source",
            "synced",
            "celery_context",
            "trace_id",
        ):
            if native_key in data and native_key not in details:
                details[native_key] = data[native_key]

        raw_roles = data.get("actor_roles")
        actor_roles = list(raw_roles) if isinstance(raw_roles, (list, tuple)) else []

        return cls(
            action=action,
            timestamp=timestamp,
            actor_id=data.get("actor_id"),
            actor_type=data.get("actor_type", "system"),
            actor_roles=actor_roles,
            target_id=data.get("target_id"),
            domain=data.get("domain"),
            details=details,
            success=bool(data.get("success", True)),
            error_message=data.get("error_message"),
        )


class AuditLogAdapter(ABC):
    """
    Abstract interface for audit logging.

    Implementations can store audit logs in:
    - Files (FileAuditLogAdapter)
    - stdout (StdoutAuditLogAdapter)
    - Database (user implements)
    - External services like Loki, Datadog (user implements)
    - Nowhere (NullAuditLogAdapter)
    """

    @abstractmethod
    def log(self, entry: AuditEntry) -> None:
        """
        Log an audit entry.

        Args:
            entry: The audit entry to log
        """
        pass

    @abstractmethod
    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """
        Query audit logs (optional - may not be supported by all adapters).

        Args:
            action: Filter by action type
            target_type: Filter by target type
            target_id: Filter by target ID
            start_time: Filter from this time
            end_time: Filter until this time
            limit: Maximum entries to return

        Returns:
            List of matching audit entries
        """
        pass

    def log_cb_open(
        self,
        service_name: str,
        reason: str,
        actor_id: str | None = None,
        is_manual: bool = True,
    ) -> None:
        """Convenience method for Circuit Breaker open."""
        self.log(
            AuditEntry(
                action=(
                    AuditAction.CB_FORCE_OPEN if is_manual else AuditAction.CB_AUTO_OPEN
                ),
                service_name=service_name,
                target_type="circuit_breaker",
                target_id=service_name,
                actor_id=actor_id,
                actor_type="user" if is_manual else "system",
                reason=reason,
            )
        )

    def log_cb_close(
        self,
        service_name: str,
        reason: str,
        actor_id: str | None = None,
        is_manual: bool = True,
        trigger_replay: bool = False,
    ) -> None:
        """Convenience method for Circuit Breaker close."""
        self.log(
            AuditEntry(
                action=(
                    AuditAction.CB_FORCE_CLOSE
                    if is_manual
                    else AuditAction.CB_AUTO_CLOSE
                ),
                service_name=service_name,
                target_type="circuit_breaker",
                target_id=service_name,
                actor_id=actor_id,
                actor_type="user" if is_manual else "system",
                reason=reason,
                details={"trigger_replay": trigger_replay},
            )
        )

    def log_dlq_store(
        self,
        dlq_id: int,
        domain: str,
        failure_type: str,
        error_message: str | None = None,
    ) -> None:
        """Convenience method for DLQ storage."""
        self.log(
            AuditEntry(
                action=AuditAction.DLQ_STORE,
                domain=domain,
                target_type="dlq_entry",
                target_id=str(dlq_id),
                details={
                    "failure_type": failure_type,
                    "error_message": error_message[:200] if error_message else None,
                },
            )
        )

    def log_dlq_replay(
        self,
        dlq_id: int,
        domain: str,
        success: bool,
        actor_id: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Convenience method for DLQ replay."""
        self.log(
            AuditEntry(
                action=(
                    AuditAction.DLQ_REPLAY_SUCCESS
                    if success
                    else AuditAction.DLQ_REPLAY_FAILED
                ),
                domain=domain,
                target_type="dlq_entry",
                target_id=str(dlq_id),
                actor_id=actor_id,
                actor_type="user" if actor_id else "system",
                success=success,
                error_message=error_message,
            )
        )

    def log_retry(
        self,
        domain: str,
        func_name: str,
        attempt: int,
        max_attempts: int,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        """Convenience method for retry attempts."""
        if success:
            action = AuditAction.RETRY_SUCCESS
        elif attempt >= max_attempts:
            action = AuditAction.RETRY_EXHAUSTED
        else:
            action = AuditAction.RETRY_ATTEMPT

        self.log(
            AuditEntry(
                action=action,
                domain=domain,
                target_type="operation",
                target_id=func_name,
                details={
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                },
                success=success,
                error_message=error_message,
            )
        )

    def log_governance_blocked(
        self,
        block_reason: str,
        operation_name: str,
        details: dict[str, Any] | None = None,
        service_name: str | None = None,
        domain: str | None = None,
    ) -> None:
        """
        Record an entry when automation was blocked by governance.

        This method answers the "why didn't the operation run at this time?"
        question concretely.

        Args:
            block_reason: Block reason (kill_switch, emergency_mode, error_budget)
            operation_name: Name of the blocked operation
            details: Additional details (emergency_level, budget_percent, etc.)
            service_name: Related service name
            domain: Domain (payment, point, etc.)
        """
        # Select the appropriate action based on block_reason
        action_map = {
            "kill_switch": AuditAction.GOVERNANCE_KILL_SWITCH,
            "emergency_mode": AuditAction.GOVERNANCE_EMERGENCY,
            "error_budget": AuditAction.GOVERNANCE_ERROR_BUDGET,
        }
        action = action_map.get(block_reason, AuditAction.GOVERNANCE_BLOCKED)

        self.log(
            AuditEntry(
                action=action,
                service_name=service_name,
                domain=domain,
                target_type="automation",
                target_id=operation_name,
                reason=f"Governance blocked: {block_reason}",
                details=details or {},
                success=False,
                error_message=f"Operation '{operation_name}' blocked by {block_reason}",
            )
        )


# =============================================================================
# OSS NoOp defaults for the Dormant boundary audit registry slots
# =============================================================================
# Doc 528 D10-v2 "NoOp default registration requirement". The
# ``audit_kafka_adapter`` and ``audit_worm_adapter`` ProviderRegistry slots
# hold AuditLogAdapter implementations whose concrete forms live in
# ``baldur_dormant.adapters.audit.{kafka_adapter,worm_adapters}``. When
# ``baldur_dormant`` is absent, OSS bootstrap pre-registers these NoOp
# classes as the slot default so callers can route through ``.get()``
# unconditionally without ``is not None`` guards.
#
# The general OSS-tier audit slot (``ProviderRegistry.audit``) already has
# its own NoOp (``NullAuditLogAdapter`` registered under name ``"null"``),
# so these classes specifically address the *Kafka-shaped* and *WORM-shaped*
# audit slots — they signal intent ("Kafka adapter slot is in NoOp mode")
# and log under distinct event names so operators can tell the difference.


class NoOpKafkaAuditAdapter(AuditLogAdapter):
    """Broker-shaped audit NoOp returned by the broker-audit registry slot.

    When the private package is not installed, the broker-audit registry slot
    returns this adapter so OSS callers can request the broker audit path
    unconditionally without needing to check for the underlying broker.
    ``log()`` silently drops the entry (logging at DEBUG so operational tracing
    remains possible); ``query()`` returns an empty list — there is no backing
    topic.
    """

    def log(self, entry: AuditEntry) -> None:
        logger.debug(
            "audit_kafka_adapter.noop_log",
            action=str(entry.action),
            hint=(
                "baldur_dormant not installed; Kafka audit entry dropped. "
                "Install baldur-pro[dormant,kafka] to enable streaming."
            ),
        )

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        return []

    def close(self) -> None:
        return None


class NoOpWormAdapter(AuditLogAdapter):
    """Write-once-audit NoOp returned by the immutable-audit registry slot.

    The real immutable-audit adapters (object-store retention lock, log sink,
    sidecar) live in the private package. When that package is absent, this
    NoOp answers the registry slot so callers don't need to guard against
    missing providers. ``log()`` drops entries silently; the compliance /
    non-repudiation property the real adapters provide is *not* met —
    operators relying on it must install the private write-once extra
    explicitly.
    """

    def log(self, entry: AuditEntry) -> None:
        logger.debug(
            "audit_worm_adapter.noop_log",
            action=str(entry.action),
            hint=(
                "baldur_dormant not installed; WORM audit entry dropped. "
                "Install baldur-pro[dormant,aws] to enable Object Lock storage."
            ),
        )

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        return []

    def verify(self) -> bool:
        """Verify the write-once store integrity (NoOp: trivially True).

        Real implementations check retention-lock state, hash-chain
        continuity, etc. The NoOp returns True because there is nothing
        stored to violate.
        """
        return True
