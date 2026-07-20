"""Recovery Session domain model.

Framework-agnostic dataclass replacing AbstractRecoverySessionArchive's data role.
Django Abstract Model remains as adapter-internal persistence detail.

Reference:
    models/recovery_session_archive.py (original Django Abstract Model)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin

__all__ = [
    "RecoveryStatus",
    "TriggerLevel",
    "RecoveryStepData",
    "RecoverySessionData",
    "VALID_TRANSITIONS",
]


class RecoveryStatus(str, Enum):
    """Recovery session status."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    HEALTH_CHECK = "health_check"
    READY_TO_RESTORE = "ready_to_restore"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class TriggerLevel(str, Enum):
    """Trigger emergency level."""

    LEVEL_1 = "LEVEL_1"
    LEVEL_2 = "LEVEL_2"
    LEVEL_3 = "LEVEL_3"


VALID_TRANSITIONS: dict[RecoveryStatus, set[RecoveryStatus]] = {
    RecoveryStatus.NOT_STARTED: {RecoveryStatus.IN_PROGRESS, RecoveryStatus.ABORTED},
    RecoveryStatus.IN_PROGRESS: {
        RecoveryStatus.HEALTH_CHECK,
        RecoveryStatus.COMPLETED,
        RecoveryStatus.FAILED,
        RecoveryStatus.ABORTED,
    },
    RecoveryStatus.HEALTH_CHECK: {
        RecoveryStatus.READY_TO_RESTORE,
        RecoveryStatus.FAILED,
        RecoveryStatus.ABORTED,
    },
    RecoveryStatus.READY_TO_RESTORE: {
        RecoveryStatus.COMPLETED,
        RecoveryStatus.ABORTED,
    },
    # Terminal states — no outgoing transitions
    RecoveryStatus.COMPLETED: set(),
    RecoveryStatus.FAILED: set(),
    RecoveryStatus.ABORTED: set(),
}


@dataclass
class RecoveryStepData(SerializableMixin):
    """Recovery step archive data.

    Replaces RecoveryStepArchiveData from service layer.
    """

    step_type: str
    """Step type (RecoveryStepType.value)."""

    order: int
    """Execution order."""

    status: str
    """Status (RecoveryStatus.value)."""

    wait_after_seconds: int = 0
    """Wait time after completion."""

    params: dict[str, Any] = field(default_factory=dict)
    """Step parameters."""

    started_at: str | None = None
    """Start time (ISO 8601)."""

    completed_at: str | None = None
    """Completion time (ISO 8601)."""

    error_message: str | None = None
    """Error message."""

    execution_time_ms: int | None = None
    """Execution time (milliseconds)."""

    retry_count: int = 0
    """Retry count."""


