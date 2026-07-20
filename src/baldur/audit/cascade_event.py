"""
Cascade Event model - audit trail for chained events.

Groups and records every chained action caused by a single trigger.

Features:
- Causation chain tracking
- Tamper protection (hash chain)
- End-to-end flow visualization
- External distributed tracing context (W3C/OpenTelemetry compatible)
- Manual intervention records

Usage:
    from baldur.audit.cascade_event import CascadeEvent, CascadeEffect, CascadeTrigger

    trigger = CascadeTrigger(
        trigger_type="EMERGENCY_LEVEL_CHANGED",
        event_id="evt-001",
        details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
    )

    effects = [
        CascadeEffect(
            event_id="evt-002",
            action_type="GOVERNANCE_STRICT",
            caused_by="evt-001",
            success=True,
        ),
    ]

    event = CascadeEvent(
        id="cascade-abc123",
        trigger=trigger,
        effects=effects,
        namespace="seoul",
        timestamp="2026-01-21T15:30:00Z",
    )
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.time import utc_now

# =============================================================================
# CascadeEventPriority (Phase 5: Load Shedding)
# =============================================================================


class CascadeEventPriority(IntEnum):
    """
    Cascade Event priority.

    During load shedding, lower-priority events are dropped first.

    Priority order (higher is more important):
        CRITICAL (3): Never droppable - Emergency Level change, manual
            intervention
        HIGH (2): Retained when possible - Canary rollback, Circuit Breaker
            state change
        MEDIUM (1): Dropped once the buffer threshold is exceeded - ordinary
            automated actions
        LOW (0): Dropped once the buffer warning threshold is exceeded -
            informational events

    Mirrors the priority pattern used by circuit breaker load shedding.
    """

    LOW = 0
    """Informational event - dropped at the buffer warning level."""

    MEDIUM = 1
    """Ordinary automated action - dropped past the buffer threshold."""

    HIGH = 2
    """Important action (e.g. Canary rollback) - retained when possible."""

    CRITICAL = 3
    """Emergency state change, manual intervention - never droppable."""


# Default priority mapping per trigger type
TRIGGER_TYPE_PRIORITY: dict[str, CascadeEventPriority] = {
    # CRITICAL: never droppable
    "EMERGENCY_LEVEL_CHANGED": CascadeEventPriority.CRITICAL,
    "MANUAL_INTERVENTION": CascadeEventPriority.CRITICAL,
    "MANUAL_ACTIVATION": CascadeEventPriority.CRITICAL,
    "CIRCUIT_BREAKER_OPENED": CascadeEventPriority.CRITICAL,
    # HIGH: retained when possible
    "CANARY_ROLLBACK": CascadeEventPriority.HIGH,
    "GOVERNANCE_MODE_CHANGED": CascadeEventPriority.HIGH,
    "ERROR_BUDGET_EXHAUSTED": CascadeEventPriority.HIGH,
    # MEDIUM: droppable past the threshold
    "BUDGET_MULTIPLIER_APPLIED": CascadeEventPriority.MEDIUM,
    "CIRCUIT_BREAKER_HALF_OPENED": CascadeEventPriority.MEDIUM,
    "CIRCUIT_BREAKER_CLOSED": CascadeEventPriority.MEDIUM,
    # LOW: droppable at the warning level
    "METRICS_UPDATED": CascadeEventPriority.LOW,
    "HEALTH_CHECK": CascadeEventPriority.LOW,
}


def get_priority_for_trigger(trigger_type: str) -> CascadeEventPriority:
    """
    Return the priority for a trigger type.

    Args:
        trigger_type: Trigger type

    Returns:
        Priority (MEDIUM when the type is unmapped)
    """
    return TRIGGER_TYPE_PRIORITY.get(trigger_type, CascadeEventPriority.MEDIUM)


# =============================================================================
# External Trace Context (W3C/OpenTelemetry compatible)
# =============================================================================


@dataclass
class ExternalTraceContext(SerializableMixin):
    """
    External distributed tracing context.

    Compatible with the W3C Trace Context and OpenTelemetry standards.

    Naming rationale:
    - `external_trace_id`: stays consistent with the existing `trace_id`
      pattern used by tracing
    - `external_` prefix: clearly distinguishes it from the internal
      cascade_id
    - Aligned with TracingConfig.captured_headers in this project

    Reference:
    - Circuit breaker tracing's captured_headers pattern
    - W3C Trace Context: https://www.w3.org/TR/trace-context/
    """

    trace_id: str | None = None
    """trace-id of the W3C traceparent (32 hex characters)."""

    span_id: str | None = None
    """parent-id of the W3C traceparent (16 hex characters)."""

    trace_flags: str | None = None
    """trace-flags of the W3C traceparent (e.g. "01" = sampled)."""

    baggage: dict[str, str] = field(default_factory=dict)
    """W3C Baggage header values."""

    # Vendor-specific extra IDs
    aws_xray_trace_id: str | None = None
    """AWS X-Ray trace ID (X-Amzn-Trace-Id)."""

    request_id: str | None = None
    """X-Request-ID header value."""

    correlation_id: str | None = None
    """X-Correlation-ID header value."""

    # Shortened trace_id for display purposes
    trace_id_short: str | None = None
    """Shortened trace_id (req-xxx form, for UI display)."""

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> ExternalTraceContext:
        """Extract from HTTP headers."""
        ctx = cls()

        # W3C traceparent: 00-{trace_id}-{span_id}-{flags}
        traceparent = headers.get("traceparent", "")
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 4:
                ctx.trace_id = parts[1]
                ctx.span_id = parts[2]
                ctx.trace_flags = parts[3]
                # Build the shortened trace_id
                ctx.trace_id_short = f"req-{parts[1][:8]}"

        # Other headers
        ctx.aws_xray_trace_id = headers.get("x-amzn-trace-id")
        ctx.request_id = headers.get("x-request-id")
        ctx.correlation_id = headers.get("x-correlation-id")

        # Baggage handling
        baggage_header = headers.get("baggage", "")
        if baggage_header:
            for item in baggage_header.split(","):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    ctx.baggage[key] = value

        return ctx

    @classmethod
    def from_current_otel_context(cls) -> ExternalTraceContext:
        """
        Build an ExternalTraceContext from the current OpenTelemetry span.

        When OTEL is enabled, extracts trace_id and span_id from the current
        span. When OTEL is disabled, returns an empty context.
        """
        ctx = cls()

        try:
            from baldur.observability import (
                get_current_span,
                get_current_span_id_from_otel,
                get_current_trace_id_from_otel,
                is_otel_enabled,
            )

            if not is_otel_enabled():
                return ctx

            trace_id = get_current_trace_id_from_otel()
            span_id = get_current_span_id_from_otel()

            if trace_id:
                ctx.trace_id = trace_id
                ctx.trace_id_short = f"req-{trace_id[:8]}"

            if span_id:
                ctx.span_id = span_id

            # Extract trace_flags
            span = get_current_span()
            if span is not None:
                try:
                    span_context = span.get_span_context()
                    if span_context and span_context.is_valid:
                        ctx.trace_flags = format(span_context.trace_flags, "02x")
                except Exception:
                    pass

        except ImportError:
            pass
        except Exception:
            pass

        return ctx


# =============================================================================
# Cascade Effect
# =============================================================================


@dataclass
class CascadeEffect(SerializableMixin):
    """
    Cascade effect (an individual action within a Cascade Event).

    Represents each action caused by the Cascade Event's trigger.
    """

    event_id: str
    """Unique event ID."""

    action_type: str
    """Action type (GOVERNANCE_STRICT, CANARY_ROLLBACK, BUDGET_MULTIPLIER, …)."""

    caused_by: str
    """Causing event ID (causation chain tracking)."""

    success: bool
    """Whether the action succeeded."""

    target: str | None = None
    """Target (rollout ID, service name, etc.)."""

    details: dict[str, Any] = field(default_factory=dict)
    """Detailed information."""

    error_message: str | None = None
    """Error message on failure."""

    executed_at: str | None = None
    """Execution time (ISO format)."""


# =============================================================================
# Manual Intervention Effect
# =============================================================================


class InterventionType:
    """Manual intervention type constants."""

    OVERRIDE = "OVERRIDE"  # Override an automated decision
    CANCEL = "CANCEL"  # Cancel an in-flight automation
    APPROVE = "APPROVE"  # Approve a pending automation
    REJECT = "REJECT"  # Reject a pending automation
    ESCALATE = "ESCALATE"  # Manual escalation
    DEESCALATE = "DEESCALATE"  # Manual de-escalation


@dataclass
class ManualInterventionEffect(CascadeEffect):
    """
    Effect produced by a manual intervention.

    Recorded when a human overrides the system's automated decision.

    Follows the precedence pattern used by namespace emergency atomic
    queries.
    """

    intervention_type: str = InterventionType.OVERRIDE
    """Intervention type: OVERRIDE, CANCEL, APPROVE, REJECT."""

    overridden_decision: dict[str, Any] | None = None
    """Information about the overridden automated decision."""

    justification: str | None = None
    """Reason for the intervention."""

    approved_by: str | None = None
    """Approver (when two-person approval applies)."""

    related_cascade_id: str | None = None
    """Related Cascade ID (reference to the existing automation flow)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManualInterventionEffect:
        """Build from a dictionary."""
        return cls(
            event_id=data["event_id"],
            action_type=data["action_type"],
            caused_by=data["caused_by"],
            success=data["success"],
            target=data.get("target"),
            details=data.get("details", {}),
            error_message=data.get("error_message"),
            executed_at=data.get("executed_at"),
            intervention_type=data.get("intervention_type", InterventionType.OVERRIDE),
            overridden_decision=data.get("overridden_decision"),
            justification=data.get("justification"),
            approved_by=data.get("approved_by"),
            related_cascade_id=data.get("related_cascade_id"),
        )


