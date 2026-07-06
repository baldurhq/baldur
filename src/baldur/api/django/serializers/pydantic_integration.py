"""
Pydantic-DRF Serializer Integration Helper.

DRF Serializer 자동 생성 및 Pydantic 모델 통합.

이 모듈은 Pydantic Settings에서 DRF Serializer 필드를 자동으로 생성합니다.
- Pydantic 스키마 → DRF 필드 변환
- 검증은 Pydantic 모델에 위임
- 중복 코드 제거
"""

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
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
    Pydantic JSON Schema property를 DRF Field로 변환.

    Args:
        props: Pydantic 스키마의 property 정보
        field_name: 필드 이름
        required: 필수 여부

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
    Pydantic 모델에서 DRF Serializer 필드 딕셔너리 생성.

    Args:
        pydantic_model: Pydantic BaseModel 클래스
        exclude_fields: 제외할 필드 이름 set

    Returns:
        {field_name: DRF Field} 딕셔너리
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
    Pydantic 모델과 통합되는 DRF Serializer Mixin.

    사용법:
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
            # Pydantic 스키마에서 필드 자동 생성
            pydantic_fields = generate_serializer_fields_from_pydantic(
                self._pydantic_model,
                exclude_fields=self._exclude_fields,
            )

            # 기존 필드와 병합 (기존 필드 우선)
            for name, field in pydantic_fields.items():
                if name not in self.fields:
                    self.fields[name] = field

    def validate_with_pydantic(self, data: dict) -> dict:
        """
        Pydantic 모델로 검증 위임.

        Args:
            data: 검증할 데이터

        Returns:
            검증된 데이터 (Pydantic 모델에서 변환됨)

        Raises:
            serializers.ValidationError: 검증 실패 시
        """
        if not self._pydantic_model:
            return data

        try:
            # 부분 업데이트: 현재 설정과 병합
            validated_model = self._pydantic_model(**data)
            # 실제로 전달된 필드만 반환 (exclude_unset)
            return validated_model.model_dump(
                exclude_unset=True,
                exclude=self._exclude_fields,
            )
        except Exception as e:
            # Pydantic ValidationError → DRF ValidationError
            raise serializers.ValidationError(str(e)) from e

    def validate_with_pydantic_partial(
        self,
        data: dict,
        current_settings: BaseModel | None = None,
    ) -> dict:
        """
        부분 업데이트를 지원하는 Pydantic 검증.

        PATCH 요청 시 변경된 필드만 검증하고 현재 값과 병합합니다.

        Args:
            data: 변경할 필드만 포함된 dict
            current_settings: 현재 설정 인스턴스 (없으면 기본값 사용)

        Returns:
            병합된 검증 완료 데이터 (변경된 필드만)

        Raises:
            serializers.ValidationError: 검증 실패 시

        Example:
            # PATCH /api/v1/config/circuit-breaker/
            # Body: {"failure_threshold": 10}

            current = CircuitBreakerSettings()  # 현재 설정 로드
            changes = serializer.validate_with_pydantic_partial(
                data={"failure_threshold": 10},
                current_settings=current,
            )
            # changes = {"failure_threshold": 10}  # 변경된 것만
        """
        if not self._pydantic_model:
            return data

        try:
            if current_settings:
                # 현재 값과 병합 후 검증
                current_dict = current_settings.model_dump()
                current_dict.update(data)
                validated = self._pydantic_model.model_validate(current_dict)
            else:
                # 기본값과 병합 (전체 모델 생성 후 전달된 데이터만 오버라이드)
                defaults = self._pydantic_model()
                merged = defaults.model_dump()
                merged.update(data)
                validated = self._pydantic_model.model_validate(merged)

            # 실제로 변경된 필드만 반환
            return {k: v for k, v in validated.model_dump().items() if k in data}
        except Exception as e:
            raise serializers.ValidationError(str(e)) from e


def create_pydantic_serializer(
    pydantic_model: type[BaseModel],
    serializer_name: str,
    mixin_class: type | None = None,
    exclude_fields: set | None = None,
) -> type[serializers.Serializer]:
    """
    Pydantic 모델에서 DRF Serializer 클래스 동적 생성.

    Args:
        pydantic_model: Pydantic BaseModel 클래스
        serializer_name: 생성할 Serializer 클래스 이름
        mixin_class: 추가할 Mixin 클래스 (예: ApplyStrategyMixin)
        exclude_fields: 제외할 필드 이름 set

    Returns:
        동적으로 생성된 Serializer 클래스

    Example:
        >>> from baldur.settings import CircuitBreakerSettings
        >>> CBSerializer = create_pydantic_serializer(
        ...     CircuitBreakerSettings,
        ...     "CircuitBreakerSerializer",
        ...     mixin_class=ApplyStrategyMixin,
        ... )
    """
    exclude = exclude_fields or set()

    # 필드 생성
    fields = generate_serializer_fields_from_pydantic(
        pydantic_model, exclude_fields=exclude
    )

    # 클래스 속성
    attrs = {
        "_pydantic_model": pydantic_model,
        "_exclude_fields": exclude,
        **fields,
    }

    # validate 메서드 추가
    def validate(self, data):
        # Mixin의 validate 호출 (있는 경우)
        if hasattr(super(self.__class__, self), "validate"):
            data = super(self.__class__, self).validate(data)

        # Pydantic 모델로 검증
        if self._pydantic_model:
            try:
                validated_model = self._pydantic_model(**data)
                return validated_model.model_dump(
                    exclude_unset=True, exclude=self._exclude_fields
                )
            except Exception as e:
                raise serializers.ValidationError(str(e)) from e

        return data

    attrs["validate"] = validate

    # 베이스 클래스 결정
    bases: tuple[type, ...] = (serializers.Serializer,)
    if mixin_class:
        bases = (mixin_class, serializers.Serializer)

    # 동적 클래스 생성
    return type(serializer_name, bases, attrs)


# =============================================================================
# Pre-generated Pydantic-based Serializers
# 기존 Serializer를 Pydantic 기반으로 대체
# =============================================================================

# 이 섹션에서는 settings 모듈의 Pydantic 모델을 사용하여
# 간소화된 Serializer를 제공할 수 있습니다.
#
# 예시:
# from baldur.settings import CircuitBreakerSettings
#
# CircuitBreakerPydanticSerializer = create_pydantic_serializer(
#     CircuitBreakerSettings,
#     "CircuitBreakerPydanticSerializer",
#     mixin_class=ApplyStrategyMixin,
# )
