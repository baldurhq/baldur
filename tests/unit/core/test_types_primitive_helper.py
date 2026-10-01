"""Unit tests for the shared primitive whitelist in ``baldur.core.types`` (#504 D8).

Two consumers depend on this module — ``@idempotent`` (cache-key fold-in,
``decorators/idempotent.py``) and ``@protected`` / ``@dlq_protect`` (context
auto-extract, ``protect.py``). The whitelist + annotation gate must agree
on the same exact set of "safe to capture" types so both decorators classify
arguments consistently.
"""

# NOTE: do NOT use ``from __future__ import annotations`` here. The
# annotation gate inspects real ``type`` objects; PEP 563 string annotations
# would defeat the boundary tests below.

import inspect
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import orjson
import pytest

from baldur.core.types import (
    ALLOWED_PRIMITIVE_TYPES,
    is_primitive_annotation,
    primitive_json_default,
)


class _Color(str, Enum):
    RED = "red"


class _Level(Enum):
    HIGH = 1


# One sample per ``ALLOWED_PRIMITIVE_TYPES`` member. The coverage test below
# fails when the whitelist gains a member with no sample here, so a new type
# cannot reach a context snapshot without the JSON stores being able to hold it.
_SAMPLES: dict[type, Any] = {
    int: 7,
    str: "s",
    bool: True,
    float: 1.5,
    Decimal: Decimal("9.99"),
    bytes: b"xy",
    UUID: UUID("12345678-1234-5678-1234-567812345678"),
    Enum: _Level.HIGH,
    type(None): None,
    datetime: datetime(2026, 10, 1, 12, 30),
    date: date(2026, 10, 1),
    time: time(12, 30),
    timedelta: timedelta(minutes=5),
}


# =============================================================================
# Contract — the whitelist tuple's exact membership (#504 D8)
# =============================================================================


class TestAllowedPrimitiveTypesContract:
    """``ALLOWED_PRIMITIVE_TYPES`` membership is a design contract — both
    ``@idempotent`` and ``@protected`` rely on this exact tuple."""

    def test_int_str_bool_float_are_allowed(self):
        for t in (int, str, bool, float):
            assert t in ALLOWED_PRIMITIVE_TYPES

    def test_decimal_bytes_uuid_enum_none_are_allowed(self):
        for t in (Decimal, bytes, UUID, Enum, type(None)):
            assert t in ALLOWED_PRIMITIVE_TYPES

    def test_datetime_date_time_timedelta_are_allowed(self):
        for t in (datetime, date, time, timedelta):
            assert t in ALLOWED_PRIMITIVE_TYPES

    def test_container_types_are_not_in_whitelist(self):
        """D8 explicitly excludes container types — structured payloads must go
        through ``context_from=Callable`` so the user owns redaction shape."""
        for t in (dict, list, tuple, set, frozenset):
            assert t not in ALLOWED_PRIMITIVE_TYPES


# =============================================================================
# Behavior — is_primitive_annotation dispatch (#504 D8)
# =============================================================================


class TestIsPrimitiveAnnotationBehavior:
    """Annotation gate behaviour — conservative on anything not a concrete
    ``type`` in the whitelist (or a subclass of one)."""

    @pytest.mark.parametrize(
        "annotation",
        [int, str, bool, float, Decimal, bytes, UUID, type(None)],
    )
    def test_returns_true_for_whitelisted_concrete_type(self, annotation):
        assert is_primitive_annotation(annotation) is True

    def test_returns_true_for_enum_subclass(self):
        # Enum is in the tuple → str-Enum subclass is via issubclass(.., Enum).
        assert is_primitive_annotation(_Color) is True

    def test_returns_false_for_inspect_empty(self):
        # The decoration-time loop hands the parameter's annotation through
        # unchanged, so ``inspect.Parameter.empty`` must NOT be classified as
        # primitive — otherwise un-annotated args would skip the runtime gate.
        assert is_primitive_annotation(inspect.Parameter.empty) is False

    def test_returns_false_for_unknown_type(self):
        class _NotPrimitive:
            pass

        assert is_primitive_annotation(_NotPrimitive) is False

    def test_returns_false_for_container_types(self):
        for ann in (dict, list, tuple, set):
            assert is_primitive_annotation(ann) is False

    def test_returns_false_for_generic_alias(self):
        # ``dict[str, int]`` / ``list[int]`` are not concrete ``type`` objects
        # — the function must conservatively return False so the runtime
        # ``isinstance`` gate handles them.
        assert is_primitive_annotation(dict[str, int]) is False
        assert is_primitive_annotation(list[int]) is False

    def test_returns_false_for_forward_ref_string(self):
        # PEP 563 / forward-ref annotations arrive as strings.
        assert is_primitive_annotation("int") is False

    def test_returns_false_for_typing_any(self):
        assert is_primitive_annotation(Any) is False


# =============================================================================
# Contract — primitive_json_default lets a JSON store hold every whitelisted type
# =============================================================================


class TestPrimitiveJsonDefaultContract:
    """A context snapshot keeps every whitelisted value as is, so both JSON
    encoders the DLQ stores use must accept each one through this hook."""

    def test_every_whitelisted_type_has_a_sample(self):
        assert set(_SAMPLES) == set(ALLOWED_PRIMITIVE_TYPES)

    @pytest.mark.parametrize(
        "value", list(_SAMPLES.values()), ids=[t.__name__ for t in _SAMPLES]
    )
    def test_stdlib_json_encodes_every_whitelisted_type(self, value):
        encoded = json.dumps({"v": value}, default=primitive_json_default)

        assert json.loads(encoded).keys() == {"v"}

    @pytest.mark.parametrize(
        "value", list(_SAMPLES.values()), ids=[t.__name__ for t in _SAMPLES]
    )
    def test_orjson_encodes_every_whitelisted_type(self, value):
        encoded = orjson.dumps({"v": value}, default=primitive_json_default)

        assert orjson.loads(encoded).keys() == {"v"}

    @pytest.mark.parametrize(
        "value", list(_SAMPLES.values()), ids=[t.__name__ for t in _SAMPLES]
    )
    def test_stdlib_json_and_orjson_read_back_the_same_value(self, value):
        via_stdlib = json.loads(
            json.dumps({"v": value}, default=primitive_json_default)
        )
        via_orjson = orjson.loads(
            orjson.dumps({"v": value}, default=primitive_json_default)
        )

        assert via_stdlib == via_orjson

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("9.99"), "9.99"),
            (
                UUID("12345678-1234-5678-1234-567812345678"),
                "12345678-1234-5678-1234-567812345678",
            ),
            (b"xy", "b'xy'"),
            (timedelta(minutes=5), "0:05:00"),
            (_Level.HIGH, 1),
            (_Color.RED, "red"),
            (datetime(2026, 10, 1, 12, 30), "2026-10-01T12:30:00"),
            (date(2026, 10, 1), "2026-10-01"),
            (time(12, 30), "12:30:00"),
        ],
    )
    def test_encoded_form_of_each_non_native_type(self, value, expected):
        assert primitive_json_default(value) == expected

    def test_a_type_outside_the_whitelist_still_raises(self):
        class Unsupported:
            pass

        with pytest.raises(TypeError, match="Unsupported"):
            primitive_json_default(Unsupported())
