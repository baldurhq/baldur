"""
Django Audit Log Adapter.

Deduplication support:
- PostgreSQL INSERT with ON CONFLICT (audit_event_id) DO NOTHING
- Duplicate insert prevention during WAL recovery (second line of defense)

Usage:
    from baldur.adapters.audit.django_adapter import DjangoAuditLogAdapter

    adapter = DjangoAuditLogAdapter(model_class=YourAuditLogModel)

    # Wire into ContinuousAuditRecorder
    from baldur.audit.continuous_audit import ContinuousAuditRecorder
    recorder = ContinuousAuditRecorder(audit_adapter=adapter)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.audit_adapter import (
    AuditAction,
    AuditEntry,
    AuditLogAdapter,
)

if TYPE_CHECKING:
    from baldur.adapters.django.models._abstract_audit_log import AbstractAuditLog


logger = structlog.get_logger()


class DjangoAuditLogAdapter(AuditLogAdapter):
    """
    Django ORM-backed Audit Log Adapter.

    Features:
    - PostgreSQL ON CONFLICT (audit_event_id) DO NOTHING support
    - Duplicate insert prevention during WAL recovery (2nd defense)
    - Hash-chain integrity field persistence
    - Bulk insert optimization

    Attributes:
        model_class: Concrete Django model inheriting from AbstractAuditLog.
            Typed against the abstract base so django-stubs sees the
            insert_ignore_conflict / bulk_insert_ignore_conflict methods
            and the action / timestamp / actor_* / target_* / details fields.
        generate_event_id: Whether to auto-generate audit_event_id.
    """

    def __init__(
        self,
        model_class: type[AbstractAuditLog],
        generate_event_id: bool = True,
    ):
        """
        Initialize DjangoAuditLogAdapter.

        Args:
            model_class: Django model class inheriting from AbstractAuditLog
            generate_event_id: Whether to auto-generate a missing audit_event_id
        """
        self._model_class = model_class
        self._generate_event_id = generate_event_id

        logger.info(
            "django_audit_adapter.initialized_model",
            model_class=model_class._meta.db_table,
        )

    def log(self, entry: AuditEntry) -> None:
        """
        Log an audit entry to database.

        Uses insert_ignore_conflict for WAL recovery deduplication.

        Args:
            entry: The audit entry to log
        """
        # Generate or extract audit_event_id
        audit_event_id = self._get_or_generate_event_id(entry)

        # Convert AuditEntry into model fields
        fields = self._entry_to_fields(entry, audit_event_id)

        # Insert, ignoring duplicates
        try:
            instance, created = self._model_class.insert_ignore_conflict(
                audit_event_id=audit_event_id,
                **fields,
            )

            if created:
                logger.debug(
                    "django_audit_adapter.logged",
                    audit_event_id=audit_event_id,
                )
            else:
                logger.debug(
                    "django_audit_adapter.duplicate_skipped",
                    audit_event_id=audit_event_id,
                )
        except Exception as e:
            logger.exception(
                "django_audit_adapter.log_failed",
                error=e,
            )
            raise

    def log_batch(
        self,
        entries: list[AuditEntry],
    ) -> tuple[int, int]:
        """
        Batch log multiple audit entries.

        Uses bulk_insert_ignore_conflict for optimal performance.

        Args:
            entries: List of audit entries

        Returns:
            Tuple of (inserted_count, skipped_count)
        """
        if not entries:
            return 0, 0

        # Convert each entry into a dict
        records = []
        for entry in entries:
            audit_event_id = self._get_or_generate_event_id(entry)
            fields = self._entry_to_fields(entry, audit_event_id)
            fields["audit_event_id"] = audit_event_id
            records.append(fields)

        # Bulk insert
        try:
            inserted, skipped = self._model_class.bulk_insert_ignore_conflict(records)

            logger.info(
                "django_audit_adapter.batch",
                inserted=inserted,
                skipped=skipped,
            )
            return inserted, skipped
        except Exception as e:
            logger.exception(
                "django_audit_adapter.batch_failed",
                error=e,
            )
            raise

    def query(
        self,
        action: AuditAction | str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEntry]:
        """
        Query audit logs from database.

        Args:
            action: Filter by action type
            target_type: Filter by target type
            target_id: Filter by target ID
            start_time: Filter from this time
            end_time: Filter until this time
            limit: Maximum entries to return

        Returns:
            List of matching audit entries
        """
        queryset = self._model_class.objects.all()

        if action:
            action_str = action.value if isinstance(action, AuditAction) else action
            queryset = queryset.filter(action=action_str)

        if target_type:
            queryset = queryset.filter(target_type=target_type)

        if target_id:
            queryset = queryset.filter(target_id=target_id)

        if start_time:
            queryset = queryset.filter(timestamp__gte=start_time)

        if end_time:
            queryset = queryset.filter(timestamp__lte=end_time)

        queryset = queryset.order_by("-timestamp")[:limit]

        return [self._model_to_entry(obj) for obj in queryset]

    def _get_or_generate_event_id(self, entry: AuditEntry) -> str:
        """
        Look up or generate the audit_event_id.

        Priority:
        1. entry.details["audit_event_id"]
        2. Derived from entry.details["wal_sequence"]
        3. Generated UUID
        """
        details = entry.details or {}

        # 1. Explicit audit_event_id
        if "audit_event_id" in details:
            return str(details["audit_event_id"])

        # 2. Derived from the WAL sequence
        if "wal_sequence" in details:
            operation = details.get("operation", "pg_insert")
            return f"wal:{details['wal_sequence']}:{operation}"

        # 3. Generated UUID
        if self._generate_event_id:
            return f"auto:{uuid.uuid4()}"

        raise ValueError("audit_event_id is required but not provided")

    def _entry_to_fields(
        self,
        entry: AuditEntry,
        audit_event_id: str,
    ) -> dict[str, Any]:
        """Convert an AuditEntry into a dict of model fields."""
        return {
            "action": (
                entry.action.value
                if isinstance(entry.action, AuditAction)
                else entry.action
            ),
            "timestamp": entry.timestamp,
            "actor_id": entry.actor_id or "",
            "actor_type": entry.actor_type or "",
            "actor_roles": entry.actor_roles or [],
            "target_type": entry.target_type or "",
            "target_id": entry.target_id or "",
            "service_name": entry.service_name or "",
            "domain": entry.domain or "",
            "reason": entry.reason or "",
            "details": entry.details or {},
            "success": entry.success,
            "error_message": entry.error_message or "",
            # Hash-chain fields (extracted from details)
            "integrity_hash": (entry.details or {})
            .get("integrity", {})
            .get("hash", ""),
            "previous_hash": (entry.details or {})
            .get("integrity", {})
            .get("previous_hash", ""),
            "sequence_number": (entry.details or {})
            .get("integrity", {})
            .get("sequence", 0),
        }

    def _model_to_entry(self, obj: AbstractAuditLog) -> AuditEntry:
        """Convert a model instance into an AuditEntry."""
        return AuditEntry(
            action=obj.action,
            timestamp=obj.timestamp,
            actor_id=obj.actor_id,
            actor_type=obj.actor_type,
            actor_roles=obj.actor_roles,
            target_type=obj.target_type,
            target_id=obj.target_id,
            service_name=obj.service_name,
            domain=obj.domain,
            reason=obj.reason,
            details={
                **obj.details,
                "audit_event_id": obj.audit_event_id,
                "integrity": {
                    "hash": obj.integrity_hash,
                    "previous_hash": obj.previous_hash,
                    "sequence": obj.sequence_number,
                },
            },
            success=obj.success,
            error_message=obj.error_message,
        )


def get_django_audit_adapter(
    model_class: type[AbstractAuditLog] | None = None,
) -> DjangoAuditLogAdapter:
    """
    Factory function for DjangoAuditLogAdapter.

    Args:
        model_class: Model inheriting from AbstractAuditLog (required)

    Returns:
        DjangoAuditLogAdapter instance

    Raises:
        ValueError: If model_class is None
    """
    if model_class is None:
        raise ValueError(
            "model_class is required. "
            "Provide a Django model class that inherits from AbstractAuditLog. "
            "Example: DjangoAuditLogAdapter(model_class=YourAuditLogModel)"
        )

    return DjangoAuditLogAdapter(model_class=model_class)
