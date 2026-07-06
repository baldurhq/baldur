"""SerializableMixin Unit Tests

Comprehensive tests for core/serializable.py — auto to_dict()/from_dict() mixin
for dataclasses with standard type conversions.

Test Categories:
    A. Contract Tests:
        - ClassVar defaults (exclude_none, _type_hints_cache)
        - __all__ export
    B. Serialization Behavior Tests:
        - Primitive fields round-trip
        - Enum, datetime, Decimal type conversion
        - set/frozenset/tuple collection handling
        - Nested object serialization
        - dict recursive descent
        - exclude_none filtering
        - _post_serialize hook
    C. Deserialization Behavior Tests:
        - Enum, datetime restoration from type hints
        - Nested SerializableMixin restoration
        - list[SerializableMixin] restoration
        - tuple/set/frozenset restoration
        - Optional unwrapping
        - Extra keys ignored (forward compat)
        - Missing optional keys use defaults
        - Missing required keys raise TypeError
        - _deserialize_field hook override
        - isinstance guard (no double-conversion)
    D. Advanced Behavior Tests:
        - Type hints caching per-class isolation
        - TYPE_CHECKING NameError fallback
        - super().to_dict() inheritance chain
        - Explicit to_dict() override coexistence
        - Non-dataclass usage error
    E. Roundtrip Behavior Tests:
        - Full serialization → deserialization data preservation
        - Idempotency (multiple calls same result)
        - Data immutability (from_dict does not mutate input)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

import pytest

from baldur.core.serializable import SerializableMixin

# =============================================================================
# Test Fixtures — Representative dataclass samples (from design doc §5.3)
# =============================================================================


class SampleStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


class SamplePriority(str, Enum):
    LOW = "low"
    HIGH = "high"


class SampleIntLevel(int, Enum):
    """IntEnum — EmergencyLevel pattern."""

    NORMAL = 0
    LEVEL_1 = 1
    LEVEL_2 = 2
    LEVEL_3 = 3


@dataclass
class SampleFlat(SerializableMixin):
    """Flat model — Category A equivalent (services/governance/checks.py pattern)."""

    name: str
    status: SampleStatus
    created_at: datetime
    score: Decimal
    tags: list[str] = field(default_factory=list)


@dataclass
class SampleOptional(SerializableMixin):
    """Model with Optional fields."""

    name: str
    status: SampleStatus | None = None
    activated_at: datetime | None = None
    description: str | None = None
    count: int = 0


@dataclass
class SampleConditional(SerializableMixin):
    """Conditional model — Category B equivalent (canary/feature_flag.py pattern)."""

    exclude_none: ClassVar[bool] = True

    name: str
    description: str | None = None
    whitelist: set[str] = field(default_factory=set)


@dataclass
class SampleCollections(SerializableMixin):
    """Model with set, frozenset, tuple fields."""

    tags: set[str] = field(default_factory=set)
    methods: frozenset[str] = field(default_factory=frozenset)
    priority_order: tuple[str, ...] = ()


@dataclass
class SampleNested(SerializableMixin):
    """Nested model — cascade_event.py pattern."""

    trigger: SampleFlat
    effects: list[SampleFlat] = field(default_factory=list)
    metadata: SampleFlat | None = None


@dataclass
class SampleChild(SampleFlat):
    """Inherited model — ManualInterventionEffect pattern."""

    extra_field: str = ""
    child_priority: SamplePriority = SamplePriority.LOW


@dataclass
class SampleWithPostSerialize(SerializableMixin):
    """Model with custom _post_serialize hook."""

    name: str
    reasons: list[str] = field(default_factory=list)
    internal_id: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        if not data.get("reasons"):
            data.pop("reasons", None)
        return super()._post_serialize(data)


@dataclass
class SampleWithPostSerializeAndExcludeNone(SerializableMixin):
    """Model with _post_serialize + exclude_none interaction."""

    exclude_none: ClassVar[bool] = True

    name: str
    reasons: list[str] = field(default_factory=list)
    optional_tag: str | None = None

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        if not data.get("reasons"):
            data.pop("reasons", None)
        return super()._post_serialize(data)


@dataclass
class SampleWithCustomDeserialize(SerializableMixin):
    """Model with custom _deserialize_field hook."""

    name: str
    priority: SamplePriority = SamplePriority.LOW

    @classmethod
    def _deserialize_field(cls, field_name: str, value: Any) -> Any:
        if field_name == "priority" and isinstance(value, str):
            mapping = {"low": SamplePriority.LOW, "high": SamplePriority.HIGH}
            return mapping.get(value, SamplePriority.LOW)
        return super()._deserialize_field(field_name, value)


@dataclass
class SampleWithPostInit(SerializableMixin):
    """Model with __post_init__ to verify it triggers on from_dict."""

    name: str
    validated: bool = field(init=False, default=False)

    def __post_init__(self):
        self.validated = True


@dataclass
class SampleWithDict(SerializableMixin):
    """Model with dict field containing mixed values."""

    name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SampleWithToDictObject(SerializableMixin):
    """Model containing a non-Mixin object with to_dict()."""

    name: str
    inner: Any = None


class NonMixinWithToDict:
    """Non-Mixin object with to_dict() — gradual migration support."""

    def __init__(self, value: str):
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value}


@dataclass
class SampleRequired(SerializableMixin):
    """Model with all required fields (no defaults)."""

    name: str
    value: int


@dataclass
class SampleWithIntEnum(SerializableMixin):
    """Model with IntEnum field — EmergencyLevel pattern (363B)."""

    name: str
    level: SampleIntLevel = SampleIntLevel.NORMAL
    optional_level: SampleIntLevel | None = None


@dataclass
class SampleWithNonComparableSet(SerializableMixin):
    """Model with set containing non-comparable types (363B sorted fallback)."""

    name: str
    mixed_items: set = field(default_factory=set)


# Helper for explicit to_dict override test
@dataclass
class SampleExplicitOverride(SerializableMixin):
    """Model that overrides to_dict() explicitly — override takes precedence."""

    name: str
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"custom_name": self.name, "custom_count": self.count}


# =============================================================================
# A. Contract Tests
# =============================================================================


class TestSerializableMixinContract:
    """SerializableMixin ClassVar defaults and public API contract."""

    def test_exclude_none_default_is_false(self):
        """exclude_none ClassVar defaults to False."""
        assert SerializableMixin.exclude_none is False

    def test_type_hints_cache_default_is_none(self):
        """_type_hints_cache ClassVar defaults to None."""
        assert SerializableMixin._type_hints_cache is None

    def test_module_exports_serializable_mixin(self):
        """__all__ exports SerializableMixin."""
        from baldur.core import serializable

        assert "SerializableMixin" in serializable.__all__

    def test_core_init_exports_serializable_mixin(self):
        """core/__init__.py exports SerializableMixin."""
        from baldur.core import SerializableMixin as exported

        assert exported is SerializableMixin


# =============================================================================
# B. Serialization Behavior Tests
# =============================================================================


class TestSerializationPrimitiveBehavior:
    """Primitive type serialization behavior."""

    def test_flat_dataclass_all_primitive_fields_serialized(self):
        """All primitive fields (str, int, float, bool) pass through to dict."""
        # Given
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        obj = SampleFlat(
            name="test",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("99.5"),
            tags=["a", "b"],
        )

        # When
        result = obj.to_dict()

        # Then
        assert result["name"] == "test"
        assert result["tags"] == ["a", "b"]
        assert isinstance(result["name"], str)

    def test_none_value_serialized_as_none(self):
        """None field values pass through as None."""
        obj = SampleOptional(name="test")
        result = obj.to_dict()
        assert result["status"] is None
        assert result["activated_at"] is None
        assert result["description"] is None


class TestSerializationEnumBehavior:
    """Enum type serialization behavior."""

    def test_enum_field_serialized_as_value(self):
        """Enum field serialized to .value (string)."""
        dt = datetime(2026, 1, 15, tzinfo=UTC)
        obj = SampleFlat(
            name="x", status=SampleStatus.ACTIVE, created_at=dt, score=Decimal("1")
        )
        result = obj.to_dict()
        assert result["status"] == "active"
        assert isinstance(result["status"], str)

    def test_optional_enum_present_serialized_as_value(self):
        """Optional Enum field (present) serialized to .value."""
        obj = SampleOptional(name="x", status=SampleStatus.PENDING)
        result = obj.to_dict()
        assert result["status"] == "pending"

    def test_optional_enum_none_serialized_as_none(self):
        """Optional Enum field (None) serialized as None."""
        obj = SampleOptional(name="x", status=None)
        result = obj.to_dict()
        assert result["status"] is None


class TestSerializationDatetimeBehavior:
    """datetime type serialization behavior."""

    def test_datetime_field_serialized_as_isoformat(self):
        """datetime field serialized to .isoformat() string."""
        dt = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
        obj = SampleFlat(
            name="x", status=SampleStatus.ACTIVE, created_at=dt, score=Decimal("1")
        )
        result = obj.to_dict()
        assert result["created_at"] == dt.isoformat()
        assert isinstance(result["created_at"], str)

    def test_optional_datetime_present_serialized_as_isoformat(self):
        """Optional datetime field (present) serialized to isoformat."""
        dt = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
        obj = SampleOptional(name="x", activated_at=dt)
        result = obj.to_dict()
        assert result["activated_at"] == dt.isoformat()

    def test_optional_datetime_none_serialized_as_none(self):
        """Optional datetime field (None) serialized as None."""
        obj = SampleOptional(name="x", activated_at=None)
        result = obj.to_dict()
        assert result["activated_at"] is None


class TestSerializationDecimalBehavior:
    """Decimal type serialization behavior."""

    def test_decimal_field_serialized_as_string(self):
        """Decimal field serialized to str() representation."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        obj = SampleFlat(
            name="x",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("123.456"),
        )
        result = obj.to_dict()
        assert result["score"] == "123.456"
        assert isinstance(result["score"], str)


