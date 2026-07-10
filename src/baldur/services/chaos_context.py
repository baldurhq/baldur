"""
Chaos Experiment Context

Provides structured context for chaos engineering experiments.
Distinguishes intentional chaos experiments from actual failures.

Core Principle: Chaos experiments should be clearly identifiable
in the DLQ to prevent confusion with real incidents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

import structlog

from baldur.utils.time import utc_now

logger = structlog.get_logger()


# =============================================================================
# Protocols (Framework-agnostic interfaces)
# =============================================================================


class FailedOperationProtocol(Protocol):
    """Protocol for FailedOperation-like objects."""

    id: Any
    metadata: dict[str, Any] | None
    status: str
    resolution_type: str
    resolution_note: str
    resolved_at: datetime | None
    next_action_hint: str

    class Status:
        RESOLVED: str

    class ResolutionType:
        AUTO_REPLAY: str

    def save(self, update_fields: list[str] | None = None) -> None:
        """Save the operation."""
        ...


# =============================================================================
# Enums
# =============================================================================


class ChaosExperimentType(str, Enum):
    """Types of chaos experiments."""

    LATENCY_INJECTION = "latency_injection"
    ERROR_5XX = "error_5xx"
    ERROR_4XX = "error_4xx"
    TIMEOUT = "timeout"
    CONNECTION_RESET = "connection_reset"
    RATE_LIMIT = "rate_limit"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    PARTIAL_FAILURE = "partial_failure"
    CASCADING_FAILURE = "cascading_failure"


class ChaosExperimentStatus(str, Enum):
    """Status of a chaos experiment."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"


# =============================================================================
# Chaos Experiment Context
# =============================================================================


