"""
Recovery Session Archive Django Model.

Django abstract model for the PostgreSQL persistence store.

Purpose:
- Persistent storage of recovery sessions (survives Redis TTL expiry)
- Recovery history queries and statistics
- Audit trail and regulatory compliance

Usage:
    # In your Django project's models.py:
    from baldur.models import AbstractRecoverySessionArchive

    class RecoverySessionArchive(AbstractRecoverySessionArchive):
        class Meta(AbstractRecoverySessionArchive.Meta):
            abstract = False
            db_table = "baldur_recovery_sessions"

Reference:
    docs/baldur/middleware_system/77_RECOVERY_COORDINATOR.md#1.4
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from django.db import models
    from django.utils import timezone

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    models = None  # type: ignore
    timezone = None  # type: ignore


if TYPE_CHECKING:
    pass


class AbstractRecoverySessionArchive(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract Django model for the Recovery Session Archive.

    Characteristics:
    - Stores the full history of a recovery session
    - Stores per-step execution results (JSONB)
    - Recovery start/completion timestamps and duration
    - Trigger level and outcome status

    Schema design rationale:
    - session_id: unique recovery session ID
    - namespace: namespace targeted by the recovery
    - trigger_level: emergency level targeted by the recovery
    - status: final recovery status (COMPLETED, FAILED, ABORTED)
    - steps_data: execution result of each step (JSONB)
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractRecoverySessionArchive. "
            "Install it with: pip install django"
        )

    # ========================================
    # Status Choices
    # ========================================
    class RecoveryStatusChoice(models.TextChoices):
        """Recovery status choices."""

        NOT_STARTED = "not_started", "Not Started"
        IN_PROGRESS = "in_progress", "In Progress"
        HEALTH_CHECK = "health_check", "Health Check"
        READY_TO_RESTORE = "ready_to_restore", "Ready to Restore"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        ABORTED = "aborted", "Aborted"

    class TriggerLevelChoice(models.TextChoices):
        """Trigger level choices."""

        LEVEL_1 = "LEVEL_1", "Level 1"
        LEVEL_2 = "LEVEL_2", "Level 2"
        LEVEL_3 = "LEVEL_3", "Level 3"

    # ========================================
    # Primary Key & Identifiers
    # ========================================
    session_id = models.CharField(
        max_length=100,
        primary_key=True,
        verbose_name="Session ID",
        help_text="Unique recovery session ID (e.g., recovery-abc123)",
    )

    namespace = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="Namespace",
        help_text="Target namespace for recovery (e.g., seoul, global)",
    )

    # ========================================
    # Recovery Configuration
    # ========================================
    trigger_level = models.CharField(
        max_length=20,
        choices=TriggerLevelChoice.choices,
        db_index=True,
        verbose_name="Trigger Level",
        help_text="Target emergency level for recovery",
    )

    status = models.CharField(
        max_length=30,
        choices=RecoveryStatusChoice.choices,
        default=RecoveryStatusChoice.NOT_STARTED,
        db_index=True,
        verbose_name="Status",
        help_text="Final recovery status",
    )

    initiated_by = models.CharField(
        max_length=100,
        default="system",
        verbose_name="Initiated By",
        help_text="Recovery initiator (system or user ID)",
    )

    # ========================================
    # Steps Data (JSONB)
    # ========================================
    steps_data = models.JSONField(
        default=list,
        verbose_name="Steps Data",
        help_text="List of execution results for each step",
    )

    # ========================================
    # Timing Information
    # ========================================
    started_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Started At",
        help_text="Recovery start time",
    )

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Completed At",
        help_text="Recovery completion time",
    )

    duration_seconds = models.FloatField(
        null=True,
        blank=True,
        verbose_name="Duration (seconds)",
        help_text="Recovery duration in seconds",
    )

    # ========================================
    # Result Information
    # ========================================
    abort_reason = models.TextField(
        blank=True,
        default="",
        verbose_name="Abort Reason",
        help_text="Reason for abort (when status is ABORTED or FAILED)",
    )

    cascade_event_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        verbose_name="Cascade Event ID",
        help_text="Associated Cascade Event ID",
    )

    # ========================================
    # Approval Information (for READY_TO_RESTORE)
    # ========================================
    requires_approval = models.BooleanField(
        default=False,
        verbose_name="Requires Approval",
        help_text="Whether manual approval is required",
    )

    approved_by = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="Approved By",
        help_text="Approver (user ID)",
    )

    approved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Approved At",
        help_text="Approval time",
    )

    # ========================================
    # Metadata
    # ========================================
    metadata = models.JSONField(
        default=dict,
        verbose_name="Metadata",
        help_text="Additional metadata (region policy, idempotency info, etc.)",
    )

    # ========================================
    # Timestamps
    # ========================================
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Created At",
        help_text="Record creation time",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Updated At",
        help_text="Record last modified time",
    )

    class Meta:
        abstract = True
        ordering = ["-started_at"]
        indexes = [
            # Composite index: filter by namespace + status
            models.Index(
                fields=["namespace", "status"],
                name="idx_recovery_ns_status",
            ),
            # Composite index: start time + status (recent recovery lookups)
            models.Index(
                fields=["-started_at", "status"],
                name="idx_recovery_started_status",
            ),
            # For pending-approval lookups
            models.Index(
                fields=["requires_approval", "status"],
                name="idx_recovery_approval_status",
            ),
        ]
        verbose_name = "Recovery Session Archive"
        verbose_name_plural = "Recovery Session Archives"

    def __str__(self) -> str:
        return f"Recovery({self.session_id}): {self.namespace} - {self.status}"

    # ========================================
    # Convenience Methods
    # ========================================

    def mark_started(self) -> None:
        """Mark the recovery as started."""
        self.status = self.RecoveryStatusChoice.IN_PROGRESS
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at", "updated_at"])

    def mark_completed(self) -> None:
        """Mark the recovery as completed."""
        self.status = self.RecoveryStatusChoice.COMPLETED
        self.completed_at = timezone.now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.save(
            update_fields=["status", "completed_at", "duration_seconds", "updated_at"]
        )

    def mark_failed(self, reason: str) -> None:
        """Mark the recovery as failed."""
        self.status = self.RecoveryStatusChoice.FAILED
        self.abort_reason = reason
        self.completed_at = timezone.now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.save(
            update_fields=[
                "status",
                "abort_reason",
                "completed_at",
                "duration_seconds",
                "updated_at",
            ]
        )

    def mark_aborted(self, reason: str) -> None:
        """Mark the recovery as aborted."""
        self.status = self.RecoveryStatusChoice.ABORTED
        self.abort_reason = reason
        self.completed_at = timezone.now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.save(
            update_fields=[
                "status",
                "abort_reason",
                "completed_at",
                "duration_seconds",
                "updated_at",
            ]
        )

    def mark_ready_to_restore(self) -> None:
        """Mark the recovery as awaiting approval."""
        self.status = self.RecoveryStatusChoice.READY_TO_RESTORE
        self.requires_approval = True
        self.save(update_fields=["status", "requires_approval", "updated_at"])

    def approve(self, approved_by: str) -> None:
        """Record a manual approval."""
        self.approved_by = approved_by
        self.approved_at = timezone.now()
        self.status = self.RecoveryStatusChoice.COMPLETED
        self.completed_at = timezone.now()
        if self.started_at:
            self.duration_seconds = (
                self.completed_at - self.started_at
            ).total_seconds()
        self.save(
            update_fields=[
                "approved_by",
                "approved_at",
                "status",
                "completed_at",
                "duration_seconds",
                "updated_at",
            ]
        )

    def add_step_result(self, step_data: dict[str, Any]) -> None:
        """Append a step result."""
        if not isinstance(self.steps_data, list):
            self.steps_data = []
        self.steps_data.append(step_data)
        self.save(update_fields=["steps_data", "updated_at"])

    def get_step_count(self) -> int:
        """Return the number of completed steps."""
        if isinstance(self.steps_data, list):
            return len(self.steps_data)
        return 0

    def get_total_steps(self) -> int:
        """Return the total number of steps (from metadata)."""
        if isinstance(self.metadata, dict):
            return self.metadata.get("total_steps", 0)
        return 0

    def is_terminal(self) -> bool:
        """Return whether the session is in a terminal state."""
        return self.status in (
            self.RecoveryStatusChoice.COMPLETED,
            self.RecoveryStatusChoice.FAILED,
            self.RecoveryStatusChoice.ABORTED,
        )

    def to_summary_dict(self) -> dict[str, Any]:
        """Return a summary dictionary."""
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