class TestSerializationCollectionBehavior:
    """Collection type (set, frozenset, tuple) serialization behavior."""

    def test_set_field_serialized_as_sorted_list(self):
        """set field serialized to sorted list for deterministic output."""
        obj = SampleCollections(
            tags={"c", "a", "b"}, methods=frozenset(), priority_order=()
        )
        result = obj.to_dict()
        assert result["tags"] == ["a", "b", "c"]
        assert isinstance(result["tags"], list)

    def test_frozenset_field_serialized_as_sorted_list(self):
        """frozenset field serialized to sorted list."""
        obj = SampleCollections(
            tags=set(),
            methods=frozenset({"POST", "GET", "DELETE"}),
            priority_order=(),
        )
        result = obj.to_dict()
        assert result["methods"] == ["DELETE", "GET", "POST"]
        assert isinstance(result["methods"], list)

    def test_tuple_field_serialized_as_list(self):
        """tuple field serialized to list (JSON has no tuple)."""
        obj = SampleCollections(
            tags=set(), methods=frozenset(), priority_order=("high", "medium", "low")
        )
        result = obj.to_dict()
        assert result["priority_order"] == ["high", "medium", "low"]
        assert isinstance(result["priority_order"], list)

    def test_list_with_enum_values_serialized_recursively(self):
        """list containing Enum values are recursively serialized."""

        # Enum values inside a list should also be converted
        @dataclass
        class WithEnumList(SerializableMixin):
            statuses: list[SampleStatus] = field(default_factory=list)

        obj = WithEnumList(statuses=[SampleStatus.ACTIVE, SampleStatus.INACTIVE])
        result = obj.to_dict()
        assert result["statuses"] == ["active", "inactive"]


