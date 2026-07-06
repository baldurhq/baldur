"""
AbstractAuditLog abstract model.

이 모듈은 baldur.adapters.django.models 패키지의 내부 구현입니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from baldur.utils.time import utc_now

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


class AbstractAuditLog(models.Model if DJANGO_AVAILABLE else object):  # type: ignore[misc]
    """
    Abstract Audit Log model for continuous audit recording.

    136_EXCEPTION_HANDLER_6_ENHANCEMENTS.md Q3 보완 구현:
    - audit_event_id: WAL 복구 시 중복 제거를 위한 Unique 필드
    - ON CONFLICT (audit_event_id) DO NOTHING 지원

    특징:
    - 해시 체인 기반 무결성 검증 지원
    - WAL 복구 시 중복 삽입 방지 (2차 방어)
    - 규정 준수를 위한 감사 추적

    Subclasses should:
    - Set abstract = False in Meta
    - Optionally override db_table
    - Add project-specific indexes

    Usage:
        # In your Django app's models.py
        from baldur.adapters.django.models import AbstractAuditLog

        class AuditLog(AbstractAuditLog):
            class Meta(AbstractAuditLog.Meta):
                abstract = False
                db_table = "audit_log"
    """

    if not DJANGO_AVAILABLE:
        raise ImportError(
            "Django is required to use AbstractAuditLog. "
            "Install it with: pip install django"
        )

    # ========================================
    # Unique Event Identifier (WAL 중복 방지용)
    # ========================================
    audit_event_id = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        verbose_name="Audit Event ID",
        help_text=(
            "Unique identifier for audit event. "
            "Used for WAL recovery deduplication (ON CONFLICT DO NOTHING)."
        ),
    )

    # ========================================
    # Action & Timestamp
    # ========================================
    action = models.CharField(
        max_length=100,
        db_index=True,
        verbose_name="Action",
        help_text="Audit action type (e.g., AUTO_TUNING_ADJUSTMENT, CB_FORCE_OPEN)",
    )

    timestamp = models.DateTimeField(
        db_index=True,
        verbose_name="Timestamp",
        help_text="When the action occurred",
    )

    # ========================================
    # Actor Information
    # ========================================
    actor_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="Actor ID",
        help_text="Who performed the action",
    )

    actor_type = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Actor Type",
        help_text="Type of actor (user, system, etc.)",
    )

    actor_roles = models.JSONField(
        default=list,
        blank=True,
        verbose_name="Actor Roles",
        help_text="RBAC roles of the actor at action time",
    )

    # ========================================
    # Target Information
    # ========================================
    target_type = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Target Type",
        help_text="Type of target entity",
    )

    target_id = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="Target ID",
        help_text="ID of target entity",
    )

    # ========================================
    # Service & Domain
    # ========================================
    service_name = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Service Name",
        help_text="Service that generated the audit event",
    )

    domain = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        verbose_name="Domain",
        help_text="Business domain",
    )

    # ========================================
    # Details & Reason
    # ========================================
    reason = models.TextField(
        blank=True,
        verbose_name="Reason",
        help_text="Reason for the action",
    )

    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Details",
        help_text="Full action details as JSON",
    )

    # ========================================
    # Result
    # ========================================
    success = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name="Success",
        help_text="Whether the action succeeded",
    )

    error_message = models.TextField(
        blank=True,
        verbose_name="Error Message",
        help_text="Error message if action failed",
    )

    # ========================================
    # Integrity (Hash Chain)
    # ========================================
    integrity_hash = models.CharField(
        max_length=128,
        blank=True,
        db_index=True,
        verbose_name="Integrity Hash",
        help_text="Hash for integrity verification (hash chain)",
    )

    previous_hash = models.CharField(
        max_length=128,
        blank=True,
        verbose_name="Previous Hash",
        help_text="Hash of previous entry (for chain verification)",
    )

    sequence_number = models.BigIntegerField(
        default=0,
        db_index=True,
        verbose_name="Sequence Number",
        help_text="Monotonic sequence for ordering",
    )

    # ========================================
    # Metadata
    # ========================================
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name="Created At",
    )

    class Meta:
        abstract = True
        ordering = ["-sequence_number", "-timestamp"]
        indexes = [
            models.Index(fields=["action", "timestamp"]),
            models.Index(fields=["actor_id", "timestamp"]),
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["service_name", "action"]),
            models.Index(fields=["success", "timestamp"]),
        ]

    def __str__(self) -> str:
        return f"AuditLog({self.action}, {self.audit_event_id})"

    @classmethod
    def insert_ignore_conflict(
        cls,
        audit_event_id: str,
        **fields: Any,
    ) -> tuple[Any, bool]:
        """
        Insert with ON CONFLICT DO NOTHING semantics.

        WAL 복구 시 중복 삽입 방지 (2차 방어).

        Args:
            audit_event_id: Unique event identifier
            **fields: Other model fields

        Returns:
            Tuple of (instance, created)
            created=False if record already exists

        Example:
            log, created = AuditLog.insert_ignore_conflict(
                audit_event_id="wal:123:pg_insert",
                action="AUTO_TUNING_ADJUSTMENT",
                timestamp=datetime.now(),
                ...
            )
            if not created:
                logger.info(
                    "duplicate_audit_event",
                    audit_event_id=audit_event_id,
                )
        """
        from django.db import IntegrityError

        try:
            instance = cls.objects.create(
                audit_event_id=audit_event_id,
                **fields,
            )
            return instance, True
        except IntegrityError:
            # Unique constraint violation - record already exists
            instance = cls.objects.filter(audit_event_id=audit_event_id).first()
            return instance, False

    @classmethod
    def bulk_insert_ignore_conflict(
        cls,
        entries: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """
        Bulk insert with ON CONFLICT DO NOTHING.

        Uses PostgreSQL-specific INSERT ... ON CONFLICT DO NOTHING
        for optimal performance.

        Args:
            entries: List of field dictionaries (must include audit_event_id)

        Returns:
            Tuple of (inserted_count, skipped_count)
        """
        from django.db import connection

        if not entries:
            return 0, 0

        # PostgreSQL-specific bulk insert
        if connection.vendor == "postgresql":
            return cls._pg_bulk_insert_ignore(entries)
        # Fallback for other databases
        return cls._fallback_bulk_insert(entries)

    @classmethod
    def _pg_bulk_insert_ignore(
        cls,
        entries: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """PostgreSQL-specific bulk insert with ON CONFLICT DO NOTHING."""

        from django.db import connection

        from baldur.utils.serialization import fast_dumps_str

        if not entries:
            return 0, 0

        # Default values for missing fields (raw SQL doesn't use Django defaults)
        default_values = {
            "actor_id": "",
            "actor_type": "",
            "actor_roles": [],
            "target_type": "",
            "target_id": "",
            "service_name": "",
            "domain": "",
            "reason": "",
            "details": {},
            "success": True,
            "error_message": "",
            "integrity_hash": "",
            "previous_hash": "",
            "sequence_number": 0,
            "created_at": utc_now(),
        }

        # Normalize entries with defaults
        normalized = []
        for entry in entries:
            norm_entry = {**default_values, **entry}
            # Ensure created_at is always fresh for each entry if not provided
            if "created_at" not in entry:
                norm_entry["created_at"] = utc_now()
            normalized.append(norm_entry)

        # Build INSERT ... ON CONFLICT DO NOTHING query
        table_name = cls._meta.db_table
        fields = list(normalized[0].keys())
        placeholders = ", ".join(["%s"] * len(fields))
        columns = ", ".join(f'"{f}"' for f in fields)

        sql = f"""
            INSERT INTO "{table_name}" ({columns})
            VALUES ({placeholders})
            ON CONFLICT (audit_event_id) DO NOTHING
        """

        inserted = 0
        with connection.cursor() as cursor:
            for entry in normalized:
                # dict/list 타입 필드를 JSON 문자열로 변환 (psycopg2 호환)
                values = []
                for f in fields:
                    val = entry[f]
                    if isinstance(val, (dict, list)):
                        values.append(fast_dumps_str(val))
                    else:
                        values.append(val)
                cursor.execute(sql, values)
                if cursor.rowcount > 0:
                    inserted += 1

        skipped = len(entries) - inserted
        return inserted, skipped

    @classmethod
    def _fallback_bulk_insert(
        cls,
        entries: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Fallback bulk insert for non-PostgreSQL databases."""
        inserted = 0
        skipped = 0

        for entry in entries:
            audit_event_id = entry.pop("audit_event_id")
            _, created = cls.insert_ignore_conflict(
                audit_event_id=audit_event_id,
                **entry,
            )
            if created:
                inserted += 1
            else:
                skipped += 1

        return inserted, skipped
