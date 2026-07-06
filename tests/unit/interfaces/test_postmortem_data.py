"""PostmortemData DTO and _parse_datetime unit tests.

Tests for the framework-agnostic Postmortem domain model:
- Contract: field defaults, 12-field structure, SerializableMixin inheritance
- Behavior: serialization roundtrip, from_incident_dict source compatibility,
  ISO date parsing, duration calculation, _parse_datetime edge cases
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from baldur.core.serializable import SerializableMixin
from baldur.interfaces.repositories import (
    PostmortemData,
    PostmortemRepository,
    _parse_datetime,
)

# =============================================================================
# Contract Tests
# =============================================================================


class TestPostmortemDataContract:
    """PostmortemData field defaults and structural contract verification."""

    def test_default_source_is_auto(self):
        """Default source field value is 'auto'."""
        data = PostmortemData(incident_id="test-001")
        assert data.source == "auto"

    def test_default_duration_seconds_is_zero(self):
        """Default duration_seconds is 0.0."""
        data = PostmortemData(incident_id="test-001")
        assert data.duration_seconds == 0.0

    def test_to_dict_contains_all_twelve_keys(self):
        """to_dict() output contains exactly the 12 contract keys."""
        expected_keys = {
            "id",
            "incident_id",
            "started_at",
            "resolved_at",
            "duration_seconds",
            "affected_services",
            "timeline",
            "auto_actions",
            "recommendations",
            "system_snapshot",
            "created_at",
            "source",
        }
        data = PostmortemData(incident_id="test-001")
        assert set(data.to_dict().keys()) == expected_keys

    def test_inherits_serializable_mixin(self):
        """PostmortemData inherits SerializableMixin."""
        assert issubclass(PostmortemData, SerializableMixin)
        data = PostmortemData(incident_id="test-001")
        assert isinstance(data, SerializableMixin)

    def test_postmortem_repository_is_abstract(self):
        """PostmortemRepository cannot be instantiated directly."""
        with pytest.raises(TypeError):
            PostmortemRepository()


# =============================================================================
# Behavior Tests — PostmortemData
# =============================================================================


class TestPostmortemDataBehavior:
    """PostmortemData creation and field behavior verification."""

    def test_auto_generates_uuid_when_id_is_empty(self):
        """Empty id generates a valid UUID string."""
        data = PostmortemData(incident_id="test-001")
        assert len(data.id) == 36  # UUID format: 8-4-4-4-12
        assert data.id.count("-") == 4

    def test_preserves_explicit_id(self):
        """Explicitly provided id is not overwritten."""
        data = PostmortemData(incident_id="test-001", id="my-custom-id")
        assert data.id == "my-custom-id"

    def test_auto_generates_created_at_when_none(self):
        """created_at is auto-populated with UTC datetime when None."""
        data = PostmortemData(incident_id="test-001")
        assert data.created_at is not None
        assert data.created_at.tzinfo is not None

    def test_preserves_explicit_created_at(self):
        """Explicitly provided created_at is not overwritten."""
        explicit = datetime(2026, 1, 1, tzinfo=UTC)
        data = PostmortemData(incident_id="test-001", created_at=explicit)
        assert data.created_at == explicit

    def test_default_list_fields_are_empty(self):
        """List-type fields default to empty lists."""
        data = PostmortemData(incident_id="test-001")
        assert data.affected_services == []
        assert data.timeline == []
        assert data.auto_actions == []
        assert data.recommendations == []

    def test_default_dict_field_is_empty(self):
        """Dict-type system_snapshot defaults to empty dict."""
        data = PostmortemData(incident_id="test-001")
        assert data.system_snapshot == {}


# =============================================================================
# Behavior Tests — Serialization Roundtrip (§8.9)
# =============================================================================


class TestPostmortemDataSerializationBehavior:
    """PostmortemData to_dict/from_dict roundtrip verification."""

    def test_roundtrip_preserves_all_fields(self):
        """to_dict -> from_dict roundtrip preserves all field values."""
        # Given
        original = PostmortemData(
            incident_id="rt-001",
            started_at=datetime(2026, 1, 28, 10, 0, 0, tzinfo=UTC),
            resolved_at=datetime(2026, 1, 28, 10, 30, 0, tzinfo=UTC),
            duration_seconds=1800.0,
            affected_services=["payment", "order"],
            timeline=[{"event": "detected"}],
            auto_actions=[{"action": "cb_open"}],
            recommendations=["scale up"],
            system_snapshot={"cpu": 85.0},
            source="manual",
        )

        # When
        serialized = original.to_dict()
        restored = PostmortemData.from_dict(serialized)

        # Then
        assert restored.incident_id == original.incident_id
        assert restored.duration_seconds == original.duration_seconds
        assert restored.affected_services == original.affected_services
        assert restored.timeline == original.timeline
        assert restored.auto_actions == original.auto_actions
        assert restored.recommendations == original.recommendations
        assert restored.system_snapshot == original.system_snapshot
        assert restored.source == original.source
        assert restored.id == original.id

    def test_datetime_fields_serialize_as_isoformat(self):
        """datetime fields are serialized as ISO format strings."""
        dt = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
        data = PostmortemData(incident_id="iso-001", started_at=dt, created_at=dt)
        serialized = data.to_dict()
        assert isinstance(serialized["started_at"], str)
        assert "2026-03-15" in serialized["started_at"]

    def test_none_datetime_serializes_as_none(self):
        """None datetime fields serialize as None."""
        data = PostmortemData(incident_id="none-dt")
        serialized = data.to_dict()
        assert serialized["resolved_at"] is None


# =============================================================================
# Behavior Tests — from_incident_dict (source compatibility)
# =============================================================================


class TestPostmortemDataFromIncidentDictBehavior:
    """from_incident_dict() source field backward compatibility."""

    def test_source_manual_key_sets_manual(self):
        """source='manual' in input dict sets source to 'manual'."""
        data = PostmortemData.from_incident_dict({"source": "manual"})
        assert data.source == "manual"

    def test_manual_key_sets_manual(self):
        """manual=True in input dict sets source to 'manual'."""
        data = PostmortemData.from_incident_dict({"manual": True})
        assert data.source == "manual"

    def test_is_auto_false_sets_manual(self):
        """is_auto=False in input dict sets source to 'manual'."""
        data = PostmortemData.from_incident_dict({"is_auto": False})
        assert data.source == "manual"

    def test_is_auto_true_sets_auto(self):
        """is_auto=True (or missing) in input dict defaults to 'auto'."""
        data = PostmortemData.from_incident_dict({"is_auto": True})
        assert data.source == "auto"

    def test_no_source_keys_defaults_to_auto(self):
        """Empty dict defaults source to 'auto'."""
        data = PostmortemData.from_incident_dict({})
        assert data.source == "auto"

    def test_duration_calculated_from_timestamps(self):
        """duration_seconds is calculated from started_at/resolved_at when not provided."""
        data = PostmortemData.from_incident_dict(
            {
                "started_at": "2026-01-28T10:00:00+00:00",
                "resolved_at": "2026-01-28T10:30:00+00:00",
            }
        )
        assert data.duration_seconds == 1800.0

    def test_explicit_duration_takes_precedence(self):
        """Explicit duration_seconds overrides calculated value."""
        data = PostmortemData.from_incident_dict(
            {
                "started_at": "2026-01-28T10:00:00+00:00",
                "resolved_at": "2026-01-28T10:30:00+00:00",
                "duration_seconds": 999.0,
            }
        )
        assert data.duration_seconds == 999.0

    def test_generates_incident_id_when_missing(self):
        """Missing incident_id auto-generates a UUID."""
        data = PostmortemData.from_incident_dict({})
        assert len(data.incident_id) == 36

    def test_preserves_incident_id_when_provided(self):
        """Provided incident_id is preserved."""
        data = PostmortemData.from_incident_dict({"incident_id": "my-inc"})
        assert data.incident_id == "my-inc"

    def test_parses_iso_datetime_with_z_suffix(self):
        """Z suffix datetime strings are parsed correctly."""
        data = PostmortemData.from_incident_dict({"started_at": "2026-01-28T10:00:00Z"})
        assert data.started_at is not None
        assert data.started_at.year == 2026

    def test_passes_all_optional_fields_through(self):
        """Optional list/dict fields are correctly passed through."""
        data = PostmortemData.from_incident_dict(
            {
                "affected_services": ["svc-a"],
                "timeline": [{"e": 1}],
                "auto_actions": [{"a": 1}],
                "recommendations": ["rec1"],
                "system_snapshot": {"cpu": 50},
            }
        )
        assert data.affected_services == ["svc-a"]
        assert data.timeline == [{"e": 1}]
        assert data.auto_actions == [{"a": 1}]
        assert data.recommendations == ["rec1"]
        assert data.system_snapshot == {"cpu": 50}


# =============================================================================
# Behavior Tests — _parse_datetime (§8.2 edge cases)
# =============================================================================


class TestParseDatetimeBehavior:
    """_parse_datetime helper edge case and behavior verification."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert _parse_datetime(None) is None

    def test_datetime_object_passes_through(self):
        """datetime object is returned as-is."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        assert _parse_datetime(dt) is dt

    def test_valid_iso_string_returns_datetime(self):
        """Valid ISO string is parsed to datetime."""
        result = _parse_datetime("2026-01-28T10:00:00+00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026

    def test_z_suffix_string_returns_datetime(self):
        """Z suffix string is correctly parsed."""
        result = _parse_datetime("2026-01-28T10:00:00Z")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_invalid_string_returns_none(self):
        """Invalid date string returns None."""
        assert _parse_datetime("not-a-date") is None

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert _parse_datetime("") is None

    def test_integer_returns_none(self):
        """Non-string, non-datetime type returns None."""
        assert _parse_datetime(12345) is None

    def test_list_returns_none(self):
        """List input returns None."""
        assert _parse_datetime([2026, 1, 28]) is None
