"""Unit tests for ``RedisCacheAdapter`` acquire-op atomicity + outage surfacing (673).

Two 673 behaviors, mocked against the Redis client / Lua registry (the real
``EVAL`` semantics are covered by the ``requires_redis`` integration lane):

- **cas_takeover call-shape (G1):** the new atomic takeover invokes the
  registered ``idempotency_cas_takeover`` Lua script with
  ``[serialized_new, stale_before, ttl_ms]`` and maps ``1 → True`` / ``0 → False``.
- **Un-swallow (G2):** ``setnx`` and ``cas_takeover`` are dedup-gate-only acquire
  ops, so a backend I/O error is re-raised as ``AdapterConnectionError`` (never
  swallowed to ``False``) — the signal the idempotency gate's fail-open /
  ``IdempotencyUnavailableError`` path keys on. No redis-py type leaks past the
  adapter boundary.

Verification techniques (UNIT_TEST_GUIDELINES §8):
- §8.5 Dependency interaction (mock ``LuaScriptRegistry.execute`` / client).
- §8.2 Exception/edge cases (backend error → domain exception).
- §8.10 Serialization (timedelta → ms; new_record → orjson bytes).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from baldur.adapters.cache.redis_adapter import (
    LUA_CAS_TAKEOVER,
    RedisCacheAdapter,
)
from baldur.core.exceptions import AdapterConnectionError
from baldur.utils.serialization import fast_dumps


@pytest.fixture
def adapter() -> RedisCacheAdapter:
    """RedisCacheAdapter with an injected MagicMock Redis client."""
    client = MagicMock()
    return RedisCacheAdapter(client=client, key_prefix="take-test:")


@pytest.fixture
def mock_registry(adapter, monkeypatch) -> MagicMock:
    """Replace the lazy LuaScriptRegistry with a MagicMock."""
    fake_registry = MagicMock()
    monkeypatch.setattr(adapter, "_lua_registry", fake_registry)
    return fake_registry


_NEW = {"status": "executing", "started_at": 123.0, "retry_count": 1}


class TestRedisCasTakeoverCallShapeBehavior:
    """``cas_takeover`` invokes the registered takeover Lua script by name."""

    def test_invokes_registry_with_takeover_script_name(self, adapter, mock_registry):
        mock_registry.execute.return_value = 1

        adapter.cas_takeover("k", _NEW, stale_before=100.0)

        call_kwargs = mock_registry.execute.call_args.kwargs
        called_name = (
            call_kwargs["name"]
            if "name" in call_kwargs
            else mock_registry.execute.call_args.args[0]
        )
        assert called_name == "idempotency_cas_takeover"

    def test_passes_full_prefixed_key_in_keys(self, adapter, mock_registry):
        """KEYS[1] is the prefixed storage key (``_make_key`` applied)."""
        mock_registry.execute.return_value = 1

        adapter.cas_takeover("order:abc", _NEW, stale_before=100.0)

        assert mock_registry.execute.call_args.kwargs["keys"] == ["take-test:order:abc"]

    def test_argv_layout_is_serialized_stale_before_ttl(self, adapter, mock_registry):
        """ARGV layout is [serialized_new_record, stale_before, ttl_ms]."""
        mock_registry.execute.return_value = 1
        ttl = timedelta(seconds=30)

        adapter.cas_takeover("k", _NEW, stale_before=1750.5, ttl=ttl)

        args = mock_registry.execute.call_args.kwargs["args"]
        assert len(args) == 3
        assert args[0] == fast_dumps(_NEW, default=str)
        assert args[1] == 1750.5
        assert args[2] == 30_000

    def test_returns_true_when_lua_returns_1(self, adapter, mock_registry):
        mock_registry.execute.return_value = 1
        assert adapter.cas_takeover("k", _NEW, stale_before=1.0) is True

    def test_returns_false_when_lua_returns_0(self, adapter, mock_registry):
        """Lua 0 (not takeable / missing / fresh) → False, no exception."""
        mock_registry.execute.return_value = 0
        assert adapter.cas_takeover("k", _NEW, stale_before=1.0) is False

    def test_ttl_none_serializes_to_zero(self, adapter, mock_registry):
        mock_registry.execute.return_value = 1

        adapter.cas_takeover("k", _NEW, stale_before=1.0, ttl=None)

        assert mock_registry.execute.call_args.kwargs["args"][2] == 0

    def test_ttl_sub_second_uses_millisecond_precision(self, adapter, mock_registry):
        mock_registry.execute.return_value = 1

        adapter.cas_takeover(
            "k", _NEW, stale_before=1.0, ttl=timedelta(milliseconds=250)
        )

        assert mock_registry.execute.call_args.kwargs["args"][2] == 250


class TestRedisCasTakeoverUnswallowBehavior:
    """A backend I/O error surfaces as ``AdapterConnectionError`` (G2)."""

    def test_cas_takeover_backend_error_raises_adapter_connection_error(
        self, adapter, mock_registry
    ):
        mock_registry.execute.side_effect = ConnectionError("redis down")

        with pytest.raises(AdapterConnectionError, match="cas_takeover failed"):
            adapter.cas_takeover("k", _NEW, stale_before=1.0)

    def test_cas_takeover_wraps_cause_without_leaking_backend_type(
        self, adapter, mock_registry
    ):
        """The raw backend error is chained via ``from`` — no redis-py type leaks."""
        cause = ConnectionError("redis down")
        mock_registry.execute.side_effect = cause

        with pytest.raises(AdapterConnectionError) as exc_info:
            adapter.cas_takeover("k", _NEW, stale_before=1.0)

        assert exc_info.value.__cause__ is cause


class TestRedisSetnxUnswallowBehavior:
    """``setnx`` un-swallows a cache I/O error (673 G2) — the dominant full-outage
    case is caught at the first acquire op before ``get`` / ``cas_takeover``."""

    def test_setnx_no_ttl_backend_error_raises_adapter_connection_error(self, adapter):
        adapter._redis.setnx.side_effect = ConnectionError("redis down")

        with pytest.raises(AdapterConnectionError, match="setnx failed"):
            adapter.setnx("k", {"status": "executing"})

    def test_setnx_with_ttl_backend_error_raises_adapter_connection_error(
        self, adapter
    ):
        """The ttl branch (``SET NX EX``) surfaces the same domain exception."""
        adapter._redis.set.side_effect = ConnectionError("redis down")

        with pytest.raises(AdapterConnectionError, match="setnx failed"):
            adapter.setnx("k", {"status": "executing"}, ttl=timedelta(seconds=30))

    def test_setnx_wraps_cause_without_leaking_backend_type(self, adapter):
        cause = ConnectionError("redis down")
        adapter._redis.setnx.side_effect = cause

        with pytest.raises(AdapterConnectionError) as exc_info:
            adapter.setnx("k", {"status": "executing"})

        assert exc_info.value.__cause__ is cause


class TestRedisCasTakeoverRegistryWiringBehavior:
    """``_get_lua_registry`` registers the takeover script body once."""

    def test_registry_registers_takeover_script_body(self, adapter):
        adapter._redis.script_load.return_value = "deadbeef"
        adapter._redis.evalsha.return_value = 1

        adapter.cas_takeover("k", _NEW, stale_before=1.0)

        registered = adapter._lua_registry._scripts["idempotency_cas_takeover"]
        assert registered == LUA_CAS_TAKEOVER


class TestLuaCasTakeoverBodyContract:
    """Regression guard on the takeover Lua body (673 D1)."""

    def test_body_decodes_record_and_checks_status_and_started_at(self):
        """The script must ``cjson.decode`` the record and branch on both
        ``status`` and ``started_at`` — the started_at re-check is what makes a
        fresh post-takeover claim ineligible for a second taker (single-winner)."""
        assert "cjson.decode" in LUA_CAS_TAKEOVER
        assert "status" in LUA_CAS_TAKEOVER
        assert "started_at" in LUA_CAS_TAKEOVER

    def test_body_uses_stale_before_arg_not_redis_time(self):
        """Staleness is app-computed (ARGV[2]), never ``redis TIME`` — the
        comparison must stay app-clock vs app-clock (D1)."""
        assert "ARGV[2]" in LUA_CAS_TAKEOVER
        assert "TIME" not in LUA_CAS_TAKEOVER

    def test_body_writes_with_set_px_and_returns_one_zero(self):
        assert "'PX'" in LUA_CAS_TAKEOVER or '"PX"' in LUA_CAS_TAKEOVER
        assert "SET" in LUA_CAS_TAKEOVER
        assert "return 1" in LUA_CAS_TAKEOVER
        assert "return 0" in LUA_CAS_TAKEOVER
