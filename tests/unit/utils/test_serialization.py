"""Tests for utils/serialization.py — fast JSON serialization API.

Covers Phase 1 of document 364 (Serialization Hot-Path Optimization).

Test classes:
    TestFastDumpsContract        — return type, compact format, default= parameter
    TestFastDumpsStrBehavior     — str return, round-trip with fast_loads
    TestFastCanonicalDumpsContract — deterministic output, golden values, stdlib-only
    TestFastDumpsPrettyBehavior  — indented output
    TestFastLoadsBehavior        — bytes/str input, error compatibility
    TestOrjsonJsonEquivalence    — CI regression gate for orjson↔stdlib byte parity
    TestGoldenValues             — hash stability gate with hardcoded expected bytes
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import Enum
from unittest.mock import patch

import pytest

from baldur.utils.serialization import (
    FAST_JSON_AVAILABLE,
    fast_canonical_dumps,
    fast_dumps,
    fast_dumps_pretty,
    fast_dumps_str,
    fast_dumps_str_compact,
    fast_loads,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Color(str, Enum):
    RED = "red"
    GREEN = "green"


# ---------------------------------------------------------------------------
# TestFastDumps
# ---------------------------------------------------------------------------


class TestFastDumpsContract:
    """fast_dumps return-type and format contract verification."""

    def test_returns_bytes(self):
        """fast_dumps must return bytes."""
        result = fast_dumps({"a": 1})
        assert isinstance(result, bytes)

    def test_compact_output_no_whitespace(self):
        """Output must be compact — no spaces after separators."""
        result = fast_dumps({"key": "value", "num": 42})
        text = result.decode("utf-8")
        assert " " not in text

    def test_unicode_preserved(self):
        """Non-ASCII characters must be UTF-8 encoded, not escaped."""
        result = fast_dumps({"text": "한글 테스트"})
        assert "한글 테스트".encode() in result
        assert b"\\u" not in result

    def test_with_default_str_datetime(self):
        """default=str must handle datetime objects without raising."""
        dt = datetime(2026, 3, 20, 12, 0, 0, tzinfo=UTC)
        result = fast_dumps({"ts": dt}, default=str)
        assert isinstance(result, bytes)
        parsed = fast_loads(result)
        assert isinstance(parsed["ts"], str)

    def test_with_default_str_uuid(self):
        """default=str must handle UUID objects."""
        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        result = fast_dumps({"id": uid}, default=str)
        parsed = fast_loads(result)
        assert parsed["id"] == str(uid)

    def test_with_default_str_enum(self):
        """default=str must handle Enum objects."""
        result = fast_dumps({"color": _Color.RED}, default=str)
        parsed = fast_loads(result)
        assert "red" in parsed["color"].lower() or "Color.RED" in parsed["color"]

    def test_without_default_non_serializable_raises(self):
        """Non-serializable type without default must raise TypeError."""
        with pytest.raises(TypeError):
            fast_dumps({"obj": object()})

    def test_with_default_none_non_serializable_raises(self):
        """default=None (explicit) must still raise on non-serializable types."""
        with pytest.raises(TypeError):
            fast_dumps({"obj": object()}, default=None)


# ---------------------------------------------------------------------------
# TestFastDumpsStr
# ---------------------------------------------------------------------------


class TestFastDumpsStrBehavior:
    """fast_dumps_str return-type and round-trip behavior."""

    def test_returns_str(self):
        """fast_dumps_str must return str, not bytes."""
        result = fast_dumps_str({"a": 1})
        assert isinstance(result, str)

    def test_with_default_str(self):
        """default=str parameter is forwarded correctly."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        result = fast_dumps_str({"ts": dt}, default=str)
        assert isinstance(result, str)
        assert "2026" in result

    def test_round_trip_with_fast_loads(self):
        """fast_dumps_str output must be loadable by fast_loads."""
        original = {"key": "value", "num": 42, "list": [1, 2, 3]}
        serialized = fast_dumps_str(original)
        restored = fast_loads(serialized)
        assert restored == original


# ---------------------------------------------------------------------------
# TestFastDumpsStrCompact (#502 D6)
# ---------------------------------------------------------------------------


