"""
DjangoSecurityIncidentRepository unit tests (311 — Phase 2).

Uses mock Django model to verify CRUD operations and _to_data mapping
without requiring a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from baldur.interfaces.repositories import (
    SecurityIncidentData,
    SecurityIncidentStatus,
)


def _make_model_instance(**overrides):
    """Create a mock Django SecurityIncident model instance."""
    defaults = {
        "id": 1,
        "incident_type": "webhook_signature_invalid",
        "severity": "critical",
        "status": SecurityIncidentStatus.OPEN.value,
        "source_ip": "10.0.0.1",
        "user_agent": "curl/7.68.0",
        "user_id": 42,
        "description": "Test incident",
        "raw_request": {"path": "/api/webhook"},
        "action_taken": "",
        "investigation_notes": "",
        "resolved_at": None,
        "detected_at": datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 7, 12, 0, 0, tzinfo=UTC),
        "related_entity_type": "",
        "related_entity_id": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def mock_model():
    """Create a mock Django model class."""
    model = MagicMock()
    model.DoesNotExist = type("DoesNotExist", (Exception,), {})
    return model


@pytest.fixture
def repo(mock_model):
    """Create DjangoSecurityIncidentRepository with mock model."""
    from baldur.adapters.django.security_incident import (
        DjangoSecurityIncidentRepository,
    )

    return DjangoSecurityIncidentRepository(model=mock_model)


class TestDjangoSecurityIncidentToDataBehavior:
    """Verify _to_data correctly maps Django model fields to DTO."""

    def test_basic_field_mapping(self, repo):
        """Model fields map to correct DTO fields."""
        instance = _make_model_instance()
        data = repo._to_data(instance)

        assert isinstance(data, SecurityIncidentData)
        assert data.id == 1
        assert data.incident_type == "webhook_signature_invalid"
        assert data.severity == "critical"
        assert data.status == SecurityIncidentStatus.OPEN.value
        assert data.source_ip == "10.0.0.1"
        assert data.user_agent == "curl/7.68.0"

    def test_detected_at_maps_to_created_at(self, repo):
        """Django's detected_at maps to DTO's created_at."""
        ts = datetime(2026, 1, 15, 8, 30, tzinfo=UTC)
        instance = _make_model_instance(detected_at=ts)
        data = repo._to_data(instance)
        assert data.created_at == ts

    def test_raw_request_maps_to_raw_payload(self, repo):
        """Django's raw_request maps to DTO's raw_payload."""
        payload = {"method": "POST", "body": "test"}
        instance = _make_model_instance(raw_request=payload)
        data = repo._to_data(instance)
        assert data.raw_payload == payload

    def test_entity_refs_from_related_fields(self, repo):
        """related_entity_type/id map to entity_refs dict."""
        instance = _make_model_instance(
            related_entity_type="order",
            related_entity_id="123",
        )
        data = repo._to_data(instance)
        assert data.entity_refs == {"order": 123}

    def test_empty_related_fields_yield_empty_entity_refs(self, repo):
        """Empty related_entity fields result in empty entity_refs."""
        instance = _make_model_instance(
            related_entity_type="",
            related_entity_id="",
        )
        data = repo._to_data(instance)
        assert data.entity_refs == {}

    def test_none_related_fields_yield_empty_entity_refs(self, repo):
        """None related_entity fields result in empty entity_refs."""
        instance = _make_model_instance(
            related_entity_type=None,
            related_entity_id=None,
        )
        data = repo._to_data(instance)
        assert data.entity_refs == {}

    def test_invalid_entity_id_is_ignored(self, repo):
        """Non-numeric related_entity_id is silently skipped."""
        instance = _make_model_instance(
            related_entity_type="order",
            related_entity_id="not_a_number",
        )
        data = repo._to_data(instance)
        assert data.entity_refs == {}

    def test_none_user_agent_becomes_empty_string(self, repo):
        """None user_agent becomes empty string in DTO."""
        instance = _make_model_instance(user_agent=None)
        data = repo._to_data(instance)
        assert data.user_agent == ""

    def test_none_description_becomes_empty_string(self, repo):
        """None description becomes empty string in DTO."""
        instance = _make_model_instance(description=None)
        data = repo._to_data(instance)
        assert data.description == ""

    def test_none_investigation_notes_becomes_empty_string(self, repo):
        """None investigation_notes becomes empty string in DTO."""
        instance = _make_model_instance(investigation_notes=None)
        data = repo._to_data(instance)
        assert data.investigation_notes == ""


class TestDjangoSecurityIncidentCreateBehavior:
    """Verify create() builds correct model.objects.create call."""

    def test_create_calls_model_objects_create(self, repo, mock_model):
        """create() delegates to model.objects.create."""
        mock_model.objects.create.return_value = _make_model_instance()

        result = repo.create(
            incident_type="unauthorized_access",
            severity="high",
            description="Test",
            source_ip="10.0.0.1",
            user_agent="test-agent",
            user_id=5,
            raw_payload={"key": "val"},
        )

        mock_model.objects.create.assert_called_once()
        call_kwargs = mock_model.objects.create.call_args[1]
        assert call_kwargs["incident_type"] == "unauthorized_access"
        assert call_kwargs["severity"] == "high"
        assert call_kwargs["raw_request"] == {"key": "val"}
        assert isinstance(result, SecurityIncidentData)

    def test_create_maps_entity_refs_to_related_fields(self, repo, mock_model):
        """create() maps entity_refs dict to related_entity_type/id."""
        mock_model.objects.create.return_value = _make_model_instance()

        repo.create(
            incident_type="test",
            severity="medium",
            entity_refs={"payment": 456},
        )

        call_kwargs = mock_model.objects.create.call_args[1]
        assert call_kwargs["related_entity_type"] == "payment"
        assert call_kwargs["related_entity_id"] == "456"

    def test_create_with_no_entity_refs(self, repo, mock_model):
        """create() with no entity_refs uses empty related fields."""
        mock_model.objects.create.return_value = _make_model_instance()

        repo.create(incident_type="test", severity="medium")

        call_kwargs = mock_model.objects.create.call_args[1]
        assert call_kwargs["related_entity_type"] == ""
        assert call_kwargs["related_entity_id"] == ""


class TestDjangoSecurityIncidentGetByIdBehavior:
    """Verify get_by_id() correctly retrieves or returns None."""

    def test_existing_id_returns_data(self, repo, mock_model):
        """get_by_id() returns SecurityIncidentData for existing ID."""
        mock_model.objects.get.return_value = _make_model_instance(id=42)
        result = repo.get_by_id(42)
        assert result is not None
        assert result.id == 42

    def test_nonexistent_id_returns_none(self, repo, mock_model):
        """get_by_id() returns None for nonexistent ID."""
        mock_model.objects.get.side_effect = mock_model.DoesNotExist()
        result = repo.get_by_id(99999)
        assert result is None


class TestDjangoSecurityIncidentQueryBehavior:
    """Verify query methods use correct filters."""

    def test_get_open_incidents_filters_by_open_status(self, repo, mock_model):
        """get_open_incidents() filters by OPEN status."""
        mock_qs = MagicMock()
        mock_qs.order_by.return_value.__getitem__ = MagicMock(return_value=[])
        mock_model.objects.filter.return_value = mock_qs

        repo.get_open_incidents(limit=50)

        mock_model.objects.filter.assert_called_once_with(
            status=SecurityIncidentStatus.OPEN.value,
        )

    def test_get_by_type_filters_by_incident_type(self, repo, mock_model):
        """get_by_type() filters by incident_type."""
        mock_qs = MagicMock()
        mock_qs.order_by.return_value.__getitem__ = MagicMock(return_value=[])
        mock_model.objects.filter.return_value = mock_qs

        repo.get_by_type("replay_attack", limit=25)

        mock_model.objects.filter.assert_called_once_with(
            incident_type="replay_attack",
        )

    def test_get_by_severity_filters_by_severity(self, repo, mock_model):
        """get_by_severity() filters by severity."""
        mock_qs = MagicMock()
        mock_qs.order_by.return_value.__getitem__ = MagicMock(return_value=[])
        mock_model.objects.filter.return_value = mock_qs

        repo.get_by_severity("critical", limit=10)

        mock_model.objects.filter.assert_called_once_with(
            severity="critical",
        )


class TestDjangoSecurityIncidentUpdateStatusBehavior:
    """Verify update_status() and mark_as_resolved()."""

    def test_update_status_saves_new_status(self, repo, mock_model):
        """update_status() saves the new status on the model."""
        instance = _make_model_instance()
        mock_model.objects.get.return_value = instance
        instance.save = MagicMock()

        result = repo.update_status(
            1,
            SecurityIncidentStatus.INVESTIGATING.value,
            investigation_notes="Looking into it",
        )

        assert result is True
        assert instance.status == SecurityIncidentStatus.INVESTIGATING.value
        assert instance.investigation_notes == "Looking into it"
        instance.save.assert_called_once()

    def test_update_status_nonexistent_returns_false(self, repo, mock_model):
        """update_status() returns False for nonexistent ID."""
        mock_model.objects.get.side_effect = mock_model.DoesNotExist()
        result = repo.update_status(99999, "resolved")
        assert result is False

    @patch("django.utils.timezone.now")
    def test_resolved_status_sets_resolved_at(self, mock_now, repo, mock_model):
        """Resolving an incident sets resolved_at timestamp."""
        now = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)
        mock_now.return_value = now

        instance = _make_model_instance()
        instance.save = MagicMock()
        mock_model.objects.get.return_value = instance

        repo.update_status(1, SecurityIncidentStatus.RESOLVED.value)

        assert instance.resolved_at == now

    @patch("django.utils.timezone.now")
    def test_mark_as_resolved_delegates_to_update_status(
        self, mock_now, repo, mock_model
    ):
        """mark_as_resolved() calls update_status with RESOLVED."""
        mock_now.return_value = datetime(2026, 3, 7, 15, 0, 0, tzinfo=UTC)

        instance = _make_model_instance()
        instance.save = MagicMock()
        mock_model.objects.get.return_value = instance

        result = repo.mark_as_resolved(1, investigation_notes="Fixed")
        assert result is True
        assert instance.status == SecurityIncidentStatus.RESOLVED.value


class TestDjangoSecurityIncidentContract:
    """Contract: DjangoSecurityIncidentRepository implements SecurityIncidentRepository."""

    def test_implements_security_incident_repository(self, repo):
        """DjangoSecurityIncidentRepository is a SecurityIncidentRepository."""
        from baldur.interfaces.repositories import SecurityIncidentRepository

        assert isinstance(repo, SecurityIncidentRepository)
