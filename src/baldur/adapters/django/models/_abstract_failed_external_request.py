"""
AbstractFailedExternalRequest abstract model.

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


class AbstractFailedExternalRequest(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract Dead Letter Queue model for unrecoverable external API requests.

    도메인 중립적 설계 - 특정 비즈니스 도메인(결제, 주문 등)에 의존하지 않음.
    FK 대신 entity_type/entity_id로 느슨한 결합.

    Subclasses should:
    - Set abstract = False in Meta
    - Optionally override db_table
    - Add project-specific domain choices or fields

    Usage:
        from baldur.adapters.django.models import AbstractFailedExternalRequest

        class FailedExternalRequest(AbstractFailedExternalRequest):
            class Meta(AbstractFailedExternalRequest.Meta):
                abstract = False
                db_table = "my_failed_external_request"
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractFailedExternalRequest. "
            "Install it with: pip install django"
        )

    # 실패 유형
    FAILURE_TYPE_CHOICES = [
        ("max_retries_exceeded", "Max Retries Exceeded"),
        ("non_retryable_error", "Non-Retryable Error"),
        ("sla_timeout", "SLA Timeout Exceeded"),
        ("circuit_breaker_open", "Circuit Breaker Open"),
        ("manual_abort", "Manual Abort"),
        ("unknown", "Unknown Error"),
    ]

    # 처리 상태
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("reviewing", "Reviewing"),
        ("resolved", "Resolved"),
        ("rejected", "Rejected"),
        ("expired", "Expired"),
    ]

    # 도메인 타입
    DOMAIN_CHOICES = [
        ("external_api", "External API"),
        ("payment", "Payment"),
        ("point", "Point"),
        ("inventory", "Inventory"),
        ("webhook", "Webhook"),
        ("notification", "Notification"),
    ]

    domain = models.CharField(
        max_length=50,
        choices=DOMAIN_CHOICES,
        default="external_api",
        verbose_name="Domain",
    )

    entity_type = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Entity Type",
        help_text="Related entity type (e.g., 'order', 'payment', 'subscription')",
    )

    entity_id = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Entity ID",
        help_text="Related entity ID",
    )

    entity_refs = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Entity References",
        help_text="Additional entity references (e.g., {'user_id': 123, 'tenant_id': 'abc'})",
    )

    user_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="User ID",
    )

    external_request_id = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="External Request ID",
    )

    external_transaction_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="External Transaction ID",
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=0,
        default=0,
        verbose_name="Amount",
    )

    failure_type = models.CharField(
        max_length=30,
        choices=FAILURE_TYPE_CHOICES,
        default="unknown",
        verbose_name="Failure Type",
    )

    error_code = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Error Code",
    )

    error_message = models.TextField(
        blank=True,
        verbose_name="Error Message",
    )

    retry_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Retry Count",
    )

    last_retry_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Last Retry At",
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="Status",
    )

    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Resolved At",
    )

    resolved_by_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Resolved By ID",
    )

    resolution_note = models.TextField(
        blank=True,
        verbose_name="Resolution Note",
    )

    request_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Request Data",
    )

    response_data = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Response Data",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Metadata",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Created At",
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Updated At",
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Expires At",
    )

    class Meta:
        abstract = True
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["failure_type", "-created_at"]),
            models.Index(fields=["domain", "-created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self) -> str:
        entity_info = (
            f"{self.entity_type}:{self.entity_id}" if self.entity_type else "N/A"
        )
        return f"[{self.domain}] {self.get_failure_type_display()} {entity_info} - {self.get_status_display()}"

    def mark_as_resolved(
        self, resolved_by_id: int | None = None, note: str = ""
    ) -> None:
        """Mark as resolved."""
        self.status = "resolved"
        self.resolved_at = timezone.now()
        self.resolved_by_id = resolved_by_id
        self.resolution_note = note
        self.save(
            update_fields=[
                "status",
                "resolved_at",
                "resolved_by_id",
                "resolution_note",
                "updated_at",
            ]
        )

    def mark_as_rejected(
        self, resolved_by_id: int | None = None, note: str = ""
    ) -> None:
        """Mark as rejected (unrecoverable)."""
        self.status = "rejected"
        self.resolved_at = timezone.now()
        self.resolved_by_id = resolved_by_id
        self.resolution_note = note
        self.save(
            update_fields=[
                "status",
                "resolved_at",
                "resolved_by_id",
                "resolution_note",
                "updated_at",
            ]
        )

    @classmethod
    def create_from_failure(
        cls,
        domain: str = "external_api",
        entity_type: str = "",
        entity_id: str = "",
        entity_refs: dict[str, Any] | None = None,
        user_id: int | None = None,
        failure_type: str = "unknown",
        error_code: str = "",
        error_message: str = "",
        retry_count: int = 0,
        external_request_id: str = "",
        external_transaction_id: str = "",
        amount: Any = None,
        request_data: dict[str, Any] | None = None,
        response_data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        snapshot_data: dict[str, Any] | None = None,
    ) -> AbstractFailedExternalRequest:
        """Factory method to create a DLQ entry from an external request failure."""
        from decimal import Decimal

        try:
            from django.conf import settings as django_settings

            baldur_config = getattr(django_settings, "BALDUR", {})
        except Exception:
            baldur_config = {}
        retention_days = baldur_config.get("DLQ_RETENTION_DAYS", 30)
        expires_at = timezone.now() + timedelta(days=retention_days)

        final_metadata = metadata or {}
        if snapshot_data:
            final_metadata["snapshot_data"] = snapshot_data

        return cls.objects.create(
            domain=domain,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_refs=entity_refs or {},
            user_id=user_id,
            external_request_id=external_request_id,
            external_transaction_id=external_transaction_id,
            amount=amount if amount is not None else Decimal("0"),
            failure_type=failure_type,
            error_code=error_code,
            error_message=error_message,
            retry_count=retry_count,
            last_retry_at=timezone.now() if retry_count > 0 else None,
            request_data=request_data or {},
            response_data=response_data or {},
            metadata=final_metadata,
            expires_at=expires_at,
        )