class TestSerializationNestedBehavior:
    """Nested object serialization behavior."""

    def test_nested_mixin_serialized_recursively(self):
        """Nested SerializableMixin object serialized via recursive to_dict()."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        inner = SampleFlat(
            name="inner",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("1"),
        )
        obj = SampleNested(trigger=inner)

        result = obj.to_dict()
        assert result["trigger"] == inner.to_dict()
        assert result["trigger"]["name"] == "inner"
        assert result["trigger"]["status"] == "active"

    def test_nested_list_of_mixin_serialized_recursively(self):
        """list[SerializableMixin] items serialized recursively."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        e1 = SampleFlat(
            name="e1",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("1"),
        )
        e2 = SampleFlat(
            name="e2",
            status=SampleStatus.INACTIVE,
            created_at=dt,
            score=Decimal("2"),
        )
        obj = SampleNested(trigger=e1, effects=[e1, e2])

        result = obj.to_dict()
        assert len(result["effects"]) == 2
        assert result["effects"][0]["name"] == "e1"
        assert result["effects"][1]["status"] == "inactive"

    def test_optional_nested_none_serialized_as_none(self):
        """Optional nested object (None) serialized as None."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        trigger = SampleFlat(
            name="t", status=SampleStatus.ACTIVE, created_at=dt, score=Decimal("1")
        )
        obj = SampleNested(trigger=trigger, metadata=None)

        result = obj.to_dict()
        assert result["metadata"] is None

    def test_non_mixin_object_with_to_dict_serialized(self):
        """Non-Mixin object with to_dict() is serialized via duck typing."""
        inner = NonMixinWithToDict("hello")
        obj = SampleWithToDictObject(name="outer", inner=inner)

        result = obj.to_dict()
        assert result["inner"] == {"value": "hello"}

    def test_dict_field_values_serialized_recursively(self):
        """dict field values are recursively serialized."""
        dt = datetime(2026, 6, 1, tzinfo=UTC)
        obj = SampleWithDict(
            name="test",
            metadata={
                "timestamp": dt,
                "status": SampleStatus.ACTIVE,
                "nested": {"a": 1},
            },
        )
        result = obj.to_dict()
        assert result["metadata"]["timestamp"] == dt.isoformat()
        assert result["metadata"]["status"] == "active"
        assert result["metadata"]["nested"] == {"a": 1}


class TestSerializationExcludeNoneBehavior:
    """exclude_none ClassVar behavior."""

    def test_exclude_none_omits_none_fields(self):
        """exclude_none=True omits fields with None value from output."""
        obj = SampleConditional(name="test", description=None)
        result = obj.to_dict()
        assert "description" not in result
        assert "name" in result

    def test_exclude_none_preserves_non_none_fields(self):
        """exclude_none=True preserves fields with non-None values."""
        obj = SampleConditional(name="test", description="hello")
        result = obj.to_dict()
        assert result["description"] == "hello"

    def test_exclude_none_does_not_exclude_false_value(self):
        """exclude_none=True does NOT exclude False (False != None)."""

        @dataclass
        class WithBool(SerializableMixin):
            exclude_none: ClassVar[bool] = True
            name: str
            enabled: bool = False
            label: str | None = None

        obj = WithBool(name="test", enabled=False, label=None)
        result = obj.to_dict()
        assert "enabled" in result
        assert result["enabled"] is False
        assert "label" not in result

    def test_exclude_none_does_not_exclude_zero(self):
        """exclude_none=True does NOT exclude 0 (0 != None)."""

        @dataclass
        class WithZero(SerializableMixin):
            exclude_none: ClassVar[bool] = True
            name: str
            count: int = 0
            optional_val: str | None = None

        obj = WithZero(name="test", count=0, optional_val=None)
        result = obj.to_dict()
        assert "count" in result
        assert result["count"] == 0
        assert "optional_val" not in result

    def test_exclude_none_does_not_exclude_empty_string(self):
        """exclude_none=True does NOT exclude empty string ('' != None)."""

        @dataclass
        class WithEmpty(SerializableMixin):
            exclude_none: ClassVar[bool] = True
            name: str
            label: str = ""

        obj = WithEmpty(name="test", label="")
        result = obj.to_dict()
        assert "label" in result
        assert result["label"] == ""


class TestPostSerializeHookBehavior:
    """_post_serialize hook behavior."""

    def test_post_serialize_hook_removes_empty_field(self):
        """_post_serialize can remove empty list fields."""
        obj = SampleWithPostSerialize(name="test", reasons=[], internal_id="abc")
        result = obj.to_dict()
        assert "reasons" not in result
        assert result["internal_id"] == "abc"

    def test_post_serialize_hook_preserves_non_empty_field(self):
        """_post_serialize preserves non-empty list fields."""
        obj = SampleWithPostSerialize(
            name="test", reasons=["reason1"], internal_id=None
        )
        result = obj.to_dict()
        assert result["reasons"] == ["reason1"]

    def test_post_serialize_hook_with_exclude_none_interaction(self):
        """_post_serialize + exclude_none: hook runs first, then super filters None."""
        obj = SampleWithPostSerializeAndExcludeNone(
            name="test", reasons=[], optional_tag=None
        )
        result = obj.to_dict()
        # reasons removed by hook (empty list)
        assert "reasons" not in result
        # optional_tag removed by exclude_none (None)
        assert "optional_tag" not in result
        assert result["name"] == "test"


# =============================================================================
# C. Deserialization Behavior Tests
# =============================================================================


class TestDeserializationEnumBehavior:
    """Enum type restoration from type hints."""

    def test_enum_field_restored_from_string_value(self):
        """Enum field auto-restored from string value via type hint."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-15T10:30:00+00:00",
            "score": "99.5",
            "tags": ["a"],
        }
        obj = SampleFlat.from_dict(data)
        assert obj.status == SampleStatus.ACTIVE
        assert isinstance(obj.status, SampleStatus)

    def test_optional_enum_restored_from_string_value(self):
        """Optional Enum field (present) auto-restored from string."""
        data = {"name": "test", "status": "pending"}
        obj = SampleOptional.from_dict(data)
        assert obj.status == SampleStatus.PENDING
        assert isinstance(obj.status, SampleStatus)

    def test_optional_enum_none_passes_through(self):
        """Optional Enum field with None value passes through as None."""
        data = {"name": "test", "status": None}
        obj = SampleOptional.from_dict(data)
        assert obj.status is None

    def test_already_enum_instance_not_double_converted(self):
        """Already-converted Enum object passed through unchanged (isinstance guard)."""
        data = {
            "name": "test",
            "status": SampleStatus.ACTIVE,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "score": Decimal("1"),
        }
        obj = SampleFlat.from_dict(data)
        assert obj.status is SampleStatus.ACTIVE


