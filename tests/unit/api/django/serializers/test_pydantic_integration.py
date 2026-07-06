"""Pydantic-DRF integration helper — unit tests (523 Step 7).

Covers ``baldur.api.django.serializers.pydantic_integration`` end-to-end:

- Per-type handlers: ``_handle_integer`` / ``_handle_number`` (with and without
  ``minimum`` / ``maximum`` numeric constraints), ``_handle_boolean``,
  ``_handle_string`` (plain CharField, ``maxLength``, and ``enum`` branches),
  ``_handle_array`` (``$ref`` skip, non-dict items skip, dict items → recursive
  child resolution), ``_handle_object`` (``additionalProperties`` typed child
  vs. plain DictField), and ``_get_child_serializer_for_type`` for the known +
  unknown fallback paths.
- ``pydantic_schema_to_drf_field`` registry dispatch — covers default
  propagation, ``description`` → ``help_text`` mapping, and unknown-type
  fallback to ``CharField``.
- ``generate_serializer_fields_from_pydantic`` — schema → field dict with
  ``required`` set membership, exclusion handling, and the empty-schema case.
- ``PydanticSerializerMixin.__init__`` field auto-injection (existing field
  preserved, new field added) and the ``_pydantic_model = None`` no-op path.
- ``validate_with_pydantic`` happy path, no-model passthrough, and
  Pydantic → DRF ``ValidationError`` wrapping.
- ``validate_with_pydantic_partial`` happy path with and without
  ``current_settings``, no-model passthrough, ValidationError wrapping, and
  the "return only changed fields" guarantee.
- ``create_pydantic_serializer`` — base class composition with and without a
  mixin, attribute injection, dynamic ``validate`` callable wired via
  ``super().validate`` when present, and pydantic-side ValidationError
  wrapping inside the dynamic validator.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, field_validator
from rest_framework import serializers

from baldur.api.django.serializers.pydantic_integration import (
    PydanticSerializerMixin,
    _add_numeric_constraints,
    _get_child_serializer_for_type,
    _handle_array,
    _handle_boolean,
    _handle_integer,
    _handle_number,
    _handle_object,
    _handle_string,
    create_pydantic_serializer,
    generate_serializer_fields_from_pydantic,
    pydantic_schema_to_drf_field,
)

# =============================================================================
# Pydantic test fixtures
# =============================================================================


class _SimpleModel(BaseModel):
    """Minimal Pydantic model covering required+optional, numeric bounds."""

    name: str = Field(..., max_length=20, description="Display name")
    count: int = Field(default=0, ge=0, le=100, description="Item count")
    active: bool = True


class _RangeModel(BaseModel):
    threshold: int = Field(..., ge=1, le=10)

    @field_validator("threshold")
    @classmethod
    def _must_be_odd(cls, v: int) -> int:
        if v % 2 == 0:
            raise ValueError("threshold must be odd")
        return v


class _NoFieldsModel(BaseModel):
    pass


# =============================================================================
# _add_numeric_constraints
# =============================================================================


class TestAddNumericConstraints:
    def test_no_bounds_leaves_kwargs_untouched(self):
        kwargs: dict = {"required": True}
        _add_numeric_constraints(kwargs, {})
        assert kwargs == {"required": True}

    def test_only_minimum(self):
        kwargs: dict = {}
        _add_numeric_constraints(kwargs, {"minimum": 5})
        assert kwargs == {"min_value": 5}

    def test_only_maximum(self):
        kwargs: dict = {}
        _add_numeric_constraints(kwargs, {"maximum": 99})
        assert kwargs == {"max_value": 99}

    def test_both_bounds(self):
        kwargs: dict = {}
        _add_numeric_constraints(kwargs, {"minimum": 0, "maximum": 10})
        assert kwargs == {"min_value": 0, "max_value": 10}


# =============================================================================
# Per-type handlers
# =============================================================================


class TestHandleInteger:
    def test_returns_integer_field_with_bounds(self):
        kwargs: dict = {"required": True}
        field = _handle_integer({"minimum": 0, "maximum": 5}, kwargs, "n")
        assert isinstance(field, serializers.IntegerField)
        assert field.min_value == 0
        assert field.max_value == 5
        assert field.required is True

    def test_no_bounds(self):
        field = _handle_integer({}, {"required": False}, "n")
        assert isinstance(field, serializers.IntegerField)


class TestHandleNumber:
    def test_returns_float_field_with_bounds(self):
        field = _handle_number({"minimum": 0.5, "maximum": 1.5}, {}, "f")
        assert isinstance(field, serializers.FloatField)
        assert field.min_value == 0.5
        assert field.max_value == 1.5

    def test_no_bounds(self):
        field = _handle_number({}, {}, "f")
        assert isinstance(field, serializers.FloatField)


class TestHandleBoolean:
    def test_returns_boolean_field(self):
        field = _handle_boolean({}, {"required": False}, "b")
        assert isinstance(field, serializers.BooleanField)
        assert field.required is False


class TestHandleString:
    def test_plain_char_field(self):
        field = _handle_string({}, {"required": True}, "s")
        assert isinstance(field, serializers.CharField)
        assert field.required is True

    def test_max_length_propagates(self):
        field = _handle_string({"maxLength": 32}, {}, "s")
        assert isinstance(field, serializers.CharField)
        assert field.max_length == 32

    def test_enum_returns_choice_field(self):
        field = _handle_string({"enum": ["a", "b", "c"]}, {}, "mode")
        assert isinstance(field, serializers.ChoiceField)
        assert set(field.choices.keys()) == {"a", "b", "c"}

    def test_enum_takes_precedence_over_max_length(self):
        # Both keys present → enum branch wins; maxLength is intentionally
        # dropped because ChoiceField does not accept ``max_length`` (would
        # raise TypeError at construction).
        field = _handle_string({"enum": ["one", "two"], "maxLength": 10}, {}, "mode")
        assert isinstance(field, serializers.ChoiceField)
        assert set(field.choices.keys()) == {"one", "two"}


class TestGetChildSerializerForType:
    @pytest.mark.parametrize(
        ("child_type", "expected_cls"),
        [
            ("integer", serializers.IntegerField),
            ("number", serializers.FloatField),
            ("boolean", serializers.BooleanField),
            ("string", serializers.CharField),
            ("unknown_type", serializers.CharField),
            ("", serializers.CharField),
        ],
    )
    def test_returns_expected_class(self, child_type, expected_cls):
        child = _get_child_serializer_for_type(child_type)
        assert isinstance(child, expected_cls)


class TestHandleArray:
    def test_ref_items_skip_to_dict_listfield(self):
        kwargs = {"required": False, "help_text": "stripped"}
        field = _handle_array({"items": {"$ref": "#/defs/X"}}, kwargs, "arr")
        assert isinstance(field, serializers.ListField)
        assert isinstance(field.child, serializers.DictField)
        # help_text must be popped before construction
        assert "help_text" not in kwargs

    def test_non_dict_items_skip(self):
        field = _handle_array({"items": "not_a_dict"}, {"help_text": "x"}, "arr")
        assert isinstance(field, serializers.ListField)
        assert isinstance(field.child, serializers.DictField)

    def test_dict_items_resolve_recursively(self):
        field = _handle_array({"items": {"type": "integer"}}, {}, "arr")
        assert isinstance(field, serializers.ListField)
        assert isinstance(field.child, serializers.IntegerField)

    def test_missing_items_key_defaults_to_empty_dict(self):
        # When items is absent, default {} → no type → CharField child
        field = _handle_array({}, {}, "arr")
        assert isinstance(field, serializers.ListField)
        assert isinstance(field.child, serializers.CharField)


class TestHandleObject:
    def test_typed_additional_properties(self):
        field = _handle_object({"additionalProperties": {"type": "integer"}}, {}, "m")
        assert isinstance(field, serializers.DictField)
        assert isinstance(field.child, serializers.IntegerField)

    def test_additional_properties_default_string_type(self):
        # additionalProperties dict without explicit type → defaults to "string"
        field = _handle_object({"additionalProperties": {}}, {}, "m")
        assert isinstance(field, serializers.DictField)
        # Empty dict is falsy → branch falls through to plain DictField
        assert not isinstance(field.child, serializers.IntegerField)

    def test_additional_properties_non_dict_falls_through(self):
        field = _handle_object({"additionalProperties": True}, {}, "m")
        assert isinstance(field, serializers.DictField)

    def test_no_additional_properties(self):
        field = _handle_object({}, {}, "m")
        assert isinstance(field, serializers.DictField)


# =============================================================================
# pydantic_schema_to_drf_field dispatch
# =============================================================================


class TestPydanticSchemaToDrfField:
    def test_default_propagates(self):
        field = pydantic_schema_to_drf_field(
            {"type": "integer", "default": 5}, "n", required=False
        )
        assert isinstance(field, serializers.IntegerField)
        assert field.default == 5

    def test_description_maps_to_help_text(self):
        field = pydantic_schema_to_drf_field(
            {"type": "string", "description": "desc"}, "s", required=True
        )
        assert isinstance(field, serializers.CharField)
        assert field.help_text == "desc"

    def test_unknown_type_falls_back_to_char_field(self):
        field = pydantic_schema_to_drf_field({"type": "weird"}, "x", required=False)
        assert isinstance(field, serializers.CharField)

    def test_missing_type_falls_back_to_char_field(self):
        field = pydantic_schema_to_drf_field({}, "x", required=False)
        assert isinstance(field, serializers.CharField)

    def test_required_kwarg_propagates(self):
        field = pydantic_schema_to_drf_field({"type": "boolean"}, "b", required=True)
        assert field.required is True


# =============================================================================
# generate_serializer_fields_from_pydantic
# =============================================================================


class TestGenerateSerializerFieldsFromPydantic:
    def test_all_fields_present(self):
        fields = generate_serializer_fields_from_pydantic(_SimpleModel)
        assert set(fields.keys()) == {"name", "count", "active"}
        assert isinstance(fields["name"], serializers.CharField)
        assert isinstance(fields["count"], serializers.IntegerField)
        assert isinstance(fields["active"], serializers.BooleanField)

    def test_required_set_correctly(self):
        fields = generate_serializer_fields_from_pydantic(_SimpleModel)
        # name has no default → required
        assert fields["name"].required is True
        # count has default → optional
        assert fields["count"].required is False

    def test_exclude_fields_removed(self):
        fields = generate_serializer_fields_from_pydantic(
            _SimpleModel, exclude_fields={"active"}
        )
        assert "active" not in fields
        assert set(fields.keys()) == {"name", "count"}

    def test_exclude_none_is_empty(self):
        fields = generate_serializer_fields_from_pydantic(
            _SimpleModel, exclude_fields=None
        )
        assert set(fields.keys()) == {"name", "count", "active"}

    def test_empty_model_returns_empty_dict(self):
        fields = generate_serializer_fields_from_pydantic(_NoFieldsModel)
        assert fields == {}


# =============================================================================
# PydanticSerializerMixin
# =============================================================================


class TestPydanticSerializerMixinInit:
    def test_no_model_leaves_fields_untouched(self):
        class _BareSerializer(PydanticSerializerMixin, serializers.Serializer):
            pass

        s = _BareSerializer()
        # No _pydantic_model → no auto-injection happens.
        # `name`/`count`/`active` should not be on the serializer at all.
        assert "name" not in s.fields

    def test_model_fields_auto_injected(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel

        s = _MySerializer()
        assert set(s.fields.keys()) == {"name", "count", "active"}
        assert isinstance(s.fields["name"], serializers.CharField)

    def test_existing_field_takes_precedence(self):
        # When the serializer declares its own field, the auto-injected
        # field with the same name must not overwrite it.
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel
            name = serializers.CharField(max_length=999)

        s = _MySerializer()
        assert s.fields["name"].max_length == 999

    def test_exclude_fields_skipped(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel
            _exclude_fields = {"active"}

        s = _MySerializer()
        assert "active" not in s.fields
        assert "name" in s.fields


class TestValidateWithPydantic:
    def test_no_model_returns_passthrough(self):
        class _Bare(PydanticSerializerMixin, serializers.Serializer):
            pass

        s = _Bare()
        data = {"foo": 1}
        assert s.validate_with_pydantic(data) is data

    def test_happy_path_returns_dumped_model(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel

        s = _MySerializer()
        result = s.validate_with_pydantic({"name": "alice", "count": 3})
        # exclude_unset → only fields actually passed should appear
        assert result == {"name": "alice", "count": 3}

    def test_validation_error_wrapped(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _RangeModel

        s = _MySerializer()
        with pytest.raises(serializers.ValidationError):
            s.validate_with_pydantic({"threshold": 4})  # even → invalid

    def test_exclude_fields_propagates_to_model_dump(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel
            _exclude_fields = {"active"}

        s = _MySerializer()
        result = s.validate_with_pydantic(
            {"name": "alice", "active": False, "count": 2}
        )
        assert "active" not in result
        assert result.get("name") == "alice"


class TestValidateWithPydanticPartial:
    def test_no_model_returns_passthrough(self):
        class _Bare(PydanticSerializerMixin, serializers.Serializer):
            pass

        s = _Bare()
        assert s.validate_with_pydantic_partial({"x": 1}) == {"x": 1}

    def test_with_current_settings_merges_and_returns_changed_only(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _SimpleModel

        s = _MySerializer()
        current = _SimpleModel(name="alice", count=5, active=True)
        result = s.validate_with_pydantic_partial(
            data={"count": 9}, current_settings=current
        )
        # Only the field in `data` should round-trip
        assert result == {"count": 9}

    def test_without_current_settings_uses_defaults(self):
        # _SimpleModel has a required `name` field, so we need a model with
        # all defaults for the "no current_settings" branch.
        class _AllDefault(BaseModel):
            count: int = 0
            active: bool = True

        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _AllDefault

        s = _MySerializer()
        result = s.validate_with_pydantic_partial(data={"count": 7})
        assert result == {"count": 7}

    def test_validation_error_wrapped(self):
        class _MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = _RangeModel

        s = _MySerializer()
        with pytest.raises(serializers.ValidationError):
            s.validate_with_pydantic_partial(
                data={"threshold": 200},  # out of range
                current_settings=_RangeModel(threshold=3),
            )


# =============================================================================
# create_pydantic_serializer
# =============================================================================


class TestCreatePydanticSerializer:
    def test_returns_serializer_subclass(self):
        cls = create_pydantic_serializer(_SimpleModel, "Auto1")
        assert issubclass(cls, serializers.Serializer)
        assert cls.__name__ == "Auto1"

    def test_attributes_attached(self):
        cls = create_pydantic_serializer(
            _SimpleModel, "Auto2", exclude_fields={"active"}
        )
        assert cls._pydantic_model is _SimpleModel
        assert cls._exclude_fields == {"active"}
        # Excluded field was not added as a serializer field
        assert "active" not in cls._declared_fields
        assert "name" in cls._declared_fields

    def test_with_mixin_class_prepended(self):
        class _Marker:
            marker = True

        cls = create_pydantic_serializer(_SimpleModel, "Auto3", mixin_class=_Marker)
        assert issubclass(cls, _Marker)
        assert issubclass(cls, serializers.Serializer)
        # Mixin must be first in MRO before Serializer
        bases = cls.__bases__
        assert bases[0] is _Marker

    def test_validate_happy_path(self):
        cls = create_pydantic_serializer(_SimpleModel, "Auto4")
        s = cls()
        result = s.validate({"name": "bob", "count": 1})
        assert result == {"name": "bob", "count": 1}

    def test_validate_wraps_pydantic_error(self):
        cls = create_pydantic_serializer(_RangeModel, "Auto5")
        s = cls()
        with pytest.raises(serializers.ValidationError):
            s.validate({"threshold": 4})  # even → invalid

    def test_validate_calls_mixin_super_validate(self):
        # When a Mixin provides a `validate` that transforms data, the
        # dynamic validate must call it first.
        class _DoublingMixin:
            def validate(self, data):
                return {k: v * 2 if isinstance(v, int) else v for k, v in data.items()}

        cls = create_pydantic_serializer(
            _SimpleModel, "Auto6", mixin_class=_DoublingMixin
        )
        s = cls()
        # count=2 → mixin doubles to 4 → Pydantic accepts (0..100) → dump = 4
        result = s.validate({"name": "x", "count": 2})
        assert result == {"name": "x", "count": 4}

    def test_exclude_fields_none_falls_back_to_empty_set(self):
        cls = create_pydantic_serializer(_SimpleModel, "Auto7", exclude_fields=None)
        assert cls._exclude_fields == set()
        # All three fields are present
        assert set(cls._declared_fields.keys()) == {"name", "count", "active"}
