"""Unified Lua Script Registry for Redis.

Centralized Lua script management with lazy-load, NOSCRIPT auto-recovery,
and hash slot validation for Redis Cluster compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_lua_metrics():
    """Lazy-init Prometheus metrics for Lua script operations."""
    try:
        from baldur.metrics.registry import get_or_create_counter

        if not hasattr(_get_lua_metrics, "_load"):
            _get_lua_metrics._load = get_or_create_counter(
                "baldur_lua_script_load_total",
                "Total Lua SCRIPT LOAD operations",
                [],
            )
            _get_lua_metrics._noscript = get_or_create_counter(
                "baldur_lua_noscript_total",
                "Total NOSCRIPT errors recovered",
                [],
            )
        return _get_lua_metrics._load, _get_lua_metrics._noscript
    except ImportError:
        return None, None


class LuaScriptRegistry:
    """Unified Lua script manager.

    - Lazy-load: SCRIPT LOAD on first execute()
    - NOSCRIPT auto-recovery: re-register on evalsha failure (max 2 attempts)
    - Metrics: script_load count, NOSCRIPT count exposed to Prometheus
    """

    MAX_RELOAD_ATTEMPTS = 2

    def __init__(self, redis_client):
        self._redis = redis_client
        self._scripts: dict[str, str] = {}  # name -> script body
        self._sha_cache: dict[str, str] = {}  # name -> SHA

    def register(self, name: str, script_body: str) -> None:
        """Register a Lua script by name."""
        self._scripts[name] = script_body

    def execute(self, name: str, keys: list, args: list) -> Any:
        """Execute a registered Lua script with NOSCRIPT auto-recovery."""
        if name not in self._scripts:
            raise KeyError(f"Lua script '{name}' not registered")

        if len(keys) > 1:
            self._validate_same_slot(keys)

        try:
            from redis.exceptions import NoScriptError
        except ImportError:
            NoScriptError = type(None)  # type: ignore[assignment,misc]

        for attempt in range(self.MAX_RELOAD_ATTEMPTS):
            sha = self._sha_cache.get(name)
            try:
                if sha:
                    return self._redis.evalsha(sha, len(keys), *keys, *args)
                return self._load_and_execute(name, keys, args)
            except (NoScriptError, Exception) as exc:
                if not isinstance(exc, NoScriptError) and "NOSCRIPT" not in str(exc):
                    raise
                self._sha_cache.pop(name, None)
                load_counter, noscript_counter = _get_lua_metrics()
                if noscript_counter:
                    noscript_counter.inc()
                logger.debug(
                    "lua_registry.noscript_recovery",
                    extra={
                        "script": name,
                        "attempt": attempt + 1,
                    },
                )
                if attempt == self.MAX_RELOAD_ATTEMPTS - 1:
                    return self._redis.eval(
                        self._scripts[name], len(keys), *keys, *args
                    )

        raise RuntimeError(
            f"Lua script '{name}' failed after {self.MAX_RELOAD_ATTEMPTS} attempts"
        )

    def _load_and_execute(self, name: str, keys: list, args: list) -> Any:
        body = self._scripts[name]
        sha = self._redis.script_load(body)
        self._sha_cache[name] = sha
        load_counter, _ = _get_lua_metrics()
        if load_counter:
            load_counter.inc()
        return self._redis.evalsha(sha, len(keys), *keys, *args)

    @staticmethod
    def _validate_same_slot(keys: list[str]) -> None:
        """Validate that all keys map to the same hash slot (Redis Cluster safety)."""
        tags = {LuaScriptRegistry._extract_hash_tag(k) for k in keys}
        if len(tags) > 1:
            raise ValueError(
                f"Keys span multiple hash slots: {keys}. "
                f"Use {{hash_tag}} to group related keys."
            )

    @staticmethod
    def _extract_hash_tag(key: str) -> str:
        start = key.find("{")
        end = key.find("}", start + 1)
        if start != -1 and end != -1 and end > start + 1:
            return key[start + 1 : end]
        return key
