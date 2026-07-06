"""
Abstract Django Models for Baldur System.

This module provides domain-free abstract models that can be inherited
by any Django project. The abstract models define common fields, indexes,
and state transition methods without any domain-specific dependencies.

Usage:
    # In your Django app's models.py
    from baldur.adapters.django.models import AbstractFailedOperation

    class FailedOperation(AbstractFailedOperation):
        # Add domain-specific choices
        class Domain(models.TextChoices):
            PAYMENT = "payment"
            ORDER = "order"
            # ... your domains

        # Override domain field with choices
        domain = models.CharField(
            max_length=50,
            choices=Domain.choices,
            db_index=True
        )

        # Add project-specific FKs
        user = models.ForeignKey("auth.User", ...)

        class Meta(AbstractFailedOperation.Meta):
            abstract = False
            db_table = "failed_operations"
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

try:
    from django.conf import settings
    from django.db import models
    from django.utils import timezone

    DJANGO_AVAILABLE = True
except ImportError:
    DJANGO_AVAILABLE = False
    models = None  # type: ignore
    timezone = None  # type: ignore

if TYPE_CHECKING:
    pass


from ._abstract_audit_log import AbstractAuditLog  # noqa: E402
from ._abstract_failed_external_request import (
    AbstractFailedExternalRequest,  # noqa: E402
)
from ._abstract_failed_operation import AbstractFailedOperation  # noqa: E402
from ._abstract_postmortem_record import AbstractPostmortemRecord  # noqa: E402
from ._abstract_security_incident import AbstractSecurityIncident  # noqa: E402

__all__ = [
    "AbstractFailedOperation",
    "AbstractAuditLog",
    "AbstractPostmortemRecord",
    "AbstractFailedExternalRequest",
    "AbstractSecurityIncident",
    "PostmortemRecord",
    "FailedOperation",
    "FailedExternalRequest",
    "SecurityIncident",
    "DJANGO_AVAILABLE",
]


# =============================================================================
# Concrete PostmortemRecord (223 Host App Decoupling)
# =============================================================================


class PostmortemRecord(AbstractPostmortemRecord):
    """
    Concrete PostmortemRecord model provided by baldur package.

    이 모델은 baldur/0001_initial migration에 의해 생성된 테이블을 그대로 사용한다.
    패키지에서 직접 제공하는 concrete 모델.

    호스트 앱이 커스터마이징이 필요한 경우 AbstractPostmortemRecord를 직접 상속하면 된다.
    """

    class Meta(AbstractPostmortemRecord.Meta):
        abstract = False
        db_table = "baldur_postmortem"

    def __str__(self):
        return f"PostmortemRecord({self.incident_id})"


# =============================================================================
# Concrete Models (223 Host App Decoupling)
# Provided by the package for zero-copy installation.
# =============================================================================


class FailedOperation(AbstractFailedOperation):
    """
    Concrete DLQ model provided by the baldur package.

    Includes a swappable user FK via settings.AUTH_USER_MODEL
    and default domain choices suitable for most applications.
    """

    class Domain(models.TextChoices if DJANGO_AVAILABLE else object):  # type: ignore[misc]
        PAYMENT = "payment", "Payment"
        POINT = "point", "Point"
        INVENTORY = "inventory", "Inventory"
        WEBHOOK = "webhook", "Webhook"
        NOTIFICATION = "notification", "Notification"

    domain = models.CharField(
        max_length=50,
        choices=Domain.choices,
        db_index=True,
        verbose_name="Domain",
        help_text="Business domain where the failure occurred",
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="baldur_failed_operations",
        verbose_name="User",
    )

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="baldur_resolved_operations",
        verbose_name="Resolved By",
    )

    # Additional entity references as JSON
    entity_refs = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Entity References",
        help_text="Additional entity references as {type: id} mapping",
    )

    class Meta(AbstractFailedOperation.Meta):
        abstract = False
        db_table = "failed_operations"
        verbose_name = "Failed Operation (DLQ)"
        verbose_name_plural = "Failed Operations (DLQ)"


class FailedExternalRequest(AbstractFailedExternalRequest):
    """
    Concrete FailedExternalRequest model provided by the baldur package.
    """

    class Meta(AbstractFailedExternalRequest.Meta):
        abstract = False
        db_table = "baldur_failed_external_request"
        verbose_name = "Failed External Request (DLQ)"
        verbose_name_plural = "Failed External Requests (DLQ)"


class SecurityIncident(AbstractSecurityIncident):
    """
    Concrete SecurityIncident model provided by the baldur package.

    Domain-free version without host app FK dependencies.
    Host apps that need FKs (user, order, payment) should create their own
    concrete subclass of AbstractSecurityIncident.
    """

    # Generic user reference (no FK, domain-neutral)
    user_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="User ID",
        help_text="Associated user ID (domain-neutral, no FK)",
    )

    # Generic entity references
    related_entity_type = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Related Entity Type",
    )

    related_entity_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Related Entity ID",
    )

    class Meta(AbstractSecurityIncident.Meta):
        abstract = False
        db_table = "security_incidents"
        verbose_name = "Security Incident"
        verbose_name_plural = "Security Incidents"