class TestDeserializationDatetimeBehavior:
    """datetime type restoration from type hints."""

    def test_datetime_field_restored_from_isoformat_string(self):
        """datetime field auto-restored from ISO format string."""
        dt_str = "2026-03-15T14:30:00+00:00"
        data = {
            "name": "test",
            "status": "active",
            "created_at": dt_str,
            "score": "1",
        }
        obj = SampleFlat.from_dict(data)
        assert isinstance(obj.created_at, datetime)
        assert obj.created_at == datetime.fromisoformat(dt_str)

    def test_optional_datetime_restored_from_isoformat_string(self):
        """Optional datetime field (present) auto-restored from ISO string."""
        dt_str = "2026-03-15T14:30:00+00:00"
        data = {"name": "test", "activated_at": dt_str}
        obj = SampleOptional.from_dict(data)
        assert isinstance(obj.activated_at, datetime)
        assert obj.activated_at == datetime.fromisoformat(dt_str)

    def test_optional_datetime_none_passes_through(self):
        """Optional datetime field with None passes through."""
        data = {"name": "test", "activated_at": None}
        obj = SampleOptional.from_dict(data)
        assert obj.activated_at is None

    def test_already_datetime_instance_not_double_converted(self):
        """Already-converted datetime object passed through unchanged."""
        dt = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
        data = {
            "name": "test",
            "status": "active",
            "created_at": dt,
            "score": "1",
        }
        obj = SampleFlat.from_dict(data)
        assert obj.created_at is dt