class TestFastDumpsStrCompactBehavior:
    """fast_dumps_str_compact default-drop behavior (#502 D6)."""

    def test_no_drops_when_no_keys_match_defaults(self):
        """All keys differ from defaults → output identical to fast_dumps_str."""
        data = {"a": 1, "b": "x"}
        defaults = {"a": 0, "b": ""}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        assert restored == data

    def test_all_default_valued_keys_are_dropped(self):
        """Keys whose value equals the default are omitted from the output."""
        data = {"a": 0, "b": "", "c": None, "d": {}}
        defaults = {"a": 0, "b": "", "c": None, "d": {}}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        assert restored == {}

    def test_partial_drops_keeps_non_default_keys(self):
        """Mixed defaults: only matching keys are dropped, others survive."""
        data = {"keep": "value", "drop_empty": "", "keep_int": 5, "drop_none": None}
        defaults = {"drop_empty": "", "drop_none": None, "keep_int": 0}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        assert restored == {"keep": "value", "keep_int": 5}

    def test_nested_dict_not_recursed(self):
        """Default-drop is top-level only; nested dicts pass through verbatim."""
        data = {"top": "", "nested": {"inner": ""}}
        defaults = {"top": "", "inner": ""}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        # `top` dropped (matches), `nested` kept (key not in defaults at top level)
        # `inner` inside the nested dict is NOT recursed even though it matches.
        assert restored == {"nested": {"inner": ""}}

    def test_key_absent_from_defaults_is_always_kept(self):
        """A key not appearing in the defaults map is never dropped."""
        data = {"unique": ""}
        defaults = {"other": ""}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        assert restored == {"unique": ""}

    def test_key_present_with_non_default_value_is_kept(self):
        """Key in defaults but value differs → keep the value."""
        data = {"x": "real_value"}
        defaults = {"x": ""}
        restored = fast_loads(fast_dumps_str_compact(data, defaults=defaults))
        assert restored == {"x": "real_value"}


# ---------------------------------------------------------------------------
# TestFastCanonicalDumps
# ---------------------------------------------------------------------------


