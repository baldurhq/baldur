"""Tests for SerializableMixin.to_json() and to_json_bytes() — 363↔364 integration.

Verifies that to_json()/to_json_bytes() correctly chain to_dict() type
conversion with fast_dumps_str/fast_dumps serialization.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from baldur.core.serializable import SerializableMixin
from baldur.utils.serialization import fast_loads

# ---------------------------------------------------------------------------
# Test fixtures (file-local — used only here)
# ---------------------------------------------------------------------------


class _Priority(str, Enum):
    LOW = "low"
    HIGH = "high"


@dataclasses.dataclass
class _SimpleEntry(SerializableMixin):
    name: str = "test"
    count: int = 0
    active: bool = True


@dataclasses.dataclass
class _ComplexEntry(SerializableMixin):
    event_id: str = "evt-001"
    priority: _Priority = _Priority.LOW
    timestamp: datetime = dataclasses.field(
        default_factory=lambda: datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC),
    )
    amount: Decimal = Decimal("99.95")
    tags: set[str] = dataclasses.field(default_factory=lambda: {"b", "a", "c"})
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _NullableEntry(SerializableMixin):
    exclude_none = True
    label: str = "ok"
    detail: str | None = None


# ---------------------------------------------------------------------------
# TestToJson
# ---------------------------------------------------------------------------


class TestToJsonBehavior:
    """SerializableMixin.to_json() behavior verification."""

    def test_returns_str(self):
        """to_json() must return str type."""
        entry = _SimpleEntry()
        result = entry.to_json()
        assert isinstance(result, str)

    def test_output_is_valid_json(self):
        """to_json() output must be parseable by fast_loads."""
        entry = _SimpleEntry(name="hello", count=42)
        parsed = fast_loads(entry.to_json())
        assert parsed["name"] == "hello"
        assert parsed["count"] == 42

    def test_enum_serialized_as_value(self):
        """Enum fields must appear as their .value string in JSON."""
        entry = _ComplexEntry(priority=_Priority.HIGH)
        parsed = fast_loads(entry.to_json())
        assert parsed["priority"] == "high"

    def test_datetime_serialized_as_isoformat(self):
        """datetime fields must appear as ISO 8601 string in JSON."""
        dt = datetime(2026, 3, 20, 15, 0, 0, tzinfo=UTC)
        entry = _ComplexEntry(timestamp=dt)
        parsed = fast_loads(entry.to_json())
        assert parsed["timestamp"] == "2026-03-20T15:00:00+00:00"

    def test_decimal_serialized_as_str(self):
        """Decimal fields must appear as string in JSON."""
        entry = _ComplexEntry(amount=Decimal("123.45"))
        parsed = fast_loads(entry.to_json())
        assert parsed["amount"] == "123.45"

    def test_set_serialized_as_sorted_list(self):
        """set fields must appear as sorted list in JSON."""
        entry = _ComplexEntry(tags={"z", "a", "m"})
        parsed = fast_loads(entry.to_json())
        assert parsed["tags"] == ["a", "m", "z"]

    def test_exclude_none_omits_null_fields(self):
        """exclude_none=True must omit None-valued fields from JSON output."""
        entry = _NullableEntry(label="ok", detail=None)
        parsed = fast_loads(entry.to_json())
        assert "label" in parsed
        assert "detail" not in parsed

    def test_no_default_str_needed(self):
        """to_json() does not need default=str — to_dict() handles type conversion."""
        entry = _ComplexEntry()
        # This must NOT raise TypeError for datetime/Decimal/Enum
        result = entry.to_json()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# TestToJsonBytes
# ---------------------------------------------------------------------------


class TestToJsonBytesBehavior:
    """SerializableMixin.to_json_bytes() behavior verification."""

    def test_returns_bytes(self):
        """to_json_bytes() must return bytes type."""
        entry = _SimpleEntry()
        result = entry.to_json_bytes()
        assert isinstance(result, bytes)

    def test_output_is_valid_json_bytes(self):
        """to_json_bytes() output must be parseable by fast_loads."""
        entry = _SimpleEntry(name="test", count=7)
        parsed = fast_loads(entry.to_json_bytes())
        assert parsed["name"] == "test"
        assert parsed["count"] == 7

    def test_consistent_with_to_json(self):
        """to_json_bytes() decoded must equal to_json() output."""
        entry = _ComplexEntry()
        json_str = entry.to_json()
        json_bytes = entry.to_json_bytes()
        assert json_bytes.decode("utf-8") == json_str

    def test_complex_types_serialized_correctly(self):
        """Complex type fields must be handled identically to to_json()."""
        entry = _ComplexEntry(
            priority=_Priority.HIGH,
            amount=Decimal("0.01"),
            tags={"x", "y"},
            metadata={"nested": {"key": "val"}},
        )
        parsed = fast_loads(entry.to_json_bytes())
        assert parsed["priority"] == "high"
        assert parsed["amount"] == "0.01"
        assert parsed["tags"] == ["x", "y"]
        assert parsed["metadata"] == {"nested": {"key": "val"}}


# ---------------------------------------------------------------------------
# TestSerializationRoundTrip
# ---------------------------------------------------------------------------


class TestSerializationRoundTripBehavior:
    """to_json → fast_loads → from_dict round-trip data preservation."""

    def test_simple_round_trip(self):
        """Simple dataclass survives to_json → from_dict round-trip."""
        original = _SimpleEntry(name="round-trip", count=99, active=False)
        json_str = original.to_json()
        restored = _SimpleEntry.from_dict(fast_loads(json_str))
        assert restored.name == original.name
        assert restored.count == original.count
        assert restored.active == original.active

    def test_complex_round_trip(self):
        """Complex dataclass with Enum/datetime/Decimal survives round-trip."""
        original = _ComplexEntry(
            event_id="evt-rt",
            priority=_Priority.HIGH,
            timestamp=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            amount=Decimal("42.00"),
            tags={"a", "b"},
            metadata={"key": "value"},
        )

        # When
        json_str = original.to_json()
        restored = _ComplexEntry.from_dict(fast_loads(json_str))

        # Then
        assert restored.event_id == original.event_id
        assert restored.priority == original.priority
        assert restored.timestamp == original.timestamp
        assert restored.amount == original.amount
        assert restored.tags == original.tags
        assert restored.metadata == original.metadata