class TestDeserializationDecimalBehavior:
    """Decimal type restoration from type hints."""

    def test_decimal_field_restored_from_string(self):
        """Decimal field auto-restored from string value via type hint."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "123.456",
            "tags": [],
        }
        obj = SampleFlat.from_dict(data)
        assert isinstance(obj.score, Decimal)
        assert obj.score == Decimal("123.456")

    def test_already_decimal_instance_not_double_converted(self):
        """Already-converted Decimal object passed through unchanged."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": Decimal("99.9"),
            "tags": [],
        }
        obj = SampleFlat.from_dict(data)
        assert obj.score == Decimal("99.9")
        assert isinstance(obj.score, Decimal)


class TestDeserializationNestedBehavior:
    """Nested object deserialization behavior."""

    def test_nested_mixin_restored_from_dict(self):
        """Nested SerializableMixin auto-restored via from_dict."""
        data = {
            "trigger": {
                "name": "inner",
                "status": "active",
                "created_at": "2026-01-01T00:00:00+00:00",
                "score": "1",
                "tags": [],
            },
            "effects": [],
            "metadata": None,
        }
        obj = SampleNested.from_dict(data)
        assert isinstance(obj.trigger, SampleFlat)
        assert obj.trigger.name == "inner"
        assert obj.trigger.status == SampleStatus.ACTIVE

    def test_nested_list_of_mixin_restored_from_dicts(self):
        """list[SerializableMixin] items auto-restored via from_dict."""
        dt_str = "2026-01-01T00:00:00+00:00"
        data = {
            "trigger": {
                "name": "t",
                "status": "active",
                "created_at": dt_str,
                "score": "1",
                "tags": [],
            },
            "effects": [
                {
                    "name": "e1",
                    "status": "active",
                    "created_at": dt_str,
                    "score": "10",
                    "tags": ["x"],
                },
                {
                    "name": "e2",
                    "status": "inactive",
                    "created_at": dt_str,
                    "score": "20",
                    "tags": [],
                },
            ],
        }
        obj = SampleNested.from_dict(data)
        assert len(obj.effects) == 2
        assert all(isinstance(e, SampleFlat) for e in obj.effects)
        assert obj.effects[0].name == "e1"
        assert obj.effects[1].status == SampleStatus.INACTIVE

    def test_optional_nested_none_passes_through(self):
        """Optional nested field with None passes through."""
        dt_str = "2026-01-01T00:00:00+00:00"
        data = {
            "trigger": {
                "name": "t",
                "status": "active",
                "created_at": dt_str,
                "score": "1",
                "tags": [],
            },
            "metadata": None,
        }
        obj = SampleNested.from_dict(data)
        assert obj.metadata is None


class TestDeserializationCollectionBehavior:
    """Collection type (tuple, set, frozenset) restoration behavior."""

    def test_tuple_field_restored_from_list(self):
        """tuple field auto-restored from JSON list via type hint."""
        data = {
            "tags": [],
            "methods": [],
            "priority_order": ["high", "medium", "low"],
        }
        obj = SampleCollections.from_dict(data)
        assert isinstance(obj.priority_order, tuple)
        assert obj.priority_order == ("high", "medium", "low")

    def test_set_field_restored_from_list(self):
        """set field auto-restored from JSON list via type hint."""
        data = {"tags": ["a", "b", "c"], "methods": [], "priority_order": []}
        obj = SampleCollections.from_dict(data)
        assert isinstance(obj.tags, set)
        assert obj.tags == {"a", "b", "c"}

    def test_frozenset_field_restored_from_list(self):
        """frozenset field auto-restored from JSON list via type hint."""
        data = {
            "tags": [],
            "methods": ["GET", "POST"],
            "priority_order": [],
        }
        obj = SampleCollections.from_dict(data)
        assert isinstance(obj.methods, frozenset)
        assert obj.methods == frozenset({"GET", "POST"})


