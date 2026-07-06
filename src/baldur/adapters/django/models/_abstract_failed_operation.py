"""
AbstractFailedOperation abstract model.

이 모듈은 baldur.adapters.django.models 패키지의 내부 구현입니다.
"""

from __future__ import annotations

from datetime import timedelta
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


class AbstractFailedOperation(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract Dead Letter Queue model for unrecoverable failures.

    This abstract model provides:
    - Common fields for DLQ entries (status, retry, error info, etc.)
    - State transition methods (mark_as_resolved, queue_for_replay, etc.)
    - Composite indexes for efficient queries
    - No domain-specific dependencies (no hardcoded choices, no FKs)

    Subclasses should:
    - Define Domain choices specific to their business
    - Add ForeignKey fields as needed (user, etc.)
    - Set abstract = False in Meta
    - Optionally override db_table

    Attributes:
        domain: Business domain classification (no choices - subclass defines)
        failure_type: Specific failure type (e.g., 'timeout', 'validation_error')
        status: Current status in DLQ lifecycle
        entity_type: Type of related entity (e.g., 'order', 'payment')
        entity_id: ID of related entity
        error_code: Error code from external system
        error_message: Human-readable error message
        retry_count: Number of replay attempts
        max_retries: Maximum allowed retries
        snapshot_data: State snapshot for recovery
        request_data: Original request payload
        response_data: External system response
        metadata: Additional debug context
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractFailedOperation. "
            "Install it with: pip install django"
        )

    # ========================================
    # Status Choices (domain-free)
    # ========================================
    class Status(models.TextChoices):
        """State machine for DLQ item lifecycle."""

        PENDING = "pending", "Pending Review"
        REVIEWING = "reviewing", "Under Review"
        REPLAYED = "replayed", "Replay Queued"
        REQUIRES_REVIEW = "requires_review", "Requires Human Review"
        RESOLVED = "resolved", "Resolved"
        REJECTED = "rejected", "Rejected (Unrecoverable)"
        ARCHIVED = "archived", "Archived"
        EXPIRED = "expired", "Retention Expired"
        PERMANENTLY_FAILED = "permanently_failed", "Permanently Failed"

    class ResolutionType(models.TextChoices):
        """How the failure was resolved.

        Choices are advisory in Django saves (not DB-enforced), so this list
        stays a human-readable catalogue of the values actually stamped by the
        replay pipeline and the manual entry paths.
        """

        # Provenance-specific replay triggers (ResolutionTrigger).
        MANUAL_REPLAY = "manual_replay", "Manual Replay"
        AUTO_REPLAY_CIRCUIT_CLOSE = (
            "auto_replay_circuit_close",
            "Auto Replay (Circuit Close)",
        )
        SCHEDULED_BATCH = "scheduled_batch", "Scheduled Batch Replay"
        TRAFFIC_AWARE = "traffic_aware", "Traffic-Aware Replay"
        THROTTLE_REPLAY = "throttle_replay", "Throttle-Aware Replay"
        DLQ_CONSUMER = "dlq_consumer", "DLQ Consumer Replay"
        # Retained for existing rows and the chaos-context writer (not a
        # replay-pipeline write site).
        AUTO_REPLAY = "auto_replay", "Automatic Replay"
        # Manual entry paths (already stamped at their own sites).
        MANUAL_RETRY = "manual_retry", "Manual Retry"
        FORCE_REDRIVE = "force_redrive", "Force Redrive"
        DUPLICATE_SKIP = "duplicate_skip", "Duplicate Skip"
        MANUAL = "manual", "Manual Resolve"
        MANUAL_FIX = "manual_fix", "Manual Fix"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        INTERNAL_ERROR = "internal_error", "Internal Error"
        ARCHIVED = "archived", "Archived"
        MAX_RETRIES_EXHAUSTED = "max_retries_exhausted", "Max Retries Exhausted"
        TTL_EXPIRED = "ttl_expired", "TTL Expired"

    class RecommendedAction(models.TextChoices):
        """Suggested action for operators."""

        REPLAY = "replay", "Replay Operation"
        MANUAL_CHECK = "manual_check", "Manual Verification"
        ESCALATE = "escalate", "Escalate to Senior"
        ARCHIVE = "archive", "Archive (No Action)"

    # ========================================
    # Domain & Classification
    # ========================================
    domain = models.CharField(
        max_length=50,
        db_index=True,
        verbose_name="Domain",
        help_text="Business domain where the failure occurred",
    )

    failure_type = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="Failure Type",
        help_text="Specific failure classification (e.g., TIMEOUT, VALIDATION_ERROR)",
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name="Status",
    )

    # ========================================
    # Entity Reference (Generic - no FK dependencies)
    # ========================================
    entity_type = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Entity Type",
        help_text="Type of related entity (e.g., 'order', 'payment', 'subscription')",
    )

    entity_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Entity ID",
        help_text="ID of related entity",
    )

    # Additional entity references as JSON (for multiple related entities)
    entity_refs = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Entity References",
        help_text="Additional entity references as {type: id} mapping",
    )

    # ========================================
    # Snapshot Data (for recovery without original records)
    # ========================================
    snapshot_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Snapshot Data",
        help_text="Complete state snapshot for recovery without accessing original records",
    )

    # ========================================
    # Error Information
    # ========================================
    error_code = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Error Code",
    )

    error_message = models.TextField(
        blank=True,
        verbose_name="Error Message",
    )

    # ========================================
    # Retry Tracking
    # ========================================
    retry_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Retry Count",
        help_text="Number of replay attempts from DLQ",
    )

    max_retries = models.PositiveIntegerField(
        default=2,
        verbose_name="Max Retries",
        help_text="Maximum allowed replay attempts (default: 2)",
    )

    last_retry_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Last Retry At",
    )

    # ========================================
    # Forensic Context
    # ========================================
    request_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Request Data",
        help_text="Original request payload",
    )

    response_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Response Data",
        help_text="External system response",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Metadata",
        help_text="Additional debug info: timing, retry history, state snapshots",
    )

    # ========================================
    # Resolution
    # ========================================
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Resolved At",
    )

    # Note: resolved_by FK should be added by subclass
    # resolved_by = models.ForeignKey("YourUserModel", ...)

    resolution_type = models.CharField(
        max_length=30,
        choices=ResolutionType.choices,
        blank=True,
        verbose_name="Resolution Type",
    )

    resolution_note = models.TextField(
        blank=True,
        verbose_name="Resolution Note",
    )

    # ========================================
    # Recovery Hints
    # ========================================
    next_action_hint = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Next Action Hint",
        help_text="Guidance for operators (e.g., 'Verify payment in PG admin')",
    )

    recommended_action = models.CharField(
        max_length=30,
        choices=RecommendedAction.choices,
        blank=True,
        verbose_name="Recommended Action",
    )

    # ========================================
    # Lifecycle
    # ========================================
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Created At",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Updated At",
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Expires At",
        help_text="Auto-archive after retention period",
    )

    class Meta:
        abstract = True
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["domain", "status"]),
            models.Index(fields=["failure_type", "status"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def __str__(self) -> str:
        return f"[{self.domain}] {self.failure_type} - {self.status}"

    # ========================================
    # State Transition Methods
    # ========================================
    def mark_as_resolved(
        self,
        resolved_by: Any = None,
        note: str = "",
        resolution_type: str = "",
    ) -> None:
        """
        Mark this DLQ entry as resolved.

        Args:
            resolved_by: User who resolved the issue (None for system)
            note: Resolution notes
            resolution_type: How it was resolved (use ResolutionType enum values)
        """
        if not resolution_type:
            resolution_type = self.ResolutionType.MANUAL_FIX
        self.status = self.Status.RESOLVED
        self.resolved_at = timezone.now()
        if hasattr(self, "resolved_by"):
            self.resolved_by = resolved_by
        self.resolution_type = resolution_type
        self.resolution_note = note

        update_fields = [
            "status",
            "resolved_at",
            "resolution_type",
            "resolution_note",
            "updated_at",
        ]
        if hasattr(self, "resolved_by"):
            update_fields.append("resolved_by")
        self.save(update_fields=update_fields)

    def mark_as_rejected(
        self,
        resolved_by: Any = None,
        note: str = "",
    ) -> None:
        """
        Mark this DLQ entry as rejected (unrecoverable).

        Args:
            resolved_by: User who rejected the entry
            note: Rejection reason
        """
        self.status = self.Status.REJECTED
        self.resolved_at = timezone.now()
        if hasattr(self, "resolved_by"):
            self.resolved_by = resolved_by
        self.resolution_type = self.ResolutionType.REJECTED
        self.resolution_note = note

        update_fields = [
            "status",
            "resolved_at",
            "resolution_type",
            "resolution_note",
            "updated_at",
        ]
        if hasattr(self, "resolved_by"):
            update_fields.append("resolved_by")
        self.save(update_fields=update_fields)

    def queue_for_replay(self) -> None:
        """
        Queue this DLQ entry for replay.

        Raises:
            ValueError: If maximum replay attempts exceeded
        """
        if self.retry_count >= self.max_retries:
            raise ValueError(f"Maximum replay attempts ({self.max_retries}) exceeded")

        self.status = self.Status.REPLAYED
        self.retry_count += 1
        self.last_retry_at = timezone.now()
        self.save(
            update_fields=["status", "retry_count", "last_retry_at", "updated_at"]
        )

    def mark_as_reviewing(self, reviewer: Any = None) -> None:
        """
        Mark this DLQ entry as under review.

        Args:
            reviewer: User who is reviewing (stored in metadata)
        """
        self.status = self.Status.REVIEWING
        if reviewer:
            reviewer_id = getattr(reviewer, "id", reviewer)
            self.metadata["reviewer_id"] = reviewer_id
            self.metadata["review_started_at"] = timezone.now().isoformat()
        self.save(update_fields=["status", "metadata", "updated_at"])

    def revert_to_pending(self, note: str = "") -> None:
        """
        Revert from REPLAYED back to PENDING after replay failure.
        If retry_count reaches threshold, escalate to REQUIRES_REVIEW.

        Escalation Rule:
        - 1-2 failures: stays PENDING
        - 3+ failures: escalates to REQUIRES_REVIEW

        Args:
            note: Additional error information
        """
        if self.retry_count >= 3:
            self.status = self.Status.REQUIRES_REVIEW
            self.recommended_action = self.RecommendedAction.ESCALATE
        else:
            self.status = self.Status.PENDING

        if note:
            self.error_message = f"{self.error_message}\n[Replay failed] {note}".strip()
        self.save(
            update_fields=[
                "status",
                "error_message",
                "recommended_action",
                "updated_at",
            ]
        )

    def mark_as_requires_review(self, note: str = "") -> None:
        """
        Mark this DLQ entry as requiring human investigation.

        Used when:
        - Multiple replay failures indicate non-transient issue
        - Handler encounters unexpected exception
        - Data inconsistency detected

        Args:
            note: Reason for escalation
        """
        self.status = self.Status.REQUIRES_REVIEW
        self.recommended_action = self.RecommendedAction.ESCALATE
        if note:
            self.error_message = f"{self.error_message}\n[Escalated] {note}".strip()
            if self.resolution_note:
                self.resolution_note = f"{self.resolution_note} | [Escalated] {note}"
            else:
                self.resolution_note = f"[Escalated] {note}"
        self.save(
            update_fields=[
                "status",
                "error_message",
                "recommended_action",
                "resolution_note",
                "updated_at",
            ]
        )

    def mark_as_archived(self, note: str = "") -> None:
        """
        Soft-delete by marking as archived (not hard delete).

        Used for long-term retention of resolved/rejected entries.
        Archived entries are excluded from normal queries but retained for audit.

        Args:
            note: Archive reason
        """
        self.status = self.Status.ARCHIVED
        self.resolution_type = self.ResolutionType.ARCHIVED
        self.resolved_at = timezone.now()
        if note:
            self.resolution_note = note
        self.save(
            update_fields=[
                "status",
                "resolution_type",
                "resolved_at",
                "resolution_note",
                "updated_at",
            ]
        )

    def mark_as_expired(self) -> None:
        """Mark this DLQ entry as expired (retention period passed)."""
        self.status = self.Status.EXPIRED
        self.resolution_type = self.ResolutionType.EXPIRED
        self.resolved_at = timezone.now()
        self.save(
            update_fields=["status", "resolution_type", "resolved_at", "updated_at"]
        )

    # ========================================
    # Query Helpers
    # ========================================
    @property
    def is_replayable(self) -> bool:
        """Check if this entry can be replayed."""
        return (
            self.status == self.Status.PENDING and self.retry_count < self.max_retries
        )

    @property
    def age_seconds(self) -> float:
        """Get age of this DLQ entry in seconds."""
        return (timezone.now() - self.created_at).total_seconds()

    @property
    def is_sla_breached(self) -> bool:
        """
        Check if this entry has breached its SLA.

        SLA thresholds are loaded from configuration.
        """
        try:
            from baldur.settings.layered_provider import get_layered_settings
            from baldur.settings.sla import SLASettings

            # Layered read so a console edit of the sla domain (default_hours)
            # takes effect (686 D1/D5); env base when no manager is registered.
            sla_config = get_layered_settings(SLASettings, "sla")
            threshold = sla_config.get_threshold(self.domain)
            return (
                self.status == self.Status.PENDING
                and (timezone.now() - self.created_at) > threshold
            )
        except ImportError:
            # Fallback to 1 hour if service not available
            from datetime import timedelta

            return self.status == self.Status.PENDING and (
                timezone.now() - self.created_at
            ) > timedelta(hours=1)

    # ========================================
    # Factory Methods
    # ========================================
    @classmethod
    def create_from_failure(
        cls,
        domain: str,
        failure_type: str,
        entity_type: str = "",
        entity_id: str = "",
        error_code: str = "",
        error_message: str = "",
        snapshot_data: dict[str, Any] | None = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        next_action_hint: str = "",
        recommended_action: str = "",
        retention_days: int = 30,
        **extra_fields: Any,
    ) -> AbstractFailedOperation:
        """
        Factory method to create a DLQ entry from a failure.

        Args:
            domain: Business domain (project-specific)
            failure_type: Specific failure type (e.g., TIMEOUT, VALIDATION_ERROR)
            entity_type: Type of related entity (e.g., 'order', 'payment')
            entity_id: ID of related entity
            error_code: Error code from external system
            error_message: Human-readable error message
            snapshot_data: State snapshot for recovery
            request_data: Original request payload
            response_data: External system response
            metadata: Additional debug context
            next_action_hint: Guidance for operators
            recommended_action: Suggested action (replay, manual_check, etc.)
            retention_days: Days to retain before auto-archive
            **extra_fields: Additional fields (e.g., user=user_instance)

        Returns:
            Created FailedOperation instance
        """
        expires_at = timezone.now() + timedelta(days=retention_days)

        # Phase 0: metadata에 region 자동 주입 (221 설계)
        metadata = metadata or {}
        try:
            from baldur.core.cluster_identity import get_cluster_identity

            identity = get_cluster_identity()
            if identity.region:
                metadata.setdefault("region", identity.region)
        except Exception:
            pass  # Fail-Open: region 주입 실패 시 무시

        return cls.objects.create(
            domain=domain,
            failure_type=failure_type,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else "",
            error_code=error_code,
            error_message=error_message,
            snapshot_data=snapshot_data or {},
            request_data=request_data or {},
            response_data=response_data or {},
            metadata=metadata,
            next_action_hint=next_action_hint,
            recommended_action=recommended_action,
            expires_at=expires_at,
            **extra_fields,
        )