# =============================================================================
# Cascade Trigger
# =============================================================================


@dataclass
class CascadeTrigger(SerializableMixin):
    """
    Cascade trigger (the starting point of a Cascade Event).

    Holds information about the initial event that produced the Cascade Event.
    """

    trigger_type: str
    """Trigger type (EMERGENCY_LEVEL_CHANGED, MANUAL_ACTIVATION, …)."""

    event_id: str
    """Trigger event ID."""

    details: dict[str, Any] = field(default_factory=dict)
    """Trigger details."""

    triggered_by: str | None = None
    """Actor that fired the trigger (user, system)."""


# =============================================================================
# Cascade Event
# =============================================================================


@dataclass
class CascadeEvent(SerializableMixin):
    """
    Cascade event.

    Groups and records every chained action caused by a single trigger.

    Features:
    - Causation chain tracking
    - Tamper protection (hash chain)
    - End-to-end flow visualization

    Example:
        >>> trigger = CascadeTrigger(
        ...     trigger_type="EMERGENCY_LEVEL_CHANGED",
        ...     event_id="evt-001",
        ...     details={"old_level": "NORMAL", "new_level": "LEVEL_3"},
        ... )
        >>> effects = [
        ...     CascadeEffect(
        ...         event_id="evt-002",
        ...         action_type="GOVERNANCE_STRICT",
        ...         caused_by="evt-001",
        ...         success=True,
        ...     ),
        ... ]
        >>> event = CascadeEvent(
        ...     id="cascade-abc123",
        ...     trigger=trigger,
        ...     effects=effects,
        ...     namespace="seoul",
        ...     timestamp="2026-01-21T15:30:00Z",
        ... )
    """

    id: str
    """Unique Cascade Event ID."""

    trigger: CascadeTrigger
    """Trigger information."""

    effects: list[CascadeEffect]
    """List of cascade effects."""

    namespace: str
    """Namespace."""

    timestamp: str
    """Creation time (ISO format)."""

    # Hash Chain
    previous_hash: str | None = None
    """Hash of the previous CascadeEvent."""

    current_hash: str | None = None
    """Hash of the current CascadeEvent."""

    # External distributed tracing context (W3C/OpenTelemetry compatible)
    external_trace: ExternalTraceContext | None = None
    """Trace Context of the external system."""

    # Metadata
    version: str = "1.0"
    """Schema version."""

    is_test: bool = False
    """Whether this is a test-environment event (True under X-Test-Mode)."""

    total_effects: int = field(default=0, init=False)
    """Total number of effects."""

    success_count: int = field(default=0, init=False)
    """Number of successful effects."""

    failure_count: int = field(default=0, init=False)
    """Number of failed effects."""

    def __post_init__(self) -> None:
        """Post-initialization processing."""
        self.total_effects = len(self.effects)
        self.success_count = sum(1 for e in self.effects if e.success)
        self.failure_count = self.total_effects - self.success_count

    def get_causation_chain(self) -> list[str]:
        """Return the causation chain."""
        chain = [self.trigger.event_id]
        for effect in self.effects:
            if effect.event_id not in chain:
                chain.append(effect.event_id)
        return chain

    def calculate_hash(self) -> str:
        """
        Compute the hash of the current event.

        Produces a SHA-256 hash for tamper protection.
        """
        from baldur.utils.serialization import fast_canonical_dumps

        content = {
            "id": self.id,
            "trigger": self.trigger.to_dict(),
            "effects": [e.to_dict() for e in self.effects],
            "namespace": self.namespace,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
        }
        return hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dictionary."""
        result = {
            "id": self.id,
            "trigger": self.trigger.to_dict(),
            "effects": [e.to_dict() for e in self.effects],
            "causation_chain": self.get_causation_chain(),
            "namespace": self.namespace,
            "timestamp": self.timestamp,
            "previous_hash": self.previous_hash,
            "current_hash": self.current_hash,
            "version": self.version,
            "is_test": self.is_test,
            "total_effects": self.total_effects,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
        }

        if self.external_trace:
            result["external_trace"] = self.external_trace.to_dict()

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CascadeEvent:
        """Build from a dictionary."""
        trigger = CascadeTrigger.from_dict(data["trigger"])

        effects: list[CascadeEffect] = []
        for e in data.get("effects", []):
            # Check whether this is a ManualInterventionEffect
            if "intervention_type" in e:
                effects.append(ManualInterventionEffect.from_dict(e))
            else:
                effects.append(CascadeEffect.from_dict(e))

        external_trace = None
        if "external_trace" in data and data["external_trace"]:
            external_trace = ExternalTraceContext.from_dict(data["external_trace"])

        return cls(
            id=data["id"],
            trigger=trigger,
            effects=effects,
            namespace=data["namespace"],
            timestamp=data["timestamp"],
            previous_hash=data.get("previous_hash"),
            current_hash=data.get("current_hash"),
            external_trace=external_trace,
            version=data.get("version", "1.0"),
            is_test=data.get("is_test", False),
        )


# =============================================================================
# Helper Functions
# =============================================================================


def generate_cascade_id() -> str:
    """Generate a Cascade Event ID."""
    return f"cascade-{uuid.uuid4().hex[:12]}"


def generate_event_id() -> str:
    """Generate an event ID."""
    return f"evt-{uuid.uuid4().hex[:8]}"


def get_current_timestamp() -> str:
    """Return the current time in ISO format."""
    return utc_now().isoformat()