@dataclass
class RecoverySessionData(SerializableMixin):
    """Framework-agnostic Recovery Session domain model.

    Replaces AbstractRecoverySessionArchive's data role and
    RecoverySessionArchiveData from service layer.
    """

    session_id: str
    """Unique recovery session ID."""

    namespace: str
    """Target namespace for recovery."""

    trigger_level: str
    """Target emergency level (TriggerLevel enum value)."""

    status: str = RecoveryStatus.NOT_STARTED.value
    """Recovery status (RecoveryStatus enum value)."""

    initiated_by: str = "system"
    """Recovery initiator (system or user ID)."""

    steps_data: list[dict[str, Any]] = field(default_factory=list)
    """List of execution results for each step."""

    started_at: datetime | None = None
    """Recovery start time."""

    completed_at: datetime | None = None
    """Recovery completion time."""

    duration_seconds: float | None = None
    """Recovery duration in seconds."""

    abort_reason: str = ""
    """Reason for abort (when status is ABORTED or FAILED)."""

    cascade_event_id: str = ""
    """Associated Cascade Event ID."""

    requires_approval: bool = False
    """Whether manual approval is required."""

    approved_by: str = ""
    """Approver (user ID)."""

    approved_at: datetime | None = None
    """Approval time."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata (region policy, idempotency, etc.)."""

    created_at: datetime | None = None
    """Record creation time."""

    updated_at: datetime | None = None
    """Record last modified time."""

    # --- State transition validation ---

    def _validate_transition(self, target: RecoveryStatus) -> None:
        """Validate state transition against VALID_TRANSITIONS.

        Raises:
            InvalidStateTransitionError: If transition is not allowed.
        """
        from baldur.core.exceptions import InvalidStateTransitionError

        allowed = VALID_TRANSITIONS.get(RecoveryStatus(self.status), set())
        if target not in allowed:
            raise InvalidStateTransitionError(
                current=self.status,
                target=target.value,
                entity_id=self.session_id,
            )

    # --- Pure query methods ---

    def get_step_count(self) -> int:
        """Return count of completed steps."""
        if isinstance(self.steps_data, list):
            return len(self.steps_data)
        return 0

    def get_total_steps(self) -> int:
        """Return total step count from metadata."""
        if isinstance(self.metadata, dict):
            return self.metadata.get("total_steps", 0)
        return 0

    def is_terminal(self) -> bool:
        """Check if session is in a terminal state."""
        return self.status in (
            RecoveryStatus.COMPLETED.value,
            RecoveryStatus.FAILED.value,
            RecoveryStatus.ABORTED.value,
        )

    def to_summary_dict(self) -> dict[str, Any]:
        """Return summary dictionary (13-field subset with computed fields)."""
        return {
            "session_id": self.session_id,
            "namespace": self.namespace,
            "trigger_level": self.trigger_level,
            "status": self.status,
            "initiated_by": self.initiated_by,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "duration_seconds": self.duration_seconds,
            "step_count": self.get_step_count(),
            "total_steps": self.get_total_steps(),
            "abort_reason": self.abort_reason or None,
            "requires_approval": self.requires_approval,
            "approved_by": self.approved_by or None,
        }

    # --- State mutation methods (field changes only, no .save()) ---

    def mark_started(self) -> None:
        """Mark session as started."""
        from baldur.utils.time import utc_now

        self._validate_transition(RecoveryStatus.IN_PROGRESS)
        self.status = RecoveryStatus.IN_PROGRESS.value
        self.started_at = utc_now()

    def mark_completed(self) -> None:
        """Mark session as completed with duration calculation."""
        from baldur.utils.time import utc_now

        self._validate_transition(RecoveryStatus.COMPLETED)
        self.status = RecoveryStatus.COMPLETED.value
        self.completed_at = utc_now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def mark_failed(self, reason: str) -> None:
        """Mark session as failed."""
        from baldur.utils.time import utc_now

        self._validate_transition(RecoveryStatus.FAILED)
        self.status = RecoveryStatus.FAILED.value
        self.abort_reason = reason
        self.completed_at = utc_now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def mark_aborted(self, reason: str) -> None:
        """Mark session as aborted."""
        from baldur.utils.time import utc_now

        self._validate_transition(RecoveryStatus.ABORTED)
        self.status = RecoveryStatus.ABORTED.value
        self.abort_reason = reason
        self.completed_at = utc_now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def mark_ready_to_restore(self) -> None:
        """Mark session as ready to restore (pending approval)."""
        self._validate_transition(RecoveryStatus.READY_TO_RESTORE)
        self.status = RecoveryStatus.READY_TO_RESTORE.value
        self.requires_approval = True

    def approve(self, approved_by: str) -> None:
        """Process manual approval and mark as completed."""
        from baldur.utils.time import utc_now

        self._validate_transition(RecoveryStatus.COMPLETED)
        self.approved_by = approved_by
        self.approved_at = utc_now()
        self.status = RecoveryStatus.COMPLETED.value
        self.completed_at = utc_now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()

    def add_step_result(self, step_data: dict[str, Any]) -> None:
        """Add step execution result."""
        if not isinstance(self.steps_data, list):
            self.steps_data = []
        self.steps_data.append(step_data)
