"""
AbstractPostmortemRecord abstract model.

This module is an internal implementation of the
baldur.adapters.django.models package.
"""

from __future__ import annotations

from typing import Any

try:
    from django.db import models
    from django.utils import timezone

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    models = None  # type: ignore
    timezone = None  # type: ignore


class AbstractPostmortemRecord(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract model for the post-mortem persistence store.

    Persists to PostgreSQL so records survive a server restart. Resolves the
    limits of the in-memory store (100 records max, inconsistency across
    workers).

    Attributes:
        incident_id: Unique incident identifier
        started_at: Incident start time
        resolved_at: Incident end time
        duration_seconds: Incident duration (seconds)
        affected_services: List of affected services
        timeline: Chronological event record
        auto_actions: Recovery actions performed automatically
        recommendations: List of recommendations
        system_snapshot: System state snapshot at the time of the incident
        created_at: Record creation time
        source: Origin of the record (auto/manual)
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractPostmortemRecord. "
            "Install it with: pip install django"
        )

    class Source(models.TextChoices):
        """Origin of the post-mortem record."""

        AUTO = "auto", "Automatic (System Generated)"
        MANUAL = "manual", "Manual (User Created)"

    # ========================================
    # Primary Identifier
    # ========================================
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        verbose_name="ID",
    )

    incident_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        verbose_name="Incident ID",
        help_text="Unique identifier for the incident",
    )

    # ========================================
    # Timing Information
    # ========================================
    started_at = models.DateTimeField(
        db_index=True,
        verbose_name="Incident Start Time",
        help_text="When the incident started",
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Incident Resolution Time",
        help_text="When the incident was resolved",
    )

    duration_seconds = models.FloatField(
        default=0.0,
        db_index=True,
        verbose_name="Duration (seconds)",
        help_text="Total duration of the incident in seconds",
    )

    # ========================================
    # Incident Details (JSON Fields)
    # ========================================
    affected_services = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Affected Services",
        help_text="List of services impacted by the incident",
    )

    timeline = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Timeline",
        help_text="Chronological list of events during the incident",
    )

    auto_actions = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Automatic Actions",
        help_text="List of automatic recovery actions performed",
    )

    recommendations = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Recommendations",
        help_text="Suggested actions for future prevention",
    )

    system_snapshot = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="System Snapshot",
        help_text="System state snapshot at the time of incident",
    )

    # ========================================
    # Metadata
    # ========================================
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Record Created At",
    )

    source = models.CharField(
        max_length=20,
        choices=Source.choices,
        default=Source.AUTO,
        db_index=True,
        verbose_name="Source",
        help_text="How this post-mortem was created (auto/manual)",
    )

    class Meta:
        abstract = True
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["-started_at", "-duration_seconds"]),
            models.Index(fields=["source", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"Postmortem {self.incident_id} ({self.started_at})"

    def to_dict(self) -> dict[str, Any]:
        """Convert the record into a dict (for API responses)."""
        return {
            "id": str(self.id),
            "incident_id": self.incident_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "duration_seconds": self.duration_seconds,
            "affected_services": self.affected_services,
            "timeline": self.timeline,
            "auto_actions": self.auto_actions,
            "recommendations": self.recommendations,
            "system_snapshot": self.system_snapshot,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "source": self.source,
        }
