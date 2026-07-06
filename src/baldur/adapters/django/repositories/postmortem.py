"""Django ORM implementation for PostmortemRepository.

Uses AbstractPostmortemRecord (Django Model) internally.
Converts between PostmortemData (domain) <-> Django Model.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import PostmortemData, PostmortemRepository

if TYPE_CHECKING:
    from baldur.adapters.django.models._abstract_postmortem_record import (
        AbstractPostmortemRecord,
    )

logger = structlog.get_logger()

__all__ = ["DjangoPostmortemRepository"]


class DjangoPostmortemRepository(PostmortemRepository):
    """Django ORM implementation for PostmortemRepository.

    Uses AbstractPostmortemRecord (Django Model) internally.
    Converts between PostmortemData (domain) <-> Django Model.
    """

    def __init__(self, model: type[AbstractPostmortemRecord] | None = None) -> None:
        if model is not None:
            self._model = model
        else:
            from baldur.adapters.django.models import PostmortemRecord

            self._model = PostmortemRecord

    def save(self, data: PostmortemData) -> bool:
        """Persist a postmortem record.

        Returns True if saved, False on duplicate incident_id (idempotent).
        """
        from django.db import IntegrityError

        try:
            instance = self._to_model(data)
            instance.save()
            return True
        except IntegrityError:
            # Duplicate incident_id — treat as idempotent success
            logger.debug(
                "postmortem.save_duplicate_skipped",
                incident_id=data.incident_id,
            )
            return False

    def get_by_incident_id(self, incident_id: str) -> PostmortemData | None:
        """Retrieve a single postmortem by incident ID."""
        try:
            instance = self._model.objects.get(incident_id=incident_id)
            return self._to_data(instance)
        except self._model.DoesNotExist:
            return None

    def find(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[PostmortemData]:
        """Query postmortems with optional filters."""
        qs = self._model.objects.all()
        qs = self._apply_filters(qs, start_date, end_date, service, min_duration)
        qs = qs.order_by("-started_at")[offset : offset + limit]
        return [self._to_data(r) for r in qs]

    def count(
        self,
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        service: str | None = None,
        min_duration: float | None = None,
    ) -> int:
        """Count postmortems matching filters."""
        qs = self._model.objects.all()
        qs = self._apply_filters(qs, start_date, end_date, service, min_duration)
        return qs.count()

    def update_fields(
        self,
        incident_id: str,
        fields: dict[str, Any],
    ) -> bool:
        """Partial update of specific fields.

        JSONField values are deep-merged with existing data.
        """
        record = self._model.objects.filter(incident_id=incident_id).first()
        if not record:
            return False
        model_field_names = {f.name for f in record._meta.get_fields()}
        updated_keys: list[str] = []
        for key, value in fields.items():
            if key not in model_field_names:
                continue
            existing = getattr(record, key)
            if isinstance(existing, dict) and isinstance(value, dict):
                existing.update(value)
                setattr(record, key, existing)
            else:
                setattr(record, key, value)
            updated_keys.append(key)
        if not updated_keys:
            return False
        record.save(update_fields=updated_keys)
        return True

    # --- conversion helpers ---

    def _to_data(self, instance: Any) -> PostmortemData:
        """Django model -> PostmortemData."""
        return PostmortemData(
            id=str(instance.id),
            incident_id=instance.incident_id,
            started_at=instance.started_at,
            resolved_at=instance.resolved_at,
            duration_seconds=instance.duration_seconds,
            affected_services=instance.affected_services or [],
            timeline=instance.timeline or [],
            auto_actions=instance.auto_actions or [],
            recommendations=instance.recommendations or [],
            system_snapshot=instance.system_snapshot or {},
            created_at=instance.created_at,
            source=instance.source or "auto",
        )

    def _to_model(self, data: PostmortemData) -> Any:
        """PostmortemData -> Django model (unsaved)."""
        return self._model(
            id=uuid_mod.UUID(data.id) if data.id else uuid_mod.uuid4(),
            incident_id=data.incident_id,
            started_at=data.started_at,
            resolved_at=data.resolved_at,
            duration_seconds=data.duration_seconds,
            affected_services=data.affected_services,
            timeline=data.timeline,
            auto_actions=data.auto_actions,
            recommendations=data.recommendations,
            system_snapshot=data.system_snapshot,
            source=data.source,
        )

    @staticmethod
    def _apply_filters(
        qs: Any,
        start_date: datetime | None,
        end_date: datetime | None,
        service: str | None,
        min_duration: float | None,
    ) -> Any:
        """Apply common query filters."""
        if start_date:
            qs = qs.filter(started_at__gte=start_date)
        if end_date:
            qs = qs.filter(started_at__lte=end_date)
        if min_duration is not None:
            qs = qs.filter(duration_seconds__gte=min_duration)
        if service:
            qs = qs.filter(affected_services__contains=[service])
        return qs
