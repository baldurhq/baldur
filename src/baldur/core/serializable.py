"""SerializableMixin — automatic to_dict()/from_dict() for dataclasses.

Provides standard type conversions (Enum, datetime, Decimal, set, frozenset,
tuple, nested objects) with orjson-compatible output. Designed as the foundation
for eliminating boilerplate serialization across 300+ dataclass models.

Customization hooks:
    - _post_serialize(): conditional field manipulation after serialization
    - _deserialize_field(): custom field restoration (discriminated unions, etc.)
    - exclude_none ClassVar: omit None fields from output

Exclusions (keep explicit to_dict() override):
    - Abbreviated/renamed dict keys (e.g., WALEntry: sequence → "seq")
    - Computed fields or method calls in to_dict()
    - Structural reshape (flat fields → nested groups)
    - NamedTuple, TypedDict, Pydantic BaseSettings
"""

from __future__ import annotations

import dataclasses
import types
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar, Self, Union, get_args, get_origin, get_type_hints

__all__ = ["SerializableMixin"]


class SerializableMixin:
    """Mixin for dataclasses — provides to_dict()/from_dict() with automatic type conversion.

    Supports:
    - Enum → .value
    - datetime → .isoformat()
    - Decimal → str
    - set/frozenset → sorted list
    - tuple → list
    - Nested SerializableMixin / objects with to_dict() → recursive
    - list/tuple/dict → recursive descent

    Customization:
    - _post_serialize() hook for conditional fields, structural reshaping
    - _deserialize_field() hook for custom field restoration
    - exclude_none class var for omitting None fields

    Exclusions (keep explicit override):
    - Classes with abbreviated/renamed dict keys (e.g., WALEntry: sequence → "seq")
    - Classes with computed fields or method calls in to_dict()
    - NamedTuple, TypedDict, Pydantic BaseSettings
    """

    exclude_none: ClassVar[bool] = False
    _type_hints_cache: ClassVar[dict[str, Any] | None] = None

    # --- Serialization (to_dict / to_json) ---

    def to_json(self) -> str:
        """Serialize to compact JSON string via fast_dumps_str.

        Uses to_dict() for type conversion (Enum, datetime, Decimal),
        then fast_dumps_str for JSON encoding.
        """
        from baldur.utils.serialization import fast_dumps_str

        return fast_dumps_str(self.to_dict())

    def to_json_bytes(self) -> bytes:
        """Serialize to compact JSON bytes via fast_dumps.

        For I/O paths (WAL, Kafka, HTTP) where bytes are preferred.
        """
        from baldur.utils.serialization import fast_dumps

        return fast_dumps(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            result[f.name] = self._serialize_value(value)
        return self._post_serialize(result)

    def _serialize_value(self, value: Any) -> Any:
        """Recursively serialize a value to orjson-compatible types."""
        if value is None:
            return None
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if hasattr(value, "to_dict"):
            return value.to_dict()
        if isinstance(value, (list, tuple)):
            return [self._serialize_value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            try:
                return sorted(self._serialize_value(item) for item in value)
            except TypeError:
                return [self._serialize_value(item) for item in value]
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        # Passthrough: str, int, float, bool
        return value

    def _post_serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Hook for subclasses to customize serialization output.

        When overriding, call super()._post_serialize(data) at the end
        if you want exclude_none filtering applied after your customization.
        """
        if self.exclude_none:
            return {k: v for k, v in data.items() if v is not None}
        return data

    # --- Deserialization (from_dict) ---

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Create instance from dict, filtering to valid dataclass fields only.

        - Extra keys in data are silently ignored (forward compatibility).
        - Missing optional fields use dataclass defaults.
        - Missing required fields raise TypeError (intentional — matches codebase convention).
        - Triggers __post_init__() automatically via cls() call.
        - InitVar fields are not serialized (not in dataclasses.fields()) and must be
          supplied separately if required — this is intentional.
        """
        valid_fields = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
        filtered = {}
        for k, v in data.items():
            if k in valid_fields:
                filtered[k] = cls._deserialize_field(k, v)
        return cls(**filtered)

    @classmethod
    def _get_cached_hints(cls) -> dict[str, Any]:
        """Return type hints with per-class caching.

        Cache avoids repeated get_type_hints() calls which resolve string
        annotations (from __future__ import annotations is used in 920+ files).
        NameError fallback handles TYPE_CHECKING-only imports (14 files affected).
        """
        if "_type_hints_cache" not in cls.__dict__:
            try:
                cls._type_hints_cache = get_type_hints(cls)
            except NameError:
                # TYPE_CHECKING import not available at runtime
                cls._type_hints_cache = {}
        assert cls._type_hints_cache is not None  # populated above
        return cls._type_hints_cache

    @classmethod
    def _unwrap_optional(cls, hint: Any) -> Any:
        """Unwrap Optional[X] / X | None → X.

        Handles both typing.Optional[X] (typing.Union) and PEP 604 X | None
        (types.UnionType) syntax. Returns the inner type if Optional, otherwise
        returns hint unchanged.
        """
        origin = get_origin(hint)
        if origin is Union or isinstance(hint, types.UnionType):
            args = [a for a in get_args(hint) if a is not type(None)]
            if len(args) == 1:
                return args[0]
        return hint

    @classmethod
    def _deserialize_field(cls, field_name: str, value: Any) -> Any:  # noqa: C901, PLR0912
        """Auto-restore Enum, datetime, nested objects from type hints.

        Restoration patterns (based on codebase analysis):
        - Enum: EnumClass(string_value) — all Enums use (str, Enum) pattern
        - datetime: datetime.fromisoformat(string) — universal in codebase
        - Nested SerializableMixin: NestedClass.from_dict(dict)
        - list[SerializableMixin]: [Cls.from_dict(item) for item in list]
        - tuple: tuple(list) — JSON has no tuple type
        - set/frozenset: set(list) / frozenset(list)

        Override in subclasses for:
        - Discriminated unions (check field presence to determine class)
        - Non-standard type conversions
        - Legacy format migration
        """
        if value is None:
            return None

        hints = cls._get_cached_hints()
        hint = hints.get(field_name)
        if hint is None:
            return value

        # Unwrap Optional[X] / X | None
        hint = cls._unwrap_optional(hint)
        origin = get_origin(hint)

        # Enum restoration — supports both str and int values (str,Enum / IntEnum)
        if isinstance(hint, type) and issubclass(hint, Enum):
            if isinstance(value, hint):
                return value
            try:
                return hint(value)
            except (ValueError, KeyError):
                return value

        # datetime restoration
        if isinstance(hint, type) and issubclass(hint, datetime):
            return datetime.fromisoformat(value) if isinstance(value, str) else value

        # Decimal restoration
        if isinstance(hint, type) and issubclass(hint, Decimal):
            return Decimal(value) if isinstance(value, str) else value

        # Nested SerializableMixin restoration
        if isinstance(hint, type) and issubclass(hint, SerializableMixin):
            return hint.from_dict(value) if isinstance(value, dict) else value

        # list[SerializableMixin] restoration
        if origin is list:
            args = get_args(hint)
            if (
                args
                and isinstance(args[0], type)
                and issubclass(args[0], SerializableMixin)
            ):
                item_cls = args[0]
                return [
                    item_cls.from_dict(item) if isinstance(item, dict) else item
                    for item in value
                ]

        # tuple restoration from JSON list
        if origin is tuple:
            return tuple(value) if isinstance(value, list) else value
        if isinstance(hint, type) and issubclass(hint, tuple):
            return tuple(value) if isinstance(value, list) else value

        # set / frozenset restoration from JSON list
        # origin check handles parameterized types (set[str], frozenset[str])
        if origin is set or (isinstance(hint, type) and issubclass(hint, set)):
            return set(value) if isinstance(value, (list, tuple)) else value
        if origin is frozenset or (
            isinstance(hint, type) and issubclass(hint, frozenset)
        ):
            return frozenset(value) if isinstance(value, (list, tuple)) else value

        return value