class TestDeserializationForwardCompatBehavior:
    """from_dict() forward/backward compatibility behavior."""

    def test_extra_keys_silently_ignored(self):
        """Extra keys in data are silently ignored (forward compatibility)."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "1",
            "tags": [],
            "unknown_field": "should be ignored",
            "another_extra": 42,
        }
        obj = SampleFlat.from_dict(data)
        assert obj.name == "test"
        assert not hasattr(obj, "unknown_field")

    def test_missing_optional_keys_use_defaults(self):
        """Missing optional fields use dataclass defaults."""
        data = {"name": "test"}
        obj = SampleOptional.from_dict(data)
        assert obj.status is None
        assert obj.activated_at is None
        assert obj.description is None
        assert obj.count == 0

    def test_missing_required_keys_raises_type_error(self):
        """Missing required fields raise TypeError (dataclass behavior)."""
        data = {"value": 42}  # missing "name" which is required
        with pytest.raises(TypeError):
            SampleRequired.from_dict(data)

    def test_post_init_triggered_on_from_dict(self):
        """__post_init__() is triggered by from_dict() via cls() call."""
        data = {"name": "test"}
        obj = SampleWithPostInit.from_dict(data)
        assert obj.validated is True


class TestDeserializeFieldHookBehavior:
    """_deserialize_field() hook override behavior."""

    def test_custom_deserialize_field_hook_used(self):
        """Custom _deserialize_field hook overrides default behavior."""
        data = {"name": "test", "priority": "high"}
        obj = SampleWithCustomDeserialize.from_dict(data)
        assert obj.priority == SamplePriority.HIGH

    def test_custom_deserialize_field_hook_fallback(self):
        """Custom _deserialize_field hook falls through to super() for other fields."""
        data = {"name": "my_name", "priority": "low"}
        obj = SampleWithCustomDeserialize.from_dict(data)
        assert obj.name == "my_name"


# =============================================================================
# D. Advanced Behavior Tests
# =============================================================================


class TestTypeHintsCacheBehavior:
    """Type hints cache per-class isolation behavior."""

    def test_cache_is_per_class_not_shared(self):
        """_type_hints_cache is per-class — siblings don't share cache."""

        @dataclass
        class ClassA(SerializableMixin):
            x: int = 0

        @dataclass
        class ClassB(SerializableMixin):
            y: str = ""

        # Trigger cache population
        ClassA.from_dict({"x": 1})
        ClassB.from_dict({"y": "hello"})

        # Cache should be per-class
        assert ClassA._type_hints_cache is not ClassB._type_hints_cache
        assert "x" in ClassA._type_hints_cache
        assert "y" in ClassB._type_hints_cache

    def test_cache_populated_on_first_from_dict(self):
        """Cache is populated on first from_dict call, reused on subsequent calls."""

        @dataclass
        class CachedClass(SerializableMixin):
            name: str = ""

        assert CachedClass._type_hints_cache is None
        CachedClass.from_dict({"name": "first"})
        assert CachedClass._type_hints_cache is not None

        # Second call reuses cache
        cached_ref = CachedClass._type_hints_cache
        CachedClass.from_dict({"name": "second"})
        assert CachedClass._type_hints_cache is cached_ref


class TestTypeCheckingNameErrorBehavior:
    """TYPE_CHECKING NameError fallback behavior."""

    def test_nameerror_fallback_returns_empty_dict(self):
        """NameError in get_type_hints() falls back to empty dict cache."""
        # Simulate a class where get_type_hints would fail
        # by testing the _get_cached_hints mechanism
        from unittest.mock import patch

        @dataclass
        class ProblemClass(SerializableMixin):
            name: str = ""

        # Ensure no cache in ProblemClass own __dict__ (fresh class inherits None from parent)
        assert "_type_hints_cache" not in ProblemClass.__dict__

        with patch(
            "baldur.core.serializable.get_type_hints",
            side_effect=NameError("name 'SomeType' is not defined"),
        ):
            hints = ProblemClass._get_cached_hints()

        assert hints == {}
        assert ProblemClass._type_hints_cache == {}


