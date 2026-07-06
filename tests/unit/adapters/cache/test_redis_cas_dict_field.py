"""Unit tests for ``RedisCacheAdapter.cas_dict_field`` (491 D3).

The Lua-semantics correctness path (``cjson.decode`` roundtrip,
``EVALSHA`` + ``NOSCRIPT`` auto-recovery, concurrent CAS race) is
covered by an integration test against a real Redis. This file mocks
the Redis client and asserts the adapter's call shape against
``LuaScriptRegistry.execute``:

- script name (``idempotency_cas_dict_field``)
- KEYS / ARGV layout (full prefixed key, field, expected, serialized
  new value, ttl_ms)
- return-value mapping (1 → True, 0 → False)
- TTL serialization (timedelta → integer milliseconds; ttl=None → 0)

Verification techniques (per UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock ``LuaScriptRegistry.execute``).
- §8.1 Boundary analysis (return-value mapping 1/0).
- §8.10 Serialization (timedelta → ms; new_value → orjson bytes).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.redis_adapter import (
    LUA_CAS_DICT_FIELD,
    RedisCacheAdapter,
)
from baldur.utils.serialization import fast_dumps


@pytest.fixture
def adapter() -> RedisCacheAdapter:
    """RedisCacheAdapter with an injected MagicMock Redis client."""
    client = MagicMock()
    return RedisCacheAdapter(client=client, key_prefix="cas-test:")


@pytest.fixture
def mock_registry(adapter, monkeypatch) -> MagicMock:
    """Replace the lazy LuaScriptRegistry with a MagicMock."""
    fake_registry = MagicMock()
    monkeypatch.setattr(adapter, "_lua_registry", fake_registry)
    return fake_registry


class TestRedisCasDictFieldBehavior:
    """``cas_dict_field`` invokes the registered Lua script via the registry."""

    def test_invokes_registry_with_idempotency_script_name(
        self, adapter, mock_registry
    ):
        """Script name passed to the registry MUST match the registration key."""
        mock_registry.execute.return_value = 1

        adapter.cas_dict_field("key", "status", "executing", {"status": "completed"})

        called_name = (
            mock_registry.execute.call_args.kwargs["name"]
            if "name" in mock_registry.execute.call_args.kwargs
            else mock_registry.execute.call_args.args[0]
        )
        assert called_name == "idempotency_cas_dict_field"

    def test_passes_full_prefixed_key_in_keys(self, adapter, mock_registry):
        """KEYS[1] is the prefixed storage key (``cache._make_key`` applied)."""
        mock_registry.execute.return_value = 1

        adapter.cas_dict_field(
            "order:abc", "status", "executing", {"status": "completed"}
        )

        call_kwargs = mock_registry.execute.call_args.kwargs
        assert call_kwargs["keys"] == ["cas-test:order:abc"]

    def test_argv_layout_is_field_expected_serialized_ttl(self, adapter, mock_registry):
        """ARGV layout is [field, expected, serialized_new_value, ttl_ms]."""
        mock_registry.execute.return_value = 1
        new_value = {"status": "completed", "result": {"ok": True}}
        ttl = timedelta(seconds=30)

        adapter.cas_dict_field("k", "status", "executing", new_value, ttl=ttl)

        call_kwargs = mock_registry.execute.call_args.kwargs
        args = call_kwargs["args"]
        assert len(args) == 4
        assert args[0] == "status"
        assert args[1] == "executing"
        assert args[2] == fast_dumps(new_value, default=str)
        assert args[3] == 30_000

    def test_returns_true_when_lua_returns_1(self, adapter, mock_registry):
        """Lua return 1 → adapter returns True."""
        mock_registry.execute.return_value = 1

        ok = adapter.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is True

    def test_returns_false_when_lua_returns_0(self, adapter, mock_registry):
        """Lua return 0 (mismatch / missing) → adapter returns False."""
        mock_registry.execute.return_value = 0

        ok = adapter.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert ok is False

    def test_ttl_none_serializes_to_zero(self, adapter, mock_registry):
        """ttl=None → ttl_ms=0 (Lua branch falls through to ``SET`` without ``PX``)."""
        mock_registry.execute.return_value = 1

        adapter.cas_dict_field(
            "k", "status", "executing", {"status": "completed"}, ttl=None
        )

        args = mock_registry.execute.call_args.kwargs["args"]
        assert args[3] == 0

    def test_ttl_sub_second_uses_millisecond_precision(self, adapter, mock_registry):
        """sub-second timedelta → integer milliseconds (preserves PX precision)."""
        mock_registry.execute.return_value = 1

        adapter.cas_dict_field(
            "k",
            "status",
            "executing",
            {"status": "completed"},
            ttl=timedelta(milliseconds=250),
        )

        args = mock_registry.execute.call_args.kwargs["args"]
        assert args[3] == 250


class TestRedisCasDictFieldRegistryWiringBehavior:
    """``_get_lua_registry`` lazy-init wires the script body once."""

    def test_lazy_registry_is_built_on_first_cas_call(self, adapter):
        """``_lua_registry`` is None until first cas_dict_field call."""
        assert adapter._lua_registry is None

        # Stub script_load + evalsha so the registry's first execute() succeeds.
        adapter._redis.script_load.return_value = "deadbeef"
        adapter._redis.evalsha.return_value = 1

        adapter.cas_dict_field("k", "status", "executing", {"status": "completed"})

        assert adapter._lua_registry is not None

    def test_registry_registers_idempotency_script_body(self, adapter):
        """Registry holds the production ``LUA_CAS_DICT_FIELD`` body verbatim."""
        adapter._redis.script_load.return_value = "deadbeef"
        adapter._redis.evalsha.return_value = 1

        adapter.cas_dict_field("k", "status", "executing", {"status": "completed"})

        registered = adapter._lua_registry._scripts["idempotency_cas_dict_field"]
        assert registered == LUA_CAS_DICT_FIELD

    def test_lua_script_body_uses_cjson_decode_and_set_px(self):
        """``LUA_CAS_DICT_FIELD`` body must call ``cjson.decode`` + ``SET PX`` (491 D3)."""
        # Minimal regression guard against accidental script body edits.
        assert "cjson.decode" in LUA_CAS_DICT_FIELD
        assert "'PX'" in LUA_CAS_DICT_FIELD or '"PX"' in LUA_CAS_DICT_FIELD
        assert "GET" in LUA_CAS_DICT_FIELD
        assert "SET" in LUA_CAS_DICT_FIELD
