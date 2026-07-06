"""
Django ORM implementation for SecurityIncidentRepository.

Provides persistent storage for security incidents using the
SecurityIncident Django model.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from baldur.interfaces.repositories import (
    SecurityIncidentData,
    SecurityIncidentRepository,
    SecurityIncidentStatus,
)

if TYPE_CHECKING:
    from baldur.adapters.django.models._abstract_security_incident import (
        AbstractSecurityIncident,
    )

logger = structlog.get_logger()


class DjangoSecurityIncidentRepository(SecurityIncidentRepository):
    """Django ORM implementation for SecurityIncidentRepository."""

    def __init__(self, model: type[AbstractSecurityIncident] | None = None) -> None:
        if model is not None:
            self._model = model
        else:
            from baldur.adapters.django.models import SecurityIncident

            self._model = SecurityIncident

    def _to_data(self, instance: Any) -> SecurityIncidentData:
        entity_refs: dict[str, int] = {}
        if instance.related_entity_type and instance.related_entity_id:
            try:
                entity_refs[instance.related_entity_type] = int(
                    instance.related_entity_id
                )
            except (ValueError, TypeError):
                logger.debug(
                    "django_security_incident.invalid_entity_id",
                    incident_id=instance.id,
                    related_entity_id=instance.related_entity_id,
                )

        return SecurityIncidentData(
            id=instance.id,
            incident_type=instance.incident_type,
            severity=instance.severity,
            status=instance.status,
            source_ip=instance.source_ip,
            user_agent=instance.user_agent or "",
            user_id=getattr(instance, "user_id", None),
            entity_refs=entity_refs,
            description=instance.description or "",
            raw_payload=instance.raw_request or {},
            assigned_to_id=None,
            investigation_notes=instance.investigation_notes or "",
            resolved_at=instance.resolved_at,
            created_at=instance.detected_at,
            updated_at=instance.updated_at,
        )

    def create(
        self,
        incident_type: str,
        severity: str,
        description: str = "",
        source_ip: str | None = None,
        user_agent: str = "",
        user_id: int | None = None,
        entity_refs: dict[str, int] | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> SecurityIncidentData:
        """Create a new security incident."""
        refs = entity_refs or {}
        related_entity_type = ""
        related_entity_id = ""
        if refs:
            first_key = next(iter(refs))
            related_entity_type = first_key
            related_entity_id = str(refs[first_key])

        instance = self._model.objects.create(
            incident_type=incident_type,
            severity=severity,
            description=description,
            source_ip=source_ip,
            user_agent=user_agent,
            user_id=user_id,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            raw_request=raw_payload or {},
        )
        logger.info(
            "django_security_incident.created",
            incident_id=instance.id,
            incident_type=incident_type,
            severity=severity,
        )
        return self._to_data(instance)

    def get_by_id(self, id: int) -> SecurityIncidentData | None:
        """Get a security incident by ID."""
        try:
            instance = self._model.objects.get(id=id)
            return self._to_data(instance)
        except self._model.DoesNotExist:
            return None

    def get_open_incidents(self, limit: int = 100) -> list[SecurityIncidentData]:
        """Get all open (unresolved) incidents."""
        qs = self._model.objects.filter(
            status=SecurityIncidentStatus.OPEN.value,
        ).order_by("-detected_at")[:limit]
        return [self._to_data(i) for i in qs]

    def get_by_type(
        self,
        incident_type: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by type."""
        qs = self._model.objects.filter(
            incident_type=incident_type,
        ).order_by("-detected_at")[:limit]
        return [self._to_data(i) for i in qs]

    def get_by_severity(
        self,
        severity: str,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get incidents by severity."""
        qs = self._model.objects.filter(
            severity=severity,
        ).order_by("-detected_at")[:limit]
        return [self._to_data(i) for i in qs]

    def update_status(
        self,
        id: int,
        status: str,
        investigation_notes: str = "",
        assigned_to_id: int | None = None,
    ) -> bool:
        """Update incident status."""
        from django.utils import timezone

        try:
            instance = self._model.objects.get(id=id)
        except self._model.DoesNotExist:
            return False

        instance.status = status
        update_fields = ["status", "updated_at"]

        if investigation_notes:
            instance.investigation_notes = investigation_notes
            update_fields.append("investigation_notes")

        if status == SecurityIncidentStatus.RESOLVED.value:
            instance.resolved_at = timezone.now()
            update_fields.append("resolved_at")

        instance.save(update_fields=update_fields)
        return True

    def mark_as_resolved(
        self,
        id: int,
        investigation_notes: str = "",
    ) -> bool:
        """Mark incident as resolved."""
        return self.update_status(
            id=id,
            status=SecurityIncidentStatus.RESOLVED.value,
            investigation_notes=investigation_notes,
        )

    def get_recent_by_ip(
        self,
        source_ip: str,
        hours: int = 24,
        limit: int = 100,
    ) -> list[SecurityIncidentData]:
        """Get recent incidents from a specific IP."""
        from django.utils import timezone

        cutoff = timezone.now() - timedelta(hours=hours)
        qs = self._model.objects.filter(
            source_ip=source_ip,
            detected_at__gte=cutoff,
        ).order_by("-detected_at")[:limit]
        return [self._to_data(i) for i in qs]

    def count_by_type_since(
        self,
        incident_type: str,
        since: datetime,
    ) -> int:
        """Count incidents of a type since a given time."""
        return self._model.objects.filter(
            incident_type=incident_type,
            detected_at__gte=since,
        ).count()
