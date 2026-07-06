"""
AbstractSecurityIncident abstract model.

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


class AbstractSecurityIncident(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract Security Incident model for violations that never auto-recover.

    Security violations are immediately blocked and routed to security team.
    Domain-free: no FK dependencies on specific host app models.

    Subclasses can:
    - Add FK fields to host app models (user, order, payment, etc.)
    - Set abstract = False in Meta
    - Optionally override db_table

    Usage:
        from baldur.adapters.django.models import AbstractSecurityIncident

        class SecurityIncident(AbstractSecurityIncident):
            user = models.ForeignKey("auth.User", ...)
            class Meta(AbstractSecurityIncident.Meta):
                abstract = False
                db_table = "security_incidents"
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractSecurityIncident. "
            "Install it with: pip install django"
        )

    class IncidentType(models.TextChoices):
        """Types of security incidents."""

        WEBHOOK_SIGNATURE_INVALID = (
            "webhook_signature_invalid",
            "Webhook Signature Invalid",
        )
        PAYMENT_AMOUNT_TAMPERED = "payment_amount_tampered", "Payment Amount Tampered"
        TOKEN_FORGED = "token_forged", "Token Forged"
        UNAUTHORIZED_ACCESS = "unauthorized_access", "Unauthorized Access"
        RATE_LIMIT_ABUSE = "rate_limit_abuse", "Rate Limit Abuse"
        SUSPICIOUS_ACTIVITY = "suspicious_activity", "Suspicious Activity"
        REPLAY_ATTACK = "replay_attack", "Replay Attack Detected"
        INJECTION_ATTEMPT = "injection_attempt", "Injection Attempt"

    class Severity(models.TextChoices):
        """Severity levels for incidents."""

        CRITICAL = "critical", "Critical"
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"

    class Status(models.TextChoices):
        """Investigation status."""

        OPEN = "open", "Open"
        INVESTIGATING = "investigating", "Investigating"
        RESOLVED = "resolved", "Resolved"
        FALSE_POSITIVE = "false_positive", "False Positive"

    # Classification
    incident_type = models.CharField(
        max_length=100,
        choices=IncidentType.choices,
        db_index=True,
        verbose_name="Incident Type",
    )

    severity = models.CharField(
        max_length=20,
        choices=Severity.choices,
        db_index=True,
        verbose_name="Severity",
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
        verbose_name="Status",
    )

    # Source Information
    source_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="Source IP",
        help_text="IP address of the request origin",
    )

    user_agent = models.TextField(
        blank=True,
        verbose_name="User Agent",
    )

    # Incident Details
    description = models.TextField(
        verbose_name="Description",
        help_text="Detailed description of the security incident",
    )

    raw_request = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Raw Request",
        help_text="Sanitized request data for forensic analysis",
    )

    # Response & Resolution
    action_taken = models.TextField(
        blank=True,
        verbose_name="Action Taken",
        help_text="Immediate protective action taken",
    )

    investigation_notes = models.TextField(
        blank=True,
        verbose_name="Investigation Notes",
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Resolved At",
    )

    # Lifecycle
    detected_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Detected At",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Updated At",
    )

    # Severity mapping by incident type
    SEVERITY_BY_TYPE: dict[str, str] = {}  # Populated after class

    class Meta:
        abstract = True
        ordering = ["-detected_at"]
        indexes = [
            models.Index(fields=["incident_type", "status"]),
            models.Index(fields=["severity", "status"]),
            models.Index(fields=["source_ip", "-detected_at"]),
            models.Index(fields=["status", "-detected_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.incident_type} - {self.status}"

    def start_investigation(self, investigator: Any = None) -> None:
        """Mark incident as being investigated."""
        self.status = self.Status.INVESTIGATING
        update_fields = ["status", "updated_at"]
        self.save(update_fields=update_fields)

    def resolve(
        self,
        investigator: Any = None,
        notes: str = "",
        is_false_positive: bool = False,
    ) -> None:
        """Resolve the security incident."""
        self.status = (
            self.Status.FALSE_POSITIVE if is_false_positive else self.Status.RESOLVED
        )
        self.investigation_notes = notes
        self.resolved_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "investigation_notes",
                "resolved_at",
                "updated_at",
            ]
        )

    def add_action_taken(self, action: str) -> None:
        """Record an action taken in response to the incident."""
        timestamp = timezone.now().isoformat()
        if self.action_taken:
            self.action_taken = f"{self.action_taken}\n[{timestamp}] {action}"
        else:
            self.action_taken = f"[{timestamp}] {action}"
        self.save(update_fields=["action_taken", "updated_at"])

    @property
    def is_open(self) -> bool:
        """Check if incident is still open."""
        return self.status in (self.Status.OPEN, self.Status.INVESTIGATING)

    @property
    def age_seconds(self) -> float:
        """Get age of this incident in seconds."""
        return (timezone.now() - self.detected_at).total_seconds()

    @classmethod
    def create_incident(
        cls,
        incident_type: str,
        description: str,
        source_ip: str | None = None,
        user_agent: str = "",
        raw_request: dict | None = None,
        immediate_action: str = "",
        **extra_fields: Any,
    ) -> AbstractSecurityIncident:
        """
        Factory method to create a security incident.

        Automatically determines severity based on incident type.
        Subclasses can pass additional FK fields via **extra_fields.
        """
        severity_map = {
            cls.IncidentType.WEBHOOK_SIGNATURE_INVALID: cls.Severity.CRITICAL,
            cls.IncidentType.PAYMENT_AMOUNT_TAMPERED: cls.Severity.CRITICAL,
            cls.IncidentType.TOKEN_FORGED: cls.Severity.CRITICAL,
            cls.IncidentType.REPLAY_ATTACK: cls.Severity.CRITICAL,
            cls.IncidentType.UNAUTHORIZED_ACCESS: cls.Severity.HIGH,
            cls.IncidentType.INJECTION_ATTEMPT: cls.Severity.HIGH,
            cls.IncidentType.RATE_LIMIT_ABUSE: cls.Severity.MEDIUM,
            cls.IncidentType.SUSPICIOUS_ACTIVITY: cls.Severity.MEDIUM,
        }
        # IncidentType is a TextChoices enum (str at runtime); the dict.get
        # overload tightens the key arg type.
        severity = severity_map.get(incident_type, cls.Severity.MEDIUM)  # type: ignore[call-overload]

        return cls.objects.create(
            incident_type=incident_type,
            severity=severity,
            description=description,
            source_ip=source_ip,
            user_agent=user_agent,
            raw_request=raw_request or {},
            action_taken=(
                f"[{timezone.now().isoformat()}] {immediate_action}"
                if immediate_action
                else ""
            ),
            **extra_fields,
        )

    @classmethod
    def get_open_by_ip(cls, ip_address: str, hours: int = 24):
        """Get open incidents from a specific IP in the last N hours."""
        cutoff = timezone.now() - timedelta(hours=hours)
        return cls.objects.filter(
            source_ip=ip_address,
            status__in=[cls.Status.OPEN, cls.Status.INVESTIGATING],
            detected_at__gte=cutoff,
        )
