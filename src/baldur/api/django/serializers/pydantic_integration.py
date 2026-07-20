"""
Pydantic-DRF Serializer Integration Helper.

Automatic DRF Serializer generation and Pydantic model integration.

This module generates DRF Serializer fields automatically from Pydantic
Settings.
- Pydantic schema -> DRF field conversion
- Validation delegated to the Pydantic model
- Removes duplicated code
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from rest_framework import serializers

# =============================================================================
# Type Handlers for pydantic_schema_to_drf_field (Complexity Reduction)
# =============================================================================


def _add_numeric_constraints(kwargs: dict[str, Any], props: dict[str, Any]) -> None:
    """Add min/max constraints for numeric fields."""
    if "minimum" in props:
        kwargs["min_value"] = props["minimum"]
    if "maximum" in props:
        kwargs["max_value"] = props["maximum"]


def _handle_integer(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle integer type."""
    _add_numeric_constraints(kwargs, props)
    return serializers.IntegerField(**kwargs)


def _handle_number(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle number (float) type."""
    _add_numeric_constraints(kwargs, props)
    return serializers.FloatField(**kwargs)


def _handle_boolean(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle boolean type."""
    return serializers.BooleanField(**kwargs)


def _handle_string(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle string type.

    ``enum`` takes precedence over ``maxLength``: when both are present
    the enum members already bound the value space, and ``ChoiceField``
    does not accept ``max_length`` (would raise ``TypeError`` at
    construction time).
    """
    if "enum" in props:
        kwargs["choices"] = props["enum"]
        return serializers.ChoiceField(**kwargs)
    if "maxLength" in props:
        kwargs["max_length"] = props["maxLength"]
    return serializers.CharField(**kwargs)


def _handle_array(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle array type."""
    items = props.get("items", {})
    kwargs.pop("help_text", None)  # ListField doesn't take help_text on child

    # Handle $ref or complex items (skip and use generic ListField)
    if not isinstance(items, dict) or "$ref" in items:
        return serializers.ListField(child=serializers.DictField(), **kwargs)

    child_field = pydantic_schema_to_drf_field(
        items, f"{field_name}_item", required=False
    )
    return serializers.ListField(child=child_field, **kwargs)


def _get_child_serializer_for_type(child_type: str) -> serializers.Field:
    """Get child serializer based on type string."""
    type_mapping = {
        "integer": serializers.IntegerField,
        "number": serializers.FloatField,
        "boolean": serializers.BooleanField,
    }
    return type_mapping.get(child_type, serializers.CharField)()


def _handle_object(
    props: dict[str, Any], kwargs: dict[str, Any], field_name: str
) -> serializers.Field:
    """Handle object/dict type."""
    additional_props = props.get("additionalProperties", {})
    if additional_props and isinstance(additional_props, dict):
        child_type = additional_props.get("type", "string")
        child = _get_child_serializer_for_type(child_type)
        return serializers.DictField(child=child, **kwargs)
    return serializers.DictField(**kwargs)


# Type handler registry
_TYPE_HANDLERS: dict[
    str, Callable[[dict[str, Any], dict[str, Any], str], serializers.Field]
] = {
    "integer": _handle_integer,
    "number": _handle_number,
    "boolean": _handle_boolean,
    "string": _handle_string,
    "array": _handle_array,
    "object": _handle_object,
}


def pydantic_schema_to_drf_field(
    props: dict[str, Any],
    field_name: str,
    required: bool = False,
) -> serializers.Field:
    """
    Convert a Pydantic JSON Schema property into a DRF Field.

    Args:
        props: property information from the Pydantic schema
        field_name: field name
        required: whether the field is required

    Returns:
        DRF Serializer Field
    """
    field_type = props.get("type") or ""
    kwargs: dict[str, Any] = {
        "required": required,
        "help_text": props.get("description", ""),
    }

    if "default" in props:
        kwargs["default"] = props["default"]

    # Use handler from registry, fallback to CharField
    handler = _TYPE_HANDLERS.get(field_type)
    if handler:
        return handler(props, kwargs, field_name)

    return serializers.CharField(**kwargs)


def generate_serializer_fields_from_pydantic(
    pydantic_model: type[BaseModel],
    exclude_fields: set | None = None,
) -> dict[str, serializers.Field]:
    """
    Build a DRF Serializer field dict from a Pydantic model.

    Args:
        pydantic_model: Pydantic BaseModel class
        exclude_fields: set of field names to exclude

    Returns:
        {field_name: DRF Field} dict
    """
    exclude = exclude_fields or set()
    schema = pydantic_model.model_json_schema()
    required_fields = set(schema.get("required", []))
    properties = schema.get("properties", {})

    fields = {}
    for name, props in properties.items():
        if name in exclude:
            continue

        is_required = name in required_fields
        fields[name] = pydantic_schema_to_drf_field(props, name, required=is_required)

    return fields


class PydanticSerializerMixin:
    """
    DRF Serializer Mixin integrated with a Pydantic model.

    Usage:
        class MySerializer(PydanticSerializerMixin, serializers.Serializer):
            _pydantic_model = MyPydanticSettings
            _exclude_fields = {"internal_field"}

            def validate(self, data):
                validated = super().validate(data)
                return self.validate_with_pydantic(validated)
    """

    _pydantic_model: type[BaseModel] | None = None
    _exclude_fields: set = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self._pydantic_model:
            # Generate fields automatically from the Pydantic schema
            pydantic_fields = generate_serializer_fields_from_pydantic(
                self._pydantic_model,
                exclude_fields=self._exclude_fields,
            )

            # Merge with existing fields (existing fields win)
            for name, field in pydantic_fields.items():
                if name not in self.fields:
                    self.fields[name] = field

    def validate_with_pydantic(self, data: dict) -> dict:
        """
        Delegate validation to the Pydantic model.

        Args:
            data: data to validate

        Returns:
            validated data (converted by the Pydantic model)

        Raises:
            serializers.ValidationError: on validation failure
        """
        if not self._pydantic_model:
            return data

        try:
            # Partial update: merge with the current settings
            validated_model = self._pydantic_model(**data)
            # Return only the fields actually supplied (exclude_unset)
            return validated_model.model_dump(
                exclude_unset=True,
                exclude=self._exclude_fields,
            )
        except ValidationError as e:
            # Surface only the pydantic validation message; any other exception
            # propagates (500) instead of leaking its detail to the client.
            raise serializers.ValidationError(str(e)) from e

    def validate_with_pydantic_partial(
        self,
        data: dict,
        current_settings: BaseModel | None = None,
    ) -> dict:
        """
        Pydantic validation with partial-update support.

        On a PATCH request, validates only the changed fields and merges them
        with the current values.

        Args:
            data: dict containing only the fields to change
            current_settings: current settings instance (defaults are used
                when omitted)

        Returns:
            merged validated data (changed fields only)

        Raises:
            serializers.ValidationError: on validation failure

        Example:
            # PATCH /api/v1/config/circuit-breaker/
            # Body: {"failure_threshold": 10}

            current = CircuitBreakerSettings()  # load current settings
            changes = serializer.validate_with_pydantic_partial(
                data={"failure_threshold": 10},
                current_settings=current,
            )
            # changes = {"failure_threshold": 10}  # changed fields only
        """
        if not self._pydantic_model:
            return data

        try:
            if current_settings:
                # Merge with the current values, then validate
                current_dict = current_settings.model_dump()
                current_dict.update(data)
                validated = self._pydantic_model.model_validate(current_dict)
            else:
                # Merge with the defaults (build the full model, then override
                # only the supplied data)
                defaults = self._pydantic_model()
                merged = defaults.model_dump()
                merged.update(data)
                validated = self._pydantic_model.model_validate(merged)

            # Return only the fields that actually changed
            return {k: v for k, v in validated.model_dump().items() if k in data}
        except ValidationError as e:
            raise serializers.ValidationError(str(e)) from e


def create_pydantic_serializer(
    pydantic_model: type[BaseModel],
    serializer_name: str,
    mixin_class: type | None = None,
    exclude_fields: set | None = None,
) -> type[serializers.Serializer]:
    """
    Build a DRF Serializer class dynamically from a Pydantic model.

    Args:
        pydantic_model: Pydantic BaseModel class
        serializer_name: name of the Serializer class to create
        mixin_class: extra Mixin class to add (e.g. ApplyStrategyMixin)
        exclude_fields: set of field names to exclude

    Returns:
        the dynamically created Serializer class

    Example:
        >>> from baldur.settings import CircuitBreakerSettings
        >>> CBSerializer = create_pydantic_serializer(
        ...     CircuitBreakerSettings,
        ...     "CircuitBreakerSerializer",
        ...     mixin_class=ApplyStrategyMixin,
        ... )
    """
    exclude = exclude_fields or set()

    # Build the fields
    fields = generate_serializer_fields_from_pydantic(
        pydantic_model, exclude_fields=exclude
    )

    # Class attributes
    attrs = {
        "_pydantic_model": pydantic_model,
        "_exclude_fields": exclude,
        **fields,
    }

    # Add the validate method
    def validate(self, data):
        # Call the Mixin's validate, if any
        if hasattr(super(self.__class__, self), "validate"):
            data = super(self.__class__, self).validate(data)

        # Validate through the Pydantic model
        if self._pydantic_model:
            try:
                validated_model = self._pydantic_model(**data)
                return validated_model.model_dump(
                    exclude_unset=True, exclude=self._exclude_fields
                )
            except ValidationError as e:
                raise serializers.ValidationError(str(e)) from e

        return data

    attrs["validate"] = validate

    # Decide the base classes
    bases: tuple[type, ...] = (serializers.Serializer,)
    if mixin_class:
        bases = (mixin_class, serializers.Serializer)

    # Create the class dynamically
    return type(serializer_name, bases, attrs)


# =============================================================================
# Pre-generated Pydantic-based Serializers
# Replaces the existing Serializers with Pydantic-based ones
# =============================================================================

# This section can provide simplified Serializers built from the Pydantic
# models in the settings module.
#
# Example:
# from baldur.settings import CircuitBreakerSettings
#
# CircuitBreakerPydanticSerializer = create_pydantic_serializer(
#     CircuitBreakerSettings,
#     "CircuitBreakerPydanticSerializer",
#     mixin_class=ApplyStrategyMixin,
# )
