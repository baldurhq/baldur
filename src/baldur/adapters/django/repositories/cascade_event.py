"""Django ORM implementation for CascadeEventArchiveRepository.

Uses AbstractCascadeEventArchive (Django Model) internally.
Converts between CascadeEventData (domain) <-> Django Model.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import CascadeEventArchiveRepository
from baldur.models.cascade_event import CascadeEventData

if TYPE_CHECKING:
    from baldur.models.cascade_event_archive import AbstractCascadeEventArchive

logger = structlog.get_logger()

__all__ = ["DjangoCascadeEventArchiveRepository"]


class DjangoCascadeEventArchiveRepository(CascadeEventArchiveRepository):
    """Django ORM implementation for CascadeEventArchiveRepository.

    Uses CascadeEventArchive (concrete Django Model) internally.
    """

    def __init__(self, model: type[AbstractCascadeEventArchive] | None = None) -> None:
        if model is not None:
            self._model = model
        else:
            from baldur.models.cascade_event_archive import CascadeEventArchive

            self._model = CascadeEventArchive

    def save(self, data: CascadeEventData) -> bool:
        """Persist a cascade event record.

        Returns True if saved, False on duplicate cascade_id (idempotent).
        """
        from django.db import IntegrityError

        try:
            instance = self._to_model(data)
            instance.save()
            return True
        except IntegrityError:
            logger.debug(
                "cascade_event.save_duplicate_skipped",
                cascade_id=data.cascade_id,
            )
            return False

    def get_by_cascade_id(self, cascade_id: str) -> CascadeEventData | None:
        """Retrieve a single cascade event by ID."""
        try:
            instance = self._model.objects.get(cascade_id=cascade_id)
            return self._to_data(instance)
        except self._model.DoesNotExist:
            return None

    def find(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CascadeEventData]:
        """Query with optional filters. Results ordered by timestamp DESC."""
        qs = self._model.objects.all()
        qs = self._apply_filters(
            qs, namespace, trigger_type, start_date, end_date, is_test
        )
        qs = qs.order_by("-timestamp")[offset : offset + limit]
        return [self._to_data(r) for r in qs]

    def count(
        self,
        *,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> int:
        """Count cascade events matching filters."""
        qs = self._model.objects.all()
        qs = self._apply_filters(qs, namespace, trigger_type, start_date, end_date)
        return qs.count()

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete archived events older than cutoff."""
        deleted, _ = self._model.objects.filter(timestamp__lt=cutoff).delete()
        return deleted

    def get_chain(
        self,
        namespace: str,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[CascadeEventData]:
        """Retrieve hash chain for integrity verification. Ordered by timestamp ASC."""
        qs = self._model.objects.filter(namespace=namespace)
        if start_date:
            qs = qs.filter(timestamp__gte=start_date)
        if end_date:
            qs = qs.filter(timestamp__lte=end_date)
        qs = qs.order_by("timestamp")
        return [self._to_data(r) for r in qs]

    # --- conversion helpers ---

    def _to_data(self, instance: Any) -> CascadeEventData:
        """Django model -> CascadeEventData."""
        return CascadeEventData(
            cascade_id=instance.cascade_id,
            namespace=instance.namespace,
            trigger_type=instance.trigger_type,
            trigger_details=instance.trigger_details or {},
            effects=instance.effects or [],
            causation_chain=instance.causation_chain or [],
            previous_hash=instance.previous_hash or "",
            current_hash=instance.current_hash,
            total_effects=instance.total_effects,
            success_count=instance.success_count,
            failure_count=instance.failure_count,
            timestamp=instance.timestamp,
            archived_at=instance.archived_at,
            external_trace=instance.external_trace,
            version=instance.version or "1.0",
            is_test=instance.is_test,
        )

    def _to_model(self, data: CascadeEventData) -> Any:
        """CascadeEventData -> Django model (unsaved)."""
        return self._model(
            cascade_id=data.cascade_id,
            namespace=data.namespace,
            trigger_type=data.trigger_type,
            trigger_details=data.trigger_details,
            effects=data.effects,
            causation_chain=data.causation_chain,
            previous_hash=data.previous_hash,
            current_hash=data.current_hash,
            total_effects=data.total_effects,
            success_count=data.success_count,
            failure_count=data.failure_count,
            timestamp=data.timestamp,
            external_trace=data.external_trace,
            version=data.version,
            is_test=data.is_test,
        )

    @staticmethod
    def _apply_filters(
        qs: Any,
        namespace: str | None = None,
        trigger_type: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        is_test: bool | None = None,
    ) -> Any:
        """Apply common query filters."""
        if namespace:
            qs = qs.filter(namespace=namespace)
        if trigger_type:
            qs = qs.filter(trigger_type=trigger_type)
        if start_date:
            qs = qs.filter(timestamp__gte=start_date)
        if end_date:
            qs = qs.filter(timestamp__lte=end_date)
        if is_test is not None:
            qs = qs.filter(is_test=is_test)
        return qs
