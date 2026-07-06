"""Unit tests for baldur.audit.performance.lua_registry (LuaScriptRegistry)."""

from unittest.mock import MagicMock, patch

import pytest

from baldur.audit.performance.lua_registry import LuaScriptRegistry


class TestLuaScriptRegistryContract:
    """LuaScriptRegistry design contract verification."""

    def test_max_reload_attempts_is_two(self):
        """MAX_RELOAD_ATTEMPTS contract value: 2."""
        assert LuaScriptRegistry.MAX_RELOAD_ATTEMPTS == 2


class TestLuaScriptRegistryBehavior:
    """LuaScriptRegistry behavior verification."""

    def _make_registry(self, redis_mock=None):
        redis = redis_mock or MagicMock()
        return LuaScriptRegistry(redis), redis

    def test_register_stores_script_body(self):
        """register() stores script body by name."""
        reg, _ = self._make_registry()
        reg.register("test_script", "return 1")
        assert "test_script" in reg._scripts
        assert reg._scripts["test_script"] == "return 1"

    def test_execute_unregistered_script_raises_key_error(self):
        """execute() with unregistered name raises KeyError."""
        reg, _ = self._make_registry()
        with pytest.raises(KeyError, match="not registered"):
            reg.execute("missing", [], [])

    def test_execute_lazy_loads_script_on_first_call(self):
        """First execute() triggers SCRIPT LOAD and caches SHA."""
        redis = MagicMock()
        redis.script_load.return_value = "abc123sha"
        redis.evalsha.return_value = "result"
        reg = LuaScriptRegistry(redis)
        reg.register("myscript", "return 1")

        result = reg.execute("myscript", ["{tag}:key"], ["arg1"])

        redis.script_load.assert_called_once_with("return 1")
        redis.evalsha.assert_called_once_with("abc123sha", 1, "{tag}:key", "arg1")
        assert result == "result"

    def test_execute_uses_cached_sha_on_second_call(self):
        """Second execute() uses cached SHA without SCRIPT LOAD."""
        redis = MagicMock()
        redis.script_load.return_value = "sha1"
        redis.evalsha.return_value = "ok"
        reg = LuaScriptRegistry(redis)
        reg.register("s", "return 1")

        reg.execute("s", ["{t}:k1"], [])
        reg.execute("s", ["{t}:k2"], [])

        assert redis.script_load.call_count == 1
        assert redis.evalsha.call_count == 2

    def test_execute_noscript_recovery_falls_back_to_eval(self):
        """NOSCRIPT error triggers recovery: clears SHA cache and falls back to eval()."""
        from redis.exceptions import NoScriptError

        redis_mock = MagicMock()
        redis_mock.evalsha.side_effect = NoScriptError("NOSCRIPT")
        redis_mock.eval.return_value = "eval_fallback"

        reg = LuaScriptRegistry(redis_mock)
        reg.register("s", "return 1")
        reg._sha_cache["s"] = "stale_sha"

        result = reg.execute("s", ["{t}:k"], ["arg"])

        # After MAX_RELOAD_ATTEMPTS NoScriptError, falls back to raw eval
        redis_mock.eval.assert_called_once_with("return 1", 1, "{t}:k", "arg")
        assert result == "eval_fallback"
        # SHA cache should be cleared
        assert "s" not in reg._sha_cache

    def test_validate_same_slot_accepts_same_hash_tag(self):
        """Keys with same {hash_tag} pass validation."""
        LuaScriptRegistry._validate_same_slot(
            ["{audit}:key1", "{audit}:key2", "{audit}:key3"]
        )

    def test_validate_same_slot_rejects_different_tags(self):
        """Keys with different hash tags raise ValueError."""
        with pytest.raises(ValueError, match="multiple hash slots"):
            LuaScriptRegistry._validate_same_slot(["{audit}:key1", "{other}:key2"])

    def test_validate_same_slot_untagged_keys_are_distinct(self):
        """Untagged keys are treated as having their full name as tag."""
        with pytest.raises(ValueError, match="multiple hash slots"):
            LuaScriptRegistry._validate_same_slot(["key1", "key2"])

    def test_extract_hash_tag_with_braces(self):
        """Extracts content between first pair of braces."""
        assert LuaScriptRegistry._extract_hash_tag("{audit}:buffer:1") == "audit"

    def test_extract_hash_tag_without_braces(self):
        """Returns full key when no braces present."""
        assert LuaScriptRegistry._extract_hash_tag("plain_key") == "plain_key"

    def test_extract_hash_tag_empty_braces(self):
        """Empty braces {} returns full key (no valid tag)."""
        assert LuaScriptRegistry._extract_hash_tag("{}:key") == "{}:key"

    def test_execute_single_key_skips_slot_validation(self):
        """Single-key execute() does not call slot validation."""
        redis = MagicMock()
        redis.script_load.return_value = "sha"
        redis.evalsha.return_value = "ok"
        reg = LuaScriptRegistry(redis)
        reg.register("s", "return 1")

        with patch.object(
            LuaScriptRegistry, "_validate_same_slot", autospec=True
        ) as mock_validate:
            reg.execute("s", ["single_key"], [])
            mock_validate.assert_not_called()

    def test_execute_multiple_keys_triggers_slot_validation(self):
        """Multi-key execute() triggers slot validation."""
        redis = MagicMock()
        redis.script_load.return_value = "sha"
        redis.evalsha.return_value = "ok"
        reg = LuaScriptRegistry(redis)
        reg.register("s", "return 1")

        with patch.object(
            LuaScriptRegistry, "_validate_same_slot", autospec=True
        ) as mock_validate:
            reg.execute("s", ["{t}:a", "{t}:b"], [])
            mock_validate.assert_called_once()