class TestFastCanonicalDumpsContract:
    """fast_canonical_dumps deterministic output contract verification."""

    def test_returns_bytes(self):
        """fast_canonical_dumps must return bytes."""
        result = fast_canonical_dumps({"a": 1})
        assert isinstance(result, bytes)

    def test_sort_keys_deterministic(self):
        """Keys must be sorted alphabetically in output."""
        result = fast_canonical_dumps({"z": 1, "a": 2, "m": 3})
        text = result.decode("utf-8")
        keys = list(json.loads(text).keys())
        assert keys == ["a", "m", "z"]

    def test_compact_separators(self):
        """Output must use compact separators (no spaces)."""
        result = fast_canonical_dumps({"a": 1, "b": 2})
        text = result.decode("utf-8")
        assert text == '{"a":1,"b":2}'

    def test_with_default_str(self):
        """default=str must work for canonical serialization."""
        dt = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = fast_canonical_dumps({"ts": dt}, default=str)
        assert isinstance(result, bytes)

    def test_deterministic_across_calls(self):
        """Same input must produce identical bytes across multiple calls."""
        data = {"event": "test", "count": 42, "tags": ["a", "b"]}
        results = [fast_canonical_dumps(data) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_different_key_insertion_order_same_output(self):
        """Dicts with same keys in different insertion order must produce same bytes."""
        dict_a = {"z": 1, "a": 2, "m": 3}
        dict_b = {"a": 2, "m": 3, "z": 1}
        dict_c = {"m": 3, "z": 1, "a": 2}
        assert fast_canonical_dumps(dict_a) == fast_canonical_dumps(dict_b)
        assert fast_canonical_dumps(dict_b) == fast_canonical_dumps(dict_c)

    def test_output_matches_canonical_json_bytes(self):
        """Output must be byte-identical to canonical_json_bytes() for backward compat."""
        from baldur.audit.integrity.models import canonical_json_bytes

        data = {"event": "config_change", "ts": "2026-01-15T10:30:00Z", "seq": 42}
        assert fast_canonical_dumps(data, default=str) == canonical_json_bytes(data)

    def test_always_uses_stdlib_not_orjson(self):
        """fast_canonical_dumps must use stdlib json.dumps even when orjson is available."""
        if not FAST_JSON_AVAILABLE:
            pytest.skip("orjson not installed — stdlib-only test not meaningful")

        import orjson

        original_dumps = orjson.dumps
        call_tracker = {"called": False}

        def tracked_dumps(*args, **kwargs):
            call_tracker["called"] = True
            return original_dumps(*args, **kwargs)

        with patch.object(orjson, "dumps", side_effect=tracked_dumps):
            fast_canonical_dumps({"a": 1, "b": 2})

        assert call_tracker["called"] is False, (
            "fast_canonical_dumps must NOT call orjson.dumps"
        )


# ---------------------------------------------------------------------------
# TestFastDumpsPretty
# ---------------------------------------------------------------------------


class TestFastDumpsPrettyBehavior:
    """fast_dumps_pretty indented output behavior."""

    def test_returns_str(self):
        """fast_dumps_pretty must return str."""
        result = fast_dumps_pretty({"a": 1})
        assert isinstance(result, str)

    def test_indented_output(self):
        """Output must contain newlines and indentation."""
        result = fast_dumps_pretty({"key": "value"})
        assert "\n" in result
        assert "  " in result  # 2-space indent

    def test_with_default_str(self):
        """default=str parameter works for pretty printing."""
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        result = fast_dumps_pretty({"ts": dt}, default=str)
        assert isinstance(result, str)
        assert "2026" in result


# ---------------------------------------------------------------------------
# TestFastLoads
# ---------------------------------------------------------------------------


class TestFastLoadsBehavior:
    """fast_loads deserialization from bytes and str."""

    def test_from_bytes(self):
        """fast_loads must accept bytes input."""
        data = b'{"key":"value"}'
        result = fast_loads(data)
        assert result == {"key": "value"}

    def test_from_str(self):
        """fast_loads must accept str input."""
        data = '{"key":"value"}'
        result = fast_loads(data)
        assert result == {"key": "value"}

    def test_invalid_json_raises(self):
        """Invalid JSON must raise an exception."""
        with pytest.raises(Exception):
            fast_loads(b"not valid json")

    def test_orjson_decode_error_is_json_decode_error(self):
        """fast_loads error must be catchable as json.JSONDecodeError.

        Regression gate: 38 files use 'except json.JSONDecodeError'.
        orjson.JSONDecodeError inherits json.JSONDecodeError since orjson 3.x.
        """
        with pytest.raises(json.JSONDecodeError):
            fast_loads(b"not valid json")

    def test_round_trip_bytes(self):
        """fast_dumps → fast_loads round-trip preserves data."""
        original = {"nested": {"list": [1, 2, 3]}, "flag": True, "val": None}
        restored = fast_loads(fast_dumps(original))
        assert restored == original

    def test_round_trip_str(self):
        """fast_dumps_str → fast_loads round-trip preserves data."""
        original = {"unicode": "한글 테스트", "num": 3.14}
        restored = fast_loads(fast_dumps_str(original))
        assert restored == original


# ---------------------------------------------------------------------------
# TestOrjsonJsonEquivalence — CI regression gate
# ---------------------------------------------------------------------------


class TestOrjsonJsonEquivalenceBehavior:
    """Verify orjson and stdlib json produce identical bytes.

    CI regression gate — catches orjson version upgrade regressions.
    These tests compare serialization output between the two backends
    for types used in hot-path serialization.
    """

    @staticmethod
    def _stdlib_compact(obj: object) -> bytes:
        """Reference stdlib compact serialization."""
        return json.dumps(
            obj,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    @pytest.mark.skipif(
        not FAST_JSON_AVAILABLE,
        reason="orjson not installed",
    )
    def test_byte_equivalence_basic_types(self):
        """Basic types: int, str, bool, null, list, dict."""
        import orjson

        cases = [
            {"int": 42, "str": "hello", "bool": True, "null": None},
            {"list": [1, 2, 3], "nested": {"a": "b"}},
        ]
        for case in cases:
            assert orjson.dumps(case) == self._stdlib_compact(case), (
                f"Mismatch for: {case}"
            )

    @pytest.mark.skipif(
        not FAST_JSON_AVAILABLE,
        reason="orjson not installed",
    )
    def test_byte_equivalence_unicode(self):
        """Unicode strings must produce identical bytes."""
        import orjson

        case = {"unicode": "한글 테스트", "emoji": "✓"}
        assert orjson.dumps(case) == self._stdlib_compact(case)

    @pytest.mark.skipif(
        not FAST_JSON_AVAILABLE,
        reason="orjson not installed",
    )
    def test_byte_equivalence_nested_structures(self):
        """Deeply nested structures must match."""
        import orjson

        case = {"a": {"b": {"c": [1, {"d": True}]}}}
        assert orjson.dumps(case) == self._stdlib_compact(case)

    @pytest.mark.skipif(
        not FAST_JSON_AVAILABLE,
        reason="orjson not installed",
    )
    def test_byte_equivalence_large_int(self):
        """Large integers and edge values must match."""
        import orjson

        case = {"large": 2**53, "zero": 0, "neg": -1}
        assert orjson.dumps(case) == self._stdlib_compact(case)

    @pytest.mark.skipif(
        not FAST_JSON_AVAILABLE,
        reason="orjson not installed",
    )
    def test_byte_equivalence_float_edge_cases(self):
        """Float edge cases must produce identical bytes (orjson vs stdlib)."""
        import orjson

        case = {"float_edge": 0.1 + 0.2, "zero": 0.0, "neg": -1.5}
        assert orjson.dumps(case) == self._stdlib_compact(case)

    def test_canonical_stdlib_consistency(self):
        """fast_canonical_dumps must match manual stdlib json.dumps with same options."""
        data = {"z": 3, "a": 1, "m": 2}
        expected = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        assert fast_canonical_dumps(data) == expected


# ---------------------------------------------------------------------------
# TestGoldenValues — ultimate hash stability gate
# ---------------------------------------------------------------------------


class TestGoldenValuesContract:
    """Hardcoded golden value assertions for hash stability.

    These fixed input→output pairs protect against regressions in BOTH
    orjson and Python stdlib across version upgrades. If any golden value
    fails, hash chains and Merkle roots in production would be invalidated.
    """

    def test_canonical_wal_entry_golden_value(self):
        """WAL entry structure must produce exact expected canonical bytes."""
        wal_entry = {
            "seq": 42,
            "ts": "2026-01-15T10:30:00Z",
            "op": "config_change",
            "data": {"key": "max_retries", "old": 3, "new": 5},
        }
        expected = (
            b'{"data":{"key":"max_retries","new":5,"old":3},'
            b'"op":"config_change","seq":42,"ts":"2026-01-15T10:30:00Z"}'
        )
        assert fast_canonical_dumps(wal_entry) == expected

    def test_canonical_audit_entry_golden_value(self):
        """Audit entry structure must produce exact expected canonical bytes."""
        audit_entry = {
            "event_id": "evt-001",
            "action": "circuit_breaker.state_changed",
            "level": "INFO",
        }
        expected = (
            b'{"action":"circuit_breaker.state_changed",'
            b'"event_id":"evt-001","level":"INFO"}'
        )
        assert fast_canonical_dumps(audit_entry) == expected

    def test_canonical_integrity_hash_golden_value(self):
        """Integrity hash input must produce deterministic bytes for SHA-256."""
        import hashlib

        data = {"chain_id": "main", "seq": 100, "prev_hash": "abc123"}
        canonical = fast_canonical_dumps(data, default=str)
        digest = hashlib.sha256(canonical).hexdigest()
        # Golden hash value — if this changes, all stored integrity hashes break
        expected_canonical = b'{"chain_id":"main","prev_hash":"abc123","seq":100}'
        assert canonical == expected_canonical
        assert digest == hashlib.sha256(expected_canonical).hexdigest()

    def test_golden_values_match_current_canonical_json_bytes(self):
        """Cross-check: golden values must match canonical_json_bytes() output."""
        from baldur.audit.integrity.models import canonical_json_bytes

        test_data = {
            "event": "test",
            "level": 3,
            "tags": ["a", "b"],
        }
        assert fast_canonical_dumps(test_data, default=str) == canonical_json_bytes(
            test_data
        )

    def test_canonical_unicode_golden_value(self):
        """Unicode content must be preserved in canonical output, not ASCII-escaped."""
        data = {"msg": "서버 재시작", "code": 200}
        expected = b'{"code":200,"msg":"\xec\x84\x9c\xeb\xb2\x84 \xec\x9e\xac\xec\x8b\x9c\xec\x9e\x91"}'
        assert fast_canonical_dumps(data) == expected
