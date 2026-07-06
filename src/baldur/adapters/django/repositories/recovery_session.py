"""Django ORM implementation for RecoverySessionArchiveRepository.

Uses AbstractRecoverySessionArchive (Django Model) internally.
Converts between RecoverySessionData (domain) <-> Django Model.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import RecoverySessionArchiveRepository
from baldur.models.recovery_session import RecoverySessionData

if TYPE_CHECKING:
    from baldur.models.recovery_session_archive import AbstractRecoverySessionArchive

logger = structlog.get_logger()

__all__ = ["DjangoRecoverySessionArchiveRepository"]


class DjangoRecoverySessionArchiveRepository(RecoverySessionArchiveRepository):
    """Django ORM implementation for RecoverySessionArchiveRepository.

    Uses the concrete RecoverySessionArchive Django Model internally.
    """

    def __init__(
        self, model: type[AbstractRecoverySessionArchive] | None = None
    ) -> None:
        if model is not None:
            self._model = model
        else:
            from baldur.models.recovery_session_archive import (
                AbstractRecoverySessionArchive,
            )

            self._model = AbstractRecoverySessionArchive

    def save(self, data: RecoverySessionData) -> bool:
        """Persist a recovery session record.

        Returns True if saved, False on duplicate session_id (idempotent).
        """
        from django.db import IntegrityError

        try:
            instance = self._to_model(data)
            instance.save()
            return True
        except IntegrityError:
            logger.debug(
                "recovery_session.save_duplicate_skipped",
                session_id=data.session_id,
            )
            return False

    def get_by_session_id(self, session_id: str) -> RecoverySessionData | None:
        """Retrieve a single session by ID."""
        try:
            instance = self._model.objects.get(session_id=session_id)
            return self._to_data(instance)
        except self._model.DoesNotExist:
            return None

    def find(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[RecoverySessionData]:
        """Query with optional filters. Results ordered by started_at DESC."""
        qs = self._model.objects.all()
        qs = self._apply_filters(qs, namespace, status, start_date, end_date)
        qs = qs.order_by("-started_at")[offset : offset + limit]
        return [self._to_data(r) for r in qs]

    def count(
        self,
        *,
        namespace: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count sessions matching filters."""
        qs = self._model.objects.all()
        if namespace:
            qs = qs.filter(namespace=namespace)
        if status:
            qs = qs.filter(status=status)
        return qs.count()

    def update(self, data: RecoverySessionData) -> bool:
        """Full update of an existing session record."""
        updated = self._model.objects.filter(session_id=data.session_id).update(
            namespace=data.namespace,
            trigger_level=data.trigger_level,
            status=data.status,
            initiated_by=data.initiated_by,
            steps_data=data.steps_data,
            started_at=data.started_at,
            completed_at=data.completed_at,
            duration_seconds=data.duration_seconds,
            abort_reason=data.abort_reason,
            cascade_event_id=data.cascade_event_id,
            requires_approval=data.requires_approval,
            approved_by=data.approved_by,
            approved_at=data.approved_at,
            metadata=data.metadata,
        )
        return updated > 0

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived sessions older than cutoff."""
        deleted, _ = self._model.objects.filter(started_at__lt=cutoff).delete()
        return deleted

    # --- conversion helpers ---

    def _to_data(self, instance: Any) -> RecoverySessionData:
        """Django model -> RecoverySessionData."""
        return RecoverySessionData(
            session_id=instance.session_id,
            namespace=instance.namespace,
            trigger_level=instance.trigger_level,
            status=instance.status,
            initiated_by=instance.initiated_by,
            steps_data=instance.steps_data or [],
            started_at=instance.started_at,
            completed_at=instance.completed_at,
            duration_seconds=instance.duration_seconds,
            abort_reason=instance.abort_reason or "",
            cascade_event_id=instance.cascade_event_id or "",
            requires_approval=instance.requires_approval,
            approved_by=instance.approved_by or "",
            approved_at=instance.approved_at,
            metadata=instance.metadata or {},
            created_at=instance.created_at,
            updated_at=instance.updated_at,
        )

    def _to_model(self, data: RecoverySessionData) -> Any:
        """RecoverySessionData -> Django model (unsaved)."""
        return self._model(
            session_id=data.session_id,
            namespace=data.namespace,
            trigger_level=data.trigger_level,
            status=data.status,
            initiated_by=data.initiated_by,
            steps_data=data.steps_data,
            started_at=data.started_at,
            completed_at=data.completed_at,
            duration_seconds=data.duration_seconds,
            abort_reason=data.abort_reason,
            cascade_event_id=data.cascade_event_id,
            requires_approval=data.requires_approval,
            approved_by=data.approved_by,
            approved_at=data.approved_at,
            metadata=data.metadata,
        )

    @staticmethod
    def _apply_filters(
        qs: Any,
        namespace: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> Any:
        """Apply common query filters."""
        if namespace:
            qs = qs.filter(namespace=namespace)
        if status:
            qs = qs.filter(status=status)
        if start_date:
            qs = qs.filter(started_at__gte=start_date)
        if end_date:
            qs = qs.filter(started_at__lte=end_date)
        return qs
