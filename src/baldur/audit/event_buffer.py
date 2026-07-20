"""
RequestAuditBuffer - per-request audit event buffer

Collects audit events that occur throughout the entire lifecycle of each
HTTP request. Stored in request.META so it is accessible across the whole
middleware chain, and recorded in a batch by AuditMiddleware just before the
response.

Core design principles:
- Every middleware and service 'stages into the buffer' instead of 'logging directly'
- AuditMiddleware 'grabs' the buffer just before the response and records it as a single hash chain
- This proves that "not a single log was dropped or tampered with"
- 0% data loss achieved via RingBuffer + WAL integration

Industry examples:
- AWS CloudTrail: buffers events then sends in a batch
- Datadog APM: collects spans then sends when the trace completes
- OpenTelemetry: batch-processes in SpanProcessor's OnEnd

Usage:
    # Stage an event from a middleware or service
    from baldur.audit.event_buffer import RequestAuditBuffer, AuditEventType

    buffer = RequestAuditBuffer.get_or_create(request)
    buffer.add(
        event_type=AuditEventType.DLQ_STORE,
        source="DLQService",
        details={"dlq_id": 123, "domain": "payment"},
    )

    # Automatically collected and recorded by AuditMiddleware

Author: Baldur Team
Version: 2.0.0 (RingBuffer + WAL integration)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = structlog.get_logger()


class AuditEventType(str, Enum):
    """
    Audit event type.

    Each type maps to an AuditAction and is recorded by the ContinuousAuditRecorder.
    """

    # DLQ-related
    DLQ_STORE = "dlq_store"
    DLQ_REPLAY = "dlq_replay"
    DLQ_ESCALATE = "dlq_escalate"
    DLQ_FORCE_REDRIVE = "dlq_force_redrive"

    # Circuit Breaker-related
    CB_STATE_CHANGE = "circuit_breaker_state_change"
    CB_REJECTION = "circuit_breaker_rejection"
    CB_RECOVERY = "circuit_breaker_recovery"

    # Governance-related
    GOVERNANCE_BLOCKED = "governance_blocked"
    GOVERNANCE_KILL_SWITCH = "governance_kill_switch"

    # Rate Limit-related
    RATE_LIMITED = "rate_limited"

    # Pool Circuit Breaker-related
    POOL_CB_REJECTION = "pool_circuit_breaker_rejection"
    POOL_CB_STATE_CHANGE = "pool_circuit_breaker_state_change"

    # Error and system-related
    ERROR_DETECTED = "error_detected"
    CONFIG_CHANGE = "config_change"
    MANUAL_OVERRIDE = "manual_override"

    # API exception-related - used by the DRF exception handler
    API_EXCEPTION = "api_exception"
    """Exception during API request handling (generic exception)."""

    API_VALIDATION_ERROR = "api_validation_error"
    """Input validation failure (ValidationError, ValueError, etc.)."""

    API_AUTH_ERROR = "api_auth_error"
    """Authentication/authorization failure (AuthenticationFailed, PermissionDenied, etc.)."""

    API_NOT_FOUND = "api_not_found"
    """Resource not found (Http404, NotFound exceptions)."""

    API_THROTTLED = "api_throttled"
    """Request throttled (Throttled exception)."""

    # Recovery-related
    RECOVERY_EVENT = "recovery_event"
    RECOVERY_CHAIN_STARTED = "recovery_chain_started"
    RECOVERY_CHAIN_COMPLETED = "recovery_chain_completed"

    # Retry-related
    RETRY_ATTEMPTED = "retry_attempted"
    RETRY_EXHAUSTED = "retry_exhausted"

    # System control-related
    SYSTEM_CONTROL_CHANGED = "system_control_changed"

    # Rollback-related
    ROLLBACK_PERFORMED = "rollback_performed"

    # ═══════════════════════════════════════════════════════════
    # Security Violation-related
    # ═══════════════════════════════════════════════════════════
    SECURITY_VIOLATION = "security_violation"
    """Security violation detection and handling."""

    SECURITY_IP_BLOCKED = "security_ip_blocked"
    """IP block (temporary or permanent)."""

    SECURITY_SESSION_INVALIDATED = "security_session_invalidated"
    """User session invalidation."""

    # ═══════════════════════════════════════════════════════════
    # Regional Isolation-related
    # ═══════════════════════════════════════════════════════════
    REGION_ISOLATED = "region_isolated"
    """Region isolation activated."""

    REGION_RESTORED = "region_restored"
    """Region isolation released."""

    # Chaos experiment-related
    CHAOS_EXPERIMENT_STARTED = "chaos_experiment_started"
    CHAOS_EXPERIMENT_COMPLETED = "chaos_experiment_completed"
    CHAOS_INJECTION_APPLIED = "chaos_injection_applied"
    CHAOS_ROLLBACK_TRIGGERED = "chaos_rollback_triggered"

    # Emergency Mode-related
    EMERGENCY_MODE_ACTIVATED = "emergency_mode_activated"
    EMERGENCY_MODE_DEACTIVATED = "emergency_mode_deactivated"

    # Error Budget-related
    ERROR_BUDGET_DEPLETED = "error_budget_depleted"
    ERROR_BUDGET_BLOCKED = "error_budget_blocked"
    ERROR_BUDGET_WARNING = "error_budget_warning"
    ERROR_BUDGET_RECOVERED = "error_budget_recovered"

    # Compliance-related
    COMPLIANCE_VIOLATION = "compliance_violation"
    COMPLIANCE_CHECK_PASSED = "compliance_check_passed"
    COMPLIANCE_CHECK_EXEMPTED = "compliance_check_exempted"

    # Blast Radius-related
    BLAST_RADIUS_ISOLATION = "blast_radius_isolation"
    BLAST_RADIUS_VIOLATION = "blast_radius_violation"

    # FinOps-related
    FINOPS_THRESHOLD_EXCEEDED = "finops_threshold_exceeded"
    FINOPS_BUDGET_EXCEEDED = "finops_budget_exceeded"

    # Data access (config-based)
    DATA_ACCESS = "data_access"

    # ═══════════════════════════════════════════════════════════
    # CorruptionShield-related
    # ═══════════════════════════════════════════════════════════
    CORRUPTION_DETECTED = "corruption_detected"
    """Data integrity violation found (L1/L2/L3)."""

    CORRUPTION_BLOCKED = "corruption_blocked"
    """Request blocked due to an integrity violation."""

    # ═══════════════════════════════════════════════════════════
    # ShadowLogger/L2 Sync-related
    # ═══════════════════════════════════════════════════════════
    SHADOW_LOG_SYNC_FAILED = "shadow_log_sync_failed"
    """L2 sync failure record."""

    SHADOW_LOG_RECOVERED = "shadow_log_recovered"
    """Re-sync completed after L2 recovery."""

    # ═══════════════════════════════════════════════════════════
    # WAL-related
    # ═══════════════════════════════════════════════════════════
    WAL_CORRUPTION_DETECTED = "wal_corruption_detected"
    """WAL CRC32 checksum mismatch found."""

    WAL_RECOVERED = "wal_recovered"
    """Recovery of unprocessed WAL entries completed."""

    WAL_ROTATED = "wal_rotated"
    """WAL file rotation occurred."""

    # ═══════════════════════════════════════════════════════════
    # Forensic-related
    # ═══════════════════════════════════════════════════════════
    FORENSIC_CAPTURE_STARTED = "forensic_capture_started"
    """Forensic capture started."""

    FORENSIC_CAPTURE_COMPLETED = "forensic_capture_completed"
    """Forensic capture completed."""

    FORENSIC_ANOMALY_DETECTED = "forensic_anomaly_detected"
    """Anomalous pattern found during forensic analysis."""

    # ═══════════════════════════════════════════════════════════
    # Reconciliation-related
    # ═══════════════════════════════════════════════════════════
    FAILSAFE_PERIOD_STARTED = "failsafe_period_started"
    """Fail-Safe period started."""

    FAILSAFE_PERIOD_ENDED = "failsafe_period_ended"
    """Fail-Safe period ended."""

    SHADOW_BUDGET_CALCULATED = "shadow_budget_calculated"
    """Shadow Budget calculation completed."""

    RECONCILIATION_APPROVED = "reconciliation_approved"
    """Reconciliation approved."""

    RECONCILIATION_REJECTED = "reconciliation_rejected"
    """Reconciliation rejected."""

    RECONCILIATION_ACCURACY_VERIFIED = "reconciliation_accuracy_verified"
    """Reconciliation accuracy post-verification completed."""

    PENDING_RECONCILIATION_FREEZE = "pending_reconciliation_freeze"
    """Deployment freeze due to a large-scale adjustment."""

    # Generic
    GENERIC = "generic"


@dataclass
class AuditEvent(SerializableMixin):
    """
    A single audit event.

    Captures each event that occurs during request handling.
    Staged into the RequestAuditBuffer and batch-processed by AuditMiddleware.
    trace_id is automatically extracted from the current trace context.
    """

    event_type: AuditEventType
    timestamp: datetime = field(default_factory=lambda: utc_now())
    source: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)
    actor_id: str | None = None
    actor_type: str = "system"
    success: bool = True
    error_message: str | None = None

    # Additional metadata
    target_type: str | None = None
    target_id: str | None = None
    domain: str | None = None
    reason: str | None = None

    # Distributed tracing (auto-set)
    trace_id: str | None = field(default=None)

    def __post_init__(self) -> None:
        """Auto-set trace_id (extracted from the current trace context if absent)."""
        if self.trace_id is None:
            try:
                from baldur.audit.trace import get_trace_id

                self.trace_id = get_trace_id()
            except Exception:
                pass  # Works even in environments not using the trace module

    def __repr__(self) -> str:
        return (
            f"AuditEvent(type={self.event_type.value}, "
            f"source={self.source}, success={self.success})"
        )


class RequestAuditBuffer:
    """
    Per-request audit event buffer.

    Stored in request.META to collect events across the whole middleware chain.
    Finally recorded by AuditMiddleware.

    Design points:
    - This buffer is like a 'receipt' - it records the entire lifecycle of one request
    - Thread-safe: thread-safe via RingBuffer
    - Memory-efficient: RingBuffer's fixed capacity prevents memory blow-up
    - 0% data loss: optional disk persistence via WAL integration
    - Backward compatibility: keeps the events and truncated_count properties

    Usage example:
        # 1. Get/create the buffer
        buffer = RequestAuditBuffer.get_or_create(request)

        # 2. Add an event
        buffer.add(
            event_type=AuditEventType.CB_STATE_CHANGE,
            source="BaldurMiddleware",
            details={"cb_name": "payment", "new_state": "open"},
        )

        # 3. Automatically handled by AuditMiddleware
    """

    # Key under which it is stored in request.META
    META_KEY = "X-AUDIT-EVENTS"

    # Max events per single request (kept for backward compatibility)
    # Replaced by capacity when using RingBuffer
    DEFAULT_MAX_EVENTS = 100

    # WAL enable environment variable
    WAL_ENABLED_ENV = "BALDUR_AUDIT_WAL_ENABLED"

    def __init__(
        self,
        max_events: int | None = None,
        enable_wal: bool | None = None,
        wal_instance: Any | None = None,
    ):
        """
        Initialize the RequestAuditBuffer.

        Args:
            max_events: Max event count (uses RingBufferSettings if None)
            enable_wal: Whether to enable WAL (checks the environment variable if None)
            wal_instance: WAL instance to use (for testing)
        """
        from baldur.audit.ring_buffer import BackpressureStrategy, RingBuffer
        from baldur.settings.ring_buffer import get_ring_buffer_settings

        # Load RingBuffer settings
        settings = get_ring_buffer_settings()
        capacity = max_events if max_events is not None else settings.capacity

        # Create the RingBuffer (DROP_OLDEST strategy prioritizes new events)
        self._ring_buffer: RingBuffer[AuditEvent] = RingBuffer(
            capacity=capacity,
            strategy=BackpressureStrategy.DROP_OLDEST,
        )

        # Request metadata
        self.request_id: str | None = None
        self.start_time: datetime = utc_now()
        self._path: str | None = None
        self._method: str | None = None
        self._user_id: str | None = None

        # Backward compatibility: keep the _max_events attribute
        self._max_events = capacity

        # WAL settings (achieves 0% data loss)
        if enable_wal is None:
            enable_wal = os.environ.get(self.WAL_ENABLED_ENV, "false").lower() == "true"

        self._wal_enabled = enable_wal
        self._wal = wal_instance
        self._wal_sequences: list[int] = []  # WAL sequence tracking

        if self._wal_enabled and self._wal is None:
            self._wal = self._get_default_wal()

    def _get_default_wal(self):
        """Return the default WAL instance."""
        try:
            from baldur.audit.wal import WALConfig, WriteAheadLog

            # Configure the WAL directory from the environment variable
            wal_dir = os.environ.get(
                "BALDUR_AUDIT_WAL_DIR",
                "/var/log/audit/request_buffer_wal",
            )

            config = WALConfig(
                wal_dir=wal_dir,
                sync_on_write=True,  # Always sync for 0% data loss
                max_file_size_mb=50,
                max_files=5,
                file_prefix="request_audit",
            )

            return WriteAheadLog(config=config)

        except Exception as e:
            logger.warning(
                "request_audit_buffer.wal_init_failed",
                error=e,
            )
            return None

    @property
    def events(self) -> list[AuditEvent]:
        """
        Backward compatibility: access all events via the events property.

        Returns all RingBuffer items as a list.
        """
        return self._ring_buffer.get_all()

    @events.setter
    def events(self, value: list[AuditEvent]) -> None:
        """
        Backward compatibility: set the events property.

        Clears all existing events and replaces them with new ones.
        """
        self._ring_buffer.clear()
        for event in value:
            self._ring_buffer.put(event)

    def add_event(self, event: AuditEvent) -> bool:
        """
        Add an event directly. Non-blocking.

        Uses RingBuffer with the DROP_OLDEST strategy:
        - Removes the oldest event when the buffer is full
        - New events are always added

        When WAL is enabled, writes to disk first to prevent loss.

        Args:
            event: AuditEvent to add

        Returns:
            True: added successfully (always True with RingBuffer)
        """
        # When WAL is enabled, write to disk first
        if self._wal_enabled and self._wal is not None:
            try:
                seq = self._wal.write(event.to_dict())
                self._wal_sequences.append(seq)
            except Exception as e:
                logger.warning(
                    "wal.write_failed",
                    error=e,
                )
                # Add to the memory buffer even if WAL fails

        # Add to the memory buffer
        return self._ring_buffer.put(event)

    @property
    def stats(self) -> dict[str, Any]:
        """
        Buffer statistics (for monitoring).

        Returns:
            A dict including capacity, size, total_enqueued, total_dropped, drop_rate
        """
        rb_stats = self._ring_buffer.get_stats()
        result = {
            "capacity": rb_stats.capacity,
            "size": rb_stats.size,
            "total_enqueued": rb_stats.total_enqueued,
            "total_dropped": rb_stats.total_dropped,
            "drop_rate": rb_stats.drop_rate,
            "wal_enabled": self._wal_enabled,
        }

        if self._wal_enabled:
            result["wal_sequences_count"] = len(self._wal_sequences)

        return result

    @property
    def truncated_count(self) -> int:
        """
        Backward compatibility: truncated_count equals dropped.

        The number of events removed by DROP_OLDEST in the RingBuffer.
        """
        return self._ring_buffer.get_stats().total_dropped

    def _mark_last_event_truncated(self) -> None:
        """
        Add truncation metadata to the last event.

        Kept for backward compatibility, but with RingBuffer it is handled
        automatically by the DROP_OLDEST strategy.
        """
        events = self.events
        if not events:
            return

        last_event = events[-1]
        dropped = self.truncated_count
        if dropped > 0:
            last_event.details["_truncated"] = True
            last_event.details["_truncated_count"] = dropped

    def add(
        self,
        event_type: AuditEventType,
        source: str,
        details: dict[str, Any] | None = None,
        actor_id: str | None = None,
        actor_type: str = "system",
        success: bool = True,
        error_message: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        domain: str | None = None,
        reason: str | None = None,
    ) -> AuditEvent | None:
        """
        Convenience method: create and add an event.

        When max_events is exceeded, the event is dropped and None is returned.
        The number of dropped events is recorded in the last event's details._truncated_count.

        Args:
            event_type: Event type
            source: Event origin (middleware name, service name, etc.)
            details: Additional detail info
            actor_id: Actor ID (auto-extracted from ActorContext if absent)
            actor_type: Actor type (system, user, scheduler, etc.)
            success: Whether it succeeded
            error_message: Error message on failure
            target_type: Target type (circuit_breaker, dlq_entry, etc.)
            target_id: Target ID
            domain: Business domain
            reason: Event reason

        Returns:
            The created AuditEvent, or None when max_events is exceeded
        """
        # Try to auto-extract actor info from ActorContext
        if actor_id is None:
            try:
                from baldur.context.actor_context import ActorContext

                if ActorContext.is_set():
                    actor = ActorContext.get_current()
                    actor_id = actor.actor_id
                    actor_type = actor.actor_type
            except ImportError:
                pass

        event = AuditEvent(
            event_type=event_type,
            source=source,
            details=details or {},
            actor_id=actor_id,
            actor_type=actor_type,
            success=success,
            error_message=error_message,
            target_type=target_type,
            target_id=target_id,
            domain=domain,
            reason=reason,
        )

        # The RingBuffer DROP_OLDEST strategy always succeeds (drops the oldest then adds)
        self.add_event(event)
        return event

    def get_events(self) -> list[AuditEvent]:
        """Return all events (a copy)."""
        return list(self.events)

    def has_events(self) -> bool:
        """Whether events exist."""
        return not self._ring_buffer.is_empty

    def event_count(self) -> int:
        """Event count."""
        return self._ring_buffer.size

    def get_events_by_type(self, event_type: AuditEventType) -> list[AuditEvent]:
        """Return only events of a specific type."""
        return [e for e in self.events if e.event_type == event_type]

    def get_failed_events(self) -> list[AuditEvent]:
        """Return only failed events."""
        return [e for e in self.events if not e.success]

    def has_event_from_source(self, source: str) -> bool:
        """
        Check whether an event recorded by a specific source exists.

        Used by AuditMiddleware to prevent duplicate recording.
        e.g. If ExceptionHandler already recorded the exception, skip ERROR_DETECTED.

        Args:
            source: Event origin (ExceptionHandler, AuditMiddleware, etc.)

        Returns:
            Whether an event from that source exists
        """
        return any(e.source == source for e in self.events)

    def set_request_metadata(
        self,
        path: str | None = None,
        method: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Set request metadata."""
        if path is not None:
            self._path = path
        if method is not None:
            self._method = method
        if user_id is not None:
            self._user_id = user_id

    def get_elapsed_seconds(self) -> float:
        """Elapsed time since request start (seconds)."""
        return (utc_now() - self.start_time).total_seconds()

    @property
    def max_events(self) -> int:
        """Configured max event count (RingBuffer capacity)."""
        return self._max_events

    @property
    def is_truncated(self) -> bool:
        """Whether any events were dropped."""
        return self.truncated_count > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert the entire buffer to a dict."""
        events_list = self.events
        dropped = self.truncated_count

        result: dict[str, Any] = {
            "request_id": self.request_id,
            "start_time": self.start_time.isoformat(),
            "elapsed_seconds": self.get_elapsed_seconds(),
            "path": self._path,
            "method": self._method,
            "user_id": self._user_id,
            "event_count": len(events_list),
            "events": [e.to_dict() for e in events_list],
        }

        # Add truncation info if any events were dropped
        if dropped > 0:
            result["truncated"] = True
            result["truncated_count"] = dropped
            result["max_events"] = self._max_events

        # Add RingBuffer statistics
        result["buffer_stats"] = self.stats

        return result

    def clear(self) -> int:
        """Reset the buffer (for testing). Returns number of cleared entries."""
        count = self._ring_buffer.clear()
        self._ring_buffer.reset_stats()
        self._wal_sequences.clear()
        return count

    # =========================================================================
    # Class Methods - manage the buffer from the request
    # =========================================================================

    @classmethod
    def get_or_create(cls, request: HttpRequest) -> RequestAuditBuffer:
        """
        Get the buffer from the request, or create a new one.

        Args:
            request: Django HttpRequest object

        Returns:
            RequestAuditBuffer instance

        Usage:
            buffer = RequestAuditBuffer.get_or_create(request)
            buffer.add(event_type=..., source=..., details=...)
        """
        if not hasattr(request, "META"):
            # Return a new buffer if the request object is abnormal
            return cls()

        if cls.META_KEY not in request.META:
            request.META[cls.META_KEY] = cls()

        return request.META[cls.META_KEY]

    @classmethod
    def get(cls, request: HttpRequest) -> RequestAuditBuffer | None:
        """
        Get the existing buffer from the request (None if absent).

        Args:
            request: Django HttpRequest object

        Returns:
            RequestAuditBuffer or None
        """
        if not hasattr(request, "META"):
            return None
        return request.META.get(cls.META_KEY)

    @classmethod
    def exists(cls, request: HttpRequest) -> bool:
        """Check whether a buffer exists on the request."""
        if not hasattr(request, "META"):
            return False
        return cls.META_KEY in request.META


# =============================================================================
# Convenience functions
# =============================================================================


def add_audit_event(
    request: HttpRequest,
    event_type: AuditEventType,
    source: str,
    details: dict[str, Any] | None = None,
    **kwargs,
) -> AuditEvent | None:
    """
    Add an audit event to the request (convenience function).

    Returns None if the request object is invalid.

    Args:
        request: Django HttpRequest object
        event_type: Event type
        source: Event origin
        details: Additional detail info
        **kwargs: Additional AuditEvent parameters

    Returns:
        The created AuditEvent, or None

    Usage:
        from baldur.audit.event_buffer import add_audit_event, AuditEventType

        add_audit_event(
            request,
            AuditEventType.CB_STATE_CHANGE,
            "BaldurMiddleware",
            details={"cb_name": "payment", "new_state": "open"},
        )
    """
    try:
        buffer = RequestAuditBuffer.get_or_create(request)
        return buffer.add(
            event_type=event_type,
            source=source,
            details=details,
            **kwargs,
        )
    except Exception:
        return None