class TestInheritanceBehavior:
    """Inheritance chain behavior (super().to_dict())."""

    def test_child_class_includes_parent_fields(self):
        """Child class to_dict() includes all parent fields."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        child = SampleChild(
            name="child",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("50"),
            tags=["tag1"],
            extra_field="extra_value",
            child_priority=SamplePriority.HIGH,
        )
        result = child.to_dict()

        # Parent fields
        assert result["name"] == "child"
        assert result["status"] == "active"
        assert result["created_at"] == dt.isoformat()
        assert result["score"] == "50"
        assert result["tags"] == ["tag1"]
        # Child fields
        assert result["extra_field"] == "extra_value"
        assert result["child_priority"] == "high"

    def test_child_class_from_dict_restores_all_fields(self):
        """Child class from_dict() restores both parent and child fields including Enum."""
        data = {
            "name": "child",
            "status": "inactive",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "50",
            "tags": ["tag1"],
            "extra_field": "extra_value",
            "child_priority": "high",
        }
        obj = SampleChild.from_dict(data)
        assert isinstance(obj, SampleChild)
        assert obj.name == "child"
        assert obj.status == SampleStatus.INACTIVE
        assert obj.extra_field == "extra_value"
        # Child-specific Enum field must be restored (not stay as str)
        assert obj.child_priority == SamplePriority.HIGH
        assert isinstance(obj.child_priority, SamplePriority)


class TestExplicitOverrideBehavior:
    """Explicit to_dict() override coexistence behavior."""

    def test_explicit_override_takes_precedence(self):
        """Explicit to_dict() override takes precedence over Mixin (MRO)."""
        obj = SampleExplicitOverride(name="test", count=5)
        result = obj.to_dict()
        assert result == {"custom_name": "test", "custom_count": 5}
        # Mixin's from_dict() still works via SerializableMixin
        assert hasattr(SampleExplicitOverride, "from_dict")


class TestNonDataclassUsageBehavior:
    """Non-dataclass usage error behavior."""

    def test_non_dataclass_to_dict_raises_type_error(self):
        """to_dict() on non-dataclass raises TypeError from dataclasses.fields()."""

        class NotADataclass(SerializableMixin):
            pass

        obj = NotADataclass()
        with pytest.raises(TypeError):
            obj.to_dict()

    def test_non_dataclass_from_dict_raises_type_error(self):
        """from_dict() on non-dataclass raises TypeError from dataclasses.fields()."""

        class NotADataclass(SerializableMixin):
            pass

        with pytest.raises(TypeError):
            NotADataclass.from_dict({"name": "test"})


# =============================================================================
# E. Roundtrip & Idempotency Behavior Tests
# =============================================================================


class TestSerializationRoundtripBehavior:
    """Serialization round-trip (to_dict → from_dict) data preservation."""

    def test_flat_dataclass_roundtrip_preserves_all_fields(self):
        """to_dict → from_dict round-trip preserves all fields for flat model."""
        dt = datetime(2026, 3, 15, 14, 30, 0, tzinfo=UTC)
        original = SampleFlat(
            name="roundtrip_test",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("123.456"),
            tags=["x", "y", "z"],
        )

        serialized = original.to_dict()
        restored = SampleFlat.from_dict(serialized)

        assert restored.name == original.name
        assert restored.status == original.status
        assert restored.created_at == original.created_at
        assert restored.score == original.score
        assert isinstance(restored.score, Decimal)
        assert restored.tags == original.tags

    def test_optional_fields_roundtrip_preserves_values(self):
        """Round-trip preserves Optional field values (both present and None)."""
        dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        original = SampleOptional(
            name="opt_test",
            status=SampleStatus.INACTIVE,
            activated_at=dt,
            description="hello",
            count=42,
        )

        serialized = original.to_dict()
        restored = SampleOptional.from_dict(serialized)

        assert restored.name == original.name
        assert restored.status == original.status
        assert restored.activated_at == original.activated_at
        assert restored.description == original.description
        assert restored.count == original.count

    def test_nested_model_roundtrip_preserves_all_fields(self):
        """Round-trip preserves nested object fields."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        inner1 = SampleFlat(
            name="inner1",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("1"),
        )
        inner2 = SampleFlat(
            name="inner2",
            status=SampleStatus.PENDING,
            created_at=dt,
            score=Decimal("2"),
        )
        original = SampleNested(
            trigger=inner1, effects=[inner1, inner2], metadata=inner2
        )

        serialized = original.to_dict()
        restored = SampleNested.from_dict(serialized)

        assert isinstance(restored.trigger, SampleFlat)
        assert restored.trigger.name == original.trigger.name
        assert restored.trigger.status == original.trigger.status
        assert len(restored.effects) == 2
        assert restored.effects[0].name == original.effects[0].name
        assert isinstance(restored.metadata, SampleFlat)
        assert restored.metadata.name == original.metadata.name

    def test_collection_fields_roundtrip_preserves_values(self):
        """Round-trip preserves set/frozenset/tuple field values."""
        original = SampleCollections(
            tags={"a", "b", "c"},
            methods=frozenset({"GET", "POST"}),
            priority_order=("high", "low"),
        )

        serialized = original.to_dict()
        restored = SampleCollections.from_dict(serialized)

        assert restored.tags == original.tags
        assert isinstance(restored.tags, set)
        assert restored.methods == original.methods
        assert isinstance(restored.methods, frozenset)
        assert restored.priority_order == original.priority_order
        assert isinstance(restored.priority_order, tuple)

    def test_child_class_roundtrip_preserves_all_fields(self):
        """Round-trip preserves both parent and child fields."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        original = SampleChild(
            name="child",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("99"),
            extra_field="extended",
            child_priority=SamplePriority.HIGH,
        )

        serialized = original.to_dict()
        restored = SampleChild.from_dict(serialized)

        assert restored.name == original.name
        assert restored.status == original.status
        assert restored.extra_field == original.extra_field
        assert restored.child_priority == original.child_priority
        assert isinstance(restored.child_priority, SamplePriority)


class TestSerializationIdempotencyBehavior:
    """Serialization idempotency — multiple calls produce identical results."""

    def test_to_dict_called_twice_returns_identical_result(self):
        """to_dict() called twice returns identical output."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        obj = SampleFlat(
            name="test",
            status=SampleStatus.ACTIVE,
            created_at=dt,
            score=Decimal("1"),
        )
        result1 = obj.to_dict()
        result2 = obj.to_dict()
        assert result1 == result2

    def test_from_dict_called_twice_returns_equal_objects(self):
        """from_dict() called twice with same data returns equal objects."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "1",
            "tags": [],
        }
        obj1 = SampleFlat.from_dict(data)
        obj2 = SampleFlat.from_dict(data)
        assert obj1.name == obj2.name
        assert obj1.status == obj2.status
        assert obj1.created_at == obj2.created_at


class TestDataImmutabilityBehavior:
    """from_dict does not mutate input data."""

    def test_from_dict_does_not_mutate_input_dict(self):
        """from_dict() does not modify the original input dictionary."""
        data = {
            "name": "test",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "1",
            "tags": ["a", "b"],
        }
        data_copy = {
            k: v if not isinstance(v, list) else v.copy() for k, v in data.items()
        }

        SampleFlat.from_dict(data)

        assert data == data_copy

    def test_from_dict_does_not_mutate_nested_dicts(self):
        """from_dict() does not mutate nested dict values in input."""
        trigger_data = {
            "name": "inner",
            "status": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "score": "1",
            "tags": [],
        }
        trigger_copy = trigger_data.copy()
        data = {"trigger": trigger_data, "effects": [], "metadata": None}

        SampleNested.from_dict(data)

        assert trigger_data == trigger_copy


# =============================================================================
# F. 363B Enhancement Tests — IntEnum + sorted() fallback
# =============================================================================


class TestIntEnumSerializationBehavior:
    """IntEnum serialization behavior (363B — EmergencyLevel pattern)."""

    def test_int_enum_serialized_as_int_value(self):
        """IntEnum field serialized to .value (int)."""
        obj = SampleWithIntEnum(name="test", level=SampleIntLevel.LEVEL_2)
        result = obj.to_dict()
        assert result["level"] == 2
        assert isinstance(result["level"], int)

    def test_optional_int_enum_present_serialized_as_int(self):
        """Optional IntEnum field (present) serialized to .value (int)."""
        obj = SampleWithIntEnum(name="test", optional_level=SampleIntLevel.LEVEL_3)
        result = obj.to_dict()
        assert result["optional_level"] == 3

    def test_optional_int_enum_none_serialized_as_none(self):
        """Optional IntEnum field (None) serialized as None."""
        obj = SampleWithIntEnum(name="test", optional_level=None)
        result = obj.to_dict()
        assert result["optional_level"] is None


class TestIntEnumDeserializationBehavior:
    """IntEnum deserialization behavior (363B fix — int values restored)."""

    def test_int_enum_restored_from_int_value(self):
        """IntEnum field auto-restored from int value via type hint."""
        data = {"name": "test", "level": 2}
        obj = SampleWithIntEnum.from_dict(data)
        assert obj.level == SampleIntLevel.LEVEL_2
        assert isinstance(obj.level, SampleIntLevel)

    def test_optional_int_enum_restored_from_int_value(self):
        """Optional IntEnum field (present) auto-restored from int."""
        data = {"name": "test", "optional_level": 3}
        obj = SampleWithIntEnum.from_dict(data)
        assert obj.optional_level == SampleIntLevel.LEVEL_3
        assert isinstance(obj.optional_level, SampleIntLevel)

    def test_optional_int_enum_none_passes_through(self):
        """Optional IntEnum field with None passes through as None."""
        data = {"name": "test", "optional_level": None}
        obj = SampleWithIntEnum.from_dict(data)
        assert obj.optional_level is None

    def test_already_int_enum_instance_not_double_converted(self):
        """Already-converted IntEnum instance passes through unchanged."""
        data = {"name": "test", "level": SampleIntLevel.LEVEL_1}
        obj = SampleWithIntEnum.from_dict(data)
        assert obj.level is SampleIntLevel.LEVEL_1

    def test_invalid_int_enum_value_passes_through(self):
        """Invalid int value for IntEnum passes through (no crash)."""
        data = {"name": "test", "level": 999}
        obj = SampleWithIntEnum.from_dict(data)
        assert obj.level == 999

    def test_int_enum_roundtrip_preserves_value(self):
        """to_dict → from_dict round-trip preserves IntEnum field."""
        original = SampleWithIntEnum(
            name="roundtrip",
            level=SampleIntLevel.LEVEL_3,
            optional_level=SampleIntLevel.LEVEL_1,
        )
        serialized = original.to_dict()
        restored = SampleWithIntEnum.from_dict(serialized)
        assert restored.level == original.level
        assert isinstance(restored.level, SampleIntLevel)
        assert restored.optional_level == original.optional_level
        assert isinstance(restored.optional_level, SampleIntLevel)


class TestSortedTypeErrorFallbackBehavior:
    """sorted() TypeError fallback for non-comparable set items (363B)."""

    def test_set_with_non_comparable_items_does_not_crash(self):
        """Set containing non-comparable items serializes without crash."""
        obj = SampleWithNonComparableSet(
            name="test",
            mixed_items={1, "two", 3.0},
        )
        result = obj.to_dict()
        assert isinstance(result["mixed_items"], list)
        assert set(result["mixed_items"]) == {1, "two", 3.0}

    def test_set_with_comparable_items_still_sorted(self):
        """Set with comparable items still produces sorted output."""
        obj = SampleWithNonComparableSet(
            name="test",
            mixed_items={"c", "a", "b"},
        )
        result = obj.to_dict()
        assert result["mixed_items"] == ["a", "b", "c"]
