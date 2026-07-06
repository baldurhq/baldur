"""CascadeEventData domain model unit tests.

Tests for the framework-agnostic Cascade Event domain model:
- Contract: TriggerType enum values (9), field count (16), field defaults
- Behavior: serialization roundtrip, verify_hash_integrity, get_causation_chain_display,
  from_cascade_event factory
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from unittest.mock import MagicMock

from baldur.core.serializable import SerializableMixin
from baldur.models.cascade_event import (
    CascadeEventData,
    TriggerType,
)

# =============================================================================
# Contract Tests
# =============================================================================


class TestTriggerTypeContract:
    """TriggerType enum contract verification."""

    def test_trigger_type_has_nine_members(self):
        """TriggerType enum has exactly 9 members."""
        assert len(TriggerType) == 9

    def test_trigger_type_values_match_spec(self):
        """TriggerType enum values match 366 design spec."""
        expected = {
            "EMERGENCY_LEVEL_CHANGED",
            "MANUAL_INTERVENTION",
            "MANUAL_ACTIVATION",
            "CANARY_ROLLBACK",
            "CIRCUIT_BREAKER_OPENED",
            "GOVERNANCE_MODE_CHANGED",
            "ERROR_BUDGET_EXHAUSTED",
            "RECOVERY_STARTED",
            "DEESCALATION",
        }
        actual = {t.value for t in TriggerType}
        assert actual == expected

    def test_trigger_type_is_str_enum(self):
        """TriggerType inherits from (str, Enum) for JSON serialization."""
        assert issubclass(TriggerType, str)
        assert issubclass(TriggerType, Enum)


class TestCascadeEventDataContract:
    """CascadeEventData field defaults and structural contract verification."""

    def test_inherits_serializable_mixin(self):
        """CascadeEventData inherits from SerializableMixin."""
        assert issubclass(CascadeEventData, SerializableMixin)

    def test_to_dict_contains_all_sixteen_keys(self):
        """to_dict() output contains exactly 16 contract keys."""
        expected_keys = {
            "cascade_id",
            "namespace",
            "trigger_type",
            "current_hash",
            "timestamp",
            "trigger_details",
            "effects",
            "causation_chain",
            "previous_hash",
            "total_effects",
            "success_count",
            "failure_count",
            "archived_at",
            "external_trace",
            "version",
            "is_test",
        }
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
        )
        assert set(data.to_dict().keys()) == expected_keys

    def test_default_version_is_1_0(self):
        """Default version field is '1.0'."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
        )
        assert data.version == "1.0"

    def test_default_is_test_is_false(self):
        """Default is_test field is False."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
        )
        assert data.is_test is False

    def test_default_previous_hash_is_empty(self):
        """Default previous_hash is empty string."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
        )
        assert data.previous_hash == ""

    def test_default_counts_are_zero(self):
        """Default effect counts are 0."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
        )
        assert data.total_effects == 0
        assert data.success_count == 0
        assert data.failure_count == 0


# =============================================================================
# Behavior Tests — Serialization
# =============================================================================


class TestCascadeEventDataSerializationBehavior:
    """CascadeEventData serialization roundtrip verification."""

    def test_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict roundtrip preserves all fields."""
        from baldur.utils.time import utc_now

        now = utc_now()
        original = CascadeEventData(
            cascade_id="cascade-evt-abc123",
            namespace="seoul",
            trigger_type=TriggerType.CANARY_ROLLBACK.value,
            current_hash="deadbeef" * 8,
            timestamp=now,
            trigger_details={"old_level": 1, "new_level": 3},
            effects=[{"action_type": "disable", "success": True}],
            causation_chain=["event-1", "event-2"],
            previous_hash="0" * 64,
            total_effects=1,
            success_count=1,
            failure_count=0,
            external_trace={"trace_id": "abc"},
            version="2.0",
            is_test=True,
        )

        serialized = original.to_dict()
        restored = CascadeEventData.from_dict(serialized)

        assert restored.cascade_id == original.cascade_id
        assert restored.namespace == original.namespace
        assert restored.trigger_type == original.trigger_type
        assert restored.current_hash == original.current_hash
        assert restored.timestamp == original.timestamp
        assert restored.trigger_details == original.trigger_details
        assert restored.effects == original.effects
        assert restored.causation_chain == original.causation_chain
        assert restored.previous_hash == original.previous_hash
        assert restored.total_effects == original.total_effects
        assert restored.success_count == original.success_count
        assert restored.failure_count == original.failure_count
        assert restored.external_trace == original.external_trace
        assert restored.version == original.version
        assert restored.is_test == original.is_test

    def test_trigger_type_enum_serializes_to_string(self):
        """TriggerType enum value serializes to plain string."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type=TriggerType.DEESCALATION.value,
            current_hash="abc",
        )
        serialized = data.to_dict()
        assert serialized["trigger_type"] == "DEESCALATION"
        assert isinstance(serialized["trigger_type"], str)


# =============================================================================
# Behavior Tests — Domain Methods
# =============================================================================


class TestCascadeEventDataMethodBehavior:
    """CascadeEventData domain method behavior verification."""

    def test_get_causation_chain_display_with_chain(self):
        """get_causation_chain_display() formats chain with arrows."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
            causation_chain=["event-1", "event-2", "event-3"],
        )
        assert data.get_causation_chain_display() == "event-1 → event-2 → event-3"

    def test_get_causation_chain_display_empty_returns_no_chain(self):
        """get_causation_chain_display() returns 'No chain' for empty list."""
        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="abc",
            causation_chain=[],
        )
        assert data.get_causation_chain_display() == "No chain"

    def test_verify_hash_integrity_valid(self):
        """verify_hash_integrity() returns True for valid hash."""
        from baldur.utils.serialization import fast_canonical_dumps
        from baldur.utils.time import utc_now

        now = utc_now()
        content = {
            "id": "c-1",
            "trigger": {"trigger_type": "CANARY_ROLLBACK", "details": {}},
            "effects": [],
            "namespace": "global",
            "timestamp": now.isoformat(),
            "previous_hash": "",
        }
        valid_hash = hashlib.sha256(fast_canonical_dumps(content)).hexdigest()

        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash=valid_hash,
            timestamp=now,
        )
        assert data.verify_hash_integrity() is True

    def test_verify_hash_integrity_invalid(self):
        """verify_hash_integrity() returns False for tampered hash."""
        from baldur.utils.time import utc_now

        data = CascadeEventData(
            cascade_id="c-1",
            namespace="global",
            trigger_type="CANARY_ROLLBACK",
            current_hash="invalid_hash",
            timestamp=utc_now(),
        )
        assert data.verify_hash_integrity() is False

    def test_from_cascade_event_factory(self):
        """from_cascade_event() correctly maps CascadeEvent fields."""
        # Given
        mock_event = MagicMock()
        mock_event.id = "cascade-evt-001"
        mock_event.namespace = "global"
        mock_event.trigger.trigger_type = "EMERGENCY_LEVEL_CHANGED"
        mock_event.trigger.details = {"old_level": 1, "new_level": 2}
        mock_event.effects = []
        mock_event.get_causation_chain.return_value = ["evt-1"]
        mock_event.previous_hash = "prev"
        mock_event.current_hash = "curr"
        mock_event.total_effects = 0
        mock_event.success_count = 0
        mock_event.failure_count = 0
        mock_event.timestamp = "2026-01-23T10:00:00+00:00"
        mock_event.external_trace = None
        mock_event.version = "1.0"
        mock_event.is_test = False

        # When
        result = CascadeEventData.from_cascade_event(mock_event)

        # Then
        assert result.cascade_id == "cascade-evt-001"
        assert result.namespace == "global"
        assert result.trigger_type == "EMERGENCY_LEVEL_CHANGED"
        assert result.causation_chain == ["evt-1"]
        assert isinstance(result.timestamp, datetime)