@dataclass
class ChaosExperimentContext:
    """
    Structured context for chaos engineering experiments.

    This context is stored in FailedOperation.metadata to identify
    entries that are part of intentional chaos testing.
    """

    # Experiment identification
    experiment_id: str = field(default_factory=lambda: f"chaos-{uuid.uuid4().hex[:8]}")
    experiment_name: str = ""
    experiment_type: str = ChaosExperimentType.LATENCY_INJECTION.value

    # Experiment timing
    started_at: str = field(default_factory=lambda: utc_now().isoformat())
    expected_duration_seconds: int = 300  # 5 minutes default
    expires_at: str = ""

    # Experiment configuration
    target_service: str = ""
    target_domain: str = ""
    injection_rate: float = 0.001  # 0.1% default
    injected_latency_ms: int = 0
    injected_error_code: str = ""

    # Experiment status
    status: str = ChaosExperimentStatus.ACTIVE.value
    expected_recovery: bool = True
    auto_resolve: bool = True

    # Audit trail
    initiated_by: str = ""
    initiated_from: str = ""  # e.g., "continuous_verification_task", "gameday_exercise"
    approval_ticket: str = ""  # Optional: link to approval ticket

    # Resolution
    resolved_at: str = ""
    resolution_note: str = ""

    def __post_init__(self):
        """Calculate expires_at if not provided."""
        if not self.expires_at and self.started_at:
            try:
                started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
                expires = started + timedelta(seconds=self.expected_duration_seconds)
                self.expires_at = expires.isoformat()
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage in metadata."""
        return {
            "experiment_id": self.experiment_id,
            "experiment_name": self.experiment_name,
            "experiment_type": self.experiment_type,
            "started_at": self.started_at,
            "expected_duration_seconds": self.expected_duration_seconds,
            "expires_at": self.expires_at,
            "target_service": self.target_service,
            "target_domain": self.target_domain,
            "injection_rate": self.injection_rate,
            "injected_latency_ms": self.injected_latency_ms,
            "injected_error_code": self.injected_error_code,
            "status": self.status,
            "expected_recovery": self.expected_recovery,
            "auto_resolve": self.auto_resolve,
            "initiated_by": self.initiated_by,
            "initiated_from": self.initiated_from,
            "approval_ticket": self.approval_ticket,
            "resolved_at": self.resolved_at,
            "resolution_note": self.resolution_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChaosExperimentContext:
        """Create from dictionary."""
        return cls(
            experiment_id=data.get("experiment_id", ""),
            experiment_name=data.get("experiment_name", ""),
            experiment_type=data.get(
                "experiment_type", ChaosExperimentType.LATENCY_INJECTION.value
            ),
            started_at=data.get("started_at", ""),
            expected_duration_seconds=data.get("expected_duration_seconds", 300),
            expires_at=data.get("expires_at", ""),
            target_service=data.get("target_service", ""),
            target_domain=data.get("target_domain", ""),
            injection_rate=data.get("injection_rate", 0.001),
            injected_latency_ms=data.get("injected_latency_ms", 0),
            injected_error_code=data.get("injected_error_code", ""),
            status=data.get("status", ChaosExperimentStatus.ACTIVE.value),
            expected_recovery=data.get("expected_recovery", True),
            auto_resolve=data.get("auto_resolve", True),
            initiated_by=data.get("initiated_by", ""),
            initiated_from=data.get("initiated_from", ""),
            approval_ticket=data.get("approval_ticket", ""),
            resolved_at=data.get("resolved_at", ""),
            resolution_note=data.get("resolution_note", ""),
        )

    def is_expired(self) -> bool:
        """Check if the experiment has expired."""
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            return utc_now() > expires
        except Exception:
            return False

    def mark_completed(self, note: str = "") -> None:
        """Mark experiment as completed."""
        self.status = ChaosExperimentStatus.COMPLETED.value
        self.resolved_at = utc_now().isoformat()
        self.resolution_note = note or "Experiment completed successfully"

    def mark_aborted(self, note: str = "") -> None:
        """Mark experiment as aborted."""
        self.status = ChaosExperimentStatus.ABORTED.value
        self.resolved_at = utc_now().isoformat()
        self.resolution_note = note or "Experiment aborted"


# =============================================================================
# Chaos Context Helpers
# =============================================================================


def is_chaos_experiment(operation: FailedOperationProtocol) -> bool:
    """
    Check if a FailedOperation is from a chaos experiment.

    Args:
        operation: FailedOperation instance to check

    Returns:
        True if this is a chaos experiment entry
    """
    if not operation.metadata:
        return False

    chaos_context = operation.metadata.get("chaos_experiment_context")
    return chaos_context is not None and isinstance(chaos_context, dict)


# X-Test-Mode source constant (kept consistent with xtest/dlq.py, xtest/idempotency.py)
XTEST_SOURCE = "x-test-mode"


def is_xtest_operation(operation: FailedOperationProtocol) -> bool:
    """
    Check if a FailedOperation is from X-Test-Mode.

    Args:
        operation: FailedOperation instance to check

    Returns:
        True if source="x-test-mode" in metadata
    """
    if not operation.metadata:
        return False

    source = operation.metadata.get("source")
    return source == XTEST_SOURCE


def is_synthetic_operation(operation: FailedOperationProtocol) -> bool:
    """
    Check if a FailedOperation is synthetic (Chaos or X-Test).

    Synthetic traffic detection:
    - is_chaos_experiment=True (chaos_experiment_context present)
    - source="x-test-mode" (created by X-Test-Mode)

    Args:
        operation: FailedOperation instance to check

    Returns:
        True if this is a synthetic operation (Chaos or X-Test)
    """
    return is_chaos_experiment(operation) or is_xtest_operation(operation)


def get_chaos_context(
    operation: FailedOperationProtocol,
) -> ChaosExperimentContext | None:
    """
    Extract chaos experiment context from a FailedOperation.

    Args:
        operation: FailedOperation instance

    Returns:
        ChaosExperimentContext if present, None otherwise
    """
    if not operation.metadata:
        return None

    chaos_data = operation.metadata.get("chaos_experiment_context")
    if not chaos_data or not isinstance(chaos_data, dict):
        return None

    return ChaosExperimentContext.from_dict(chaos_data)


def attach_chaos_context(
    operation: FailedOperationProtocol,
    context: ChaosExperimentContext,
) -> None:
    """
    Attach chaos experiment context to a FailedOperation.

    Args:
        operation: FailedOperation instance to update
        context: ChaosExperimentContext to attach
    """
    if operation.metadata is None:
        operation.metadata = {}

    operation.metadata["chaos_experiment_context"] = context.to_dict()

    # Add CHAOS flag indicator for easy filtering
    operation.metadata["is_chaos_experiment"] = True

    # Update next_action_hint to indicate chaos experiment
    operation.next_action_hint = (
        f"[CHAOS] Experiment ID: {context.experiment_id}. "
        f"Type: {context.experiment_type}. "
        f"{'Auto-resolve scheduled' if context.auto_resolve else 'Manual review required'}."
    )

    operation.save(update_fields=["metadata", "next_action_hint", "updated_at"])

    logger.info(
        "chaos_context.attached_experiment_operation",
        context=context.experiment_id,
        operation=operation.id,
    )


def resolve_chaos_experiment(
    operation: FailedOperationProtocol,
    note: str = "",
) -> bool:
    """
    Resolve a chaos experiment entry.

    Args:
        operation: FailedOperation from chaos experiment
        note: Resolution note

    Returns:
        True if resolved, False if not a chaos experiment
    """
    context = get_chaos_context(operation)
    if not context:
        return False

    # Update context
    context.mark_completed(note)
    if operation.metadata is None:
        operation.metadata = {}
    operation.metadata["chaos_experiment_context"] = context.to_dict()

    # Mark operation as resolved
    operation.status = operation.Status.RESOLVED
    operation.resolution_type = operation.ResolutionType.AUTO_REPLAY
    operation.resolution_note = f"[CHAOS Experiment] {note or 'Experiment completed'}"
    operation.resolved_at = utc_now()

    operation.save(
        update_fields=[
            "status",
            "resolution_type",
            "resolution_note",
            "resolved_at",
            "metadata",
            "updated_at",
        ]
    )

    logger.info(
        "chaos_context.resolved_chaos_experiment_operation",
        context=context.experiment_id,
        operation=operation.id,
    )

    return True


# =============================================================================
# Chaos Experiment Factory
# =============================================================================


def create_chaos_context(
    experiment_type: ChaosExperimentType | str,
    target_service: str = "",
    target_domain: str = "",
    duration_seconds: int = 300,
    initiated_by: str = "system",
    initiated_from: str = "continuous_verification",
    auto_resolve: bool = True,
    **kwargs,
) -> ChaosExperimentContext:
    """
    Factory function to create a ChaosExperimentContext.

    Args:
        experiment_type: Type of chaos experiment
        target_service: Target service name
        target_domain: Target domain (payment, point, etc.)
        duration_seconds: Expected duration
        initiated_by: Who initiated the experiment
        initiated_from: Source of the experiment
        auto_resolve: Whether to auto-resolve on expiry
        **kwargs: Additional context fields

    Returns:
        ChaosExperimentContext instance
    """
    if isinstance(experiment_type, ChaosExperimentType):
        experiment_type_str = experiment_type.value
    else:
        experiment_type_str = experiment_type

    return ChaosExperimentContext(
        experiment_name=kwargs.get(
            "experiment_name", f"{experiment_type_str}_{target_service}"
        ),
        experiment_type=experiment_type_str,
        target_service=target_service,
        target_domain=target_domain,
        expected_duration_seconds=duration_seconds,
        initiated_by=initiated_by,
        initiated_from=initiated_from,
        auto_resolve=auto_resolve,
        injection_rate=kwargs.get("injection_rate", 0.001),
        injected_latency_ms=kwargs.get("injected_latency_ms", 0),
        injected_error_code=kwargs.get("injected_error_code", ""),
        approval_ticket=kwargs.get("approval_ticket", ""),
    )
