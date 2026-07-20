"""
Atomic State Query.

Queries Global + Regional state in a single round trip via a Lua script and
resolves precedence atomically.

Network round trips: 2 -> 1 (50% reduction)
Race conditions: eliminated at the source
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from baldur.utils.serialization import fast_loads

logger = structlog.get_logger()


# =============================================================================
# Lua Script: atomic Global + Regional state query and precedence resolution
# =============================================================================

ATOMIC_STATE_QUERY_SCRIPT = """
-- KEYS[1]: global emergency state key (baldur:governance:emergency_state)
-- KEYS[2]: regional emergency state key (baldur:{namespace}:governance:emergency_state)
-- ARGV[1]: precedence level (0=AUTO, 1=MANUAL, 2=ADMIN_OVERRIDE, 3=KILL_SWITCH)

local global_data = redis.call("GET", KEYS[1])
local regional_data = redis.call("GET", KEYS[2])

-- JSON parsing
local global_state = nil
local regional_state = nil

if global_data and global_data ~= false then
    global_state = cjson.decode(global_data)
end

if regional_data and regional_data ~= false then
    regional_state = cjson.decode(regional_data)
end

-- Default values
if not global_state then
    global_state = {
        namespace = "global",
        scope = "global",
        governance_mode = "NORMAL",
        is_active = false,
        emergency_level = "normal"
    }
end

if not regional_state then
    -- namespace extraction: baldur:{ns}:governance:emergency_state
    local ns = KEYS[2]:match("baldur:([^:]+):governance")
    regional_state = {
        namespace = ns or "unknown",
        scope = "regional",
        governance_mode = "NORMAL",
        is_active = false,
        emergency_level = "normal"
    }
end

local precedence = tonumber(ARGV[1]) or 0

-- Priority 1: Admin Override (precedence >= 2)
-- When the operator explicitly requests an override, use the Regional state
if precedence >= 2 then
    return {
        cjson.encode(regional_state),
        "ADMIN_OVERRIDE",
        "Admin override active, using regional state"
    }
end

-- is_active check: active when emergency_level is not 0
-- ScopedEmergencyState.to_dict() does not store is_active, so use emergency_level
local function is_active(state)
    local level = state.emergency_level
    if level == nil then
        return state.is_active == true
    end
    return level ~= "normal"
end

-- Priority 2: Safety-Max (choose the stricter of the two states)
local global_is_strict = is_active(global_state) and
                         (global_state.governance_mode == "STRICT")
local regional_is_strict = is_active(regional_state) and
                           (regional_state.governance_mode == "STRICT")

if global_is_strict and regional_is_strict then
    -- Both STRICT: Global takes priority (broader scope)
    return {
        cjson.encode(global_state),
        "GLOBAL_OVERRIDE",
        "Both Global and Regional STRICT, using Global state"
    }
elseif global_is_strict then
    -- Only Global is STRICT
    return {
        cjson.encode(global_state),
        "GLOBAL_OVERRIDE",
        "Global STRICT overrides regional " .. (regional_state.namespace or "unknown")
    }
elseif regional_is_strict then
    -- Only Regional is STRICT
    return {
        cjson.encode(regional_state),
        "REGIONAL_STRICT",
        "Regional STRICT active"
    }
else
    -- Both NORMAL: return Regional (local state takes priority)
    return {
        cjson.encode(regional_state),
        "REGIONAL_DEFAULT",
        "Both states NORMAL, using regional"
    }
end
"""


class AtomicStateQuery:
    """
    Atomic state query.

    Queries Global + Regional state in a single round trip via a Lua script and
    resolves precedence atomically.

    Benefits:
    - 50% fewer network round trips (2 -> 1)
    - Race conditions eliminated at the source
    - Precedence logic handled server-side

    Precedence Levels:
    - AUTO (0): automatic mode - Safety-Max applied
    - MANUAL (1): manual mode - Safety-Max applied
    - ADMIN_OVERRIDE (2): admin override - ignore Global
    - KILL_SWITCH (3): kill switch - ignore everything

    Code reference:
        coordination/atomic_transition.py (Lua script pattern)

    Usage:
        query = AtomicStateQuery(redis_client)
        state, decision_type, reason = query.query_effective_state("seoul")
    """

    # Precedence level mapping
    PRECEDENCE_LEVELS = {
        "AUTO": 0,
        "MANUAL": 1,
        "ADMIN_OVERRIDE": 2,
        "KILL_SWITCH": 3,
    }

    def __init__(
        self,
        redis_client: Any,
        key_prefix: str = "baldur",
    ):
        """
        Initialize AtomicStateQuery.

        Args:
            redis_client: Redis client (redis-py compatible)
            key_prefix: Redis key prefix (default: "baldur")
        """
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._script_sha: str | None = None

    def _get_global_key(self) -> str:
        """Return the Redis key for the Global state."""
        return f"{self._key_prefix}:governance:emergency_state"

    def _get_regional_key(self, namespace: str) -> str:
        """Return the Redis key for the Regional state."""
        return f"{self._key_prefix}:{namespace}:governance:emergency_state"

    @staticmethod
    def _decode_raw(raw: Any) -> dict[str, Any] | None:
        """Decode raw Redis GET result to dict, or None."""
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return fast_loads(raw)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_active(state: dict[str, Any]) -> bool:
        """Check if emergency state is active."""
        level = state.get("emergency_level")
        if level is not None:
            return level != 0
        return bool(state.get("is_active", False))

    def _resolve_precedence(
        self,
        global_raw: Any,
        regional_raw: Any,
        namespace: str,
        precedence_level: int,
    ) -> tuple[dict[str, Any], str, str]:
        """Resolve precedence between global and regional states (Python-side)."""
        global_state = self._decode_raw(global_raw)
        regional_state = self._decode_raw(regional_raw)

        if global_state is None:
            global_state = {
                "namespace": "global",
                "scope": "global",
                "governance_mode": "NORMAL",
                "is_active": False,
                "emergency_level": "normal",
            }

        if regional_state is None:
            regional_state = {
                "namespace": namespace,
                "scope": "regional",
                "governance_mode": "NORMAL",
                "is_active": False,
                "emergency_level": "normal",
            }

        # 1: Admin Override (precedence >= 2)
        if precedence_level >= 2:
            return (
                regional_state,
                "ADMIN_OVERRIDE",
                "Admin override active, using regional state",
            )

        # 2: Safety-Max
        global_is_strict = (
            self._is_active(global_state)
            and global_state.get("governance_mode") == "STRICT"
        )
        regional_is_strict = (
            self._is_active(regional_state)
            and regional_state.get("governance_mode") == "STRICT"
        )

        if global_is_strict and regional_is_strict:
            return (
                global_state,
                "GLOBAL_OVERRIDE",
                "Both Global and Regional STRICT, using Global state",
            )
        if global_is_strict:
            ns_name = regional_state.get("namespace", "unknown")
            return (
                global_state,
                "GLOBAL_OVERRIDE",
                f"Global STRICT overrides regional {ns_name}",
            )
        if regional_is_strict:
            return (
                regional_state,
                "REGIONAL_STRICT",
                "Regional STRICT active",
            )
        return (
            regional_state,
            "REGIONAL_DEFAULT",
            "Both states NORMAL, using regional",
        )

    @staticmethod
    def _decode_result_item(raw: Any) -> str:
        """Decode a bytes or str result item to str."""
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw) if raw is not None else ""

    def _parse_result(
        self,
        result: list,
    ) -> tuple[dict[str, Any], str, str]:
        """Parse Lua script / eval result into (state_dict, decision_type, reason)."""
        state_raw, decision_type_raw, reason_raw = result

        state_str = self._decode_result_item(state_raw)
        decision_type = self._decode_result_item(decision_type_raw)
        reason = self._decode_result_item(reason_raw)

        try:
            state = fast_loads(state_str)
        except (ValueError, TypeError):
            state = {}

        return (state, decision_type, reason)

    def query_effective_state(
        self,
        namespace: str,
        precedence: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        """
        Query the effective state (Lua script based).

        Atomically queries the Global and Regional states via a Lua script and
        determines the effective state based on precedence.

        Args:
            namespace: target namespace (e.g. "seoul", "tokyo")
            precedence: command precedence
                ("AUTO", "MANUAL", "ADMIN_OVERRIDE", "KILL_SWITCH")

        Returns:
            Tuple of:
            - effective_state: effective state dictionary
            - decision_type: decision type
                ("GLOBAL_OVERRIDE", "ADMIN_OVERRIDE", "REGIONAL_STRICT", "REGIONAL_DEFAULT")
            - decision_reason: decision reason (for audit/logging)

        Example:
            state, decision_type, reason = query.query_effective_state("seoul")
            # state = {"namespace": "global", "governance_mode": "STRICT", ...}
            # decision_type = "GLOBAL_OVERRIDE"
            # reason = "Global STRICT overrides regional seoul"
        """
        global_key = self._get_global_key()
        regional_key = self._get_regional_key(namespace)
        precedence_level = self.PRECEDENCE_LEVELS.get(precedence or "AUTO", 0)

        try:
            result = self._redis.eval(
                ATOMIC_STATE_QUERY_SCRIPT,
                2,
                global_key,
                regional_key,
                str(precedence_level),
            )

            state, decision_type, decision_reason = self._parse_result(result)

            logger.debug(
                "atomic_state_query.event",
                namespace=namespace,
                decision_type=decision_type,
                decision_reason=decision_reason,
            )

            return (state, decision_type, decision_reason)

        except Exception as e:
            logger.exception(
                "atomic_state_query.query_failed",
                error=e,
            )
            return (
                {
                    "namespace": namespace,
                    "scope": "regional",
                    "governance_mode": "NORMAL",
                    "is_active": False,
                    "emergency_level": "normal",
                },
                "FALLBACK",
                f"Query failed, using safe default: {e}",
            )

    def preload_script(self) -> str | None:
        """
        Preload the Lua script into Redis and cache its SHA.

        Returns:
            Script SHA hash
        """
        if self._script_sha is None:
            self._script_sha = self._redis.script_load(ATOMIC_STATE_QUERY_SCRIPT)
        return self._script_sha

    def query_with_sha(
        self,
        namespace: str,
        precedence: str | None = None,
    ) -> tuple[dict[str, Any], str, str]:
        """
        Execute the cached script via EVALSHA.

        Calls preload_script() when no SHA is cached, and falls back to EVAL
        when EVALSHA fails.

        Args:
            namespace: target namespace
            precedence: command precedence

        Returns:
            Same as query_effective_state
        """
        sha = self.preload_script()
        global_key = self._get_global_key()
        regional_key = self._get_regional_key(namespace)
        precedence_level = self.PRECEDENCE_LEVELS.get(precedence or "AUTO", 0)

        try:
            result = self._redis.evalsha(
                sha,
                2,
                global_key,
                regional_key,
                str(precedence_level),
            )
            return self._parse_result(result)
        except Exception:
            result = self._redis.eval(
                ATOMIC_STATE_QUERY_SCRIPT,
                2,
                global_key,
                regional_key,
                str(precedence_level),
            )
            return self._parse_result(result)


# =============================================================================
# Singleton
# =============================================================================

_atomic_query: AtomicStateQuery | None = None
_atomic_query_lock = threading.Lock()


def get_atomic_state_query() -> AtomicStateQuery:
    """
    Return the AtomicStateQuery singleton.

    Returns:
        AtomicStateQuery instance
    """
    global _atomic_query
    if _atomic_query is None:
        with _atomic_query_lock:
            if _atomic_query is None:
                # get_redis_client lives on the PRO state_backend extension;
                # raise ImportError when unavailable so callers can fall
                # back to their manual non-Redis path (preserves OSS-only
                # behavior in tests that pass ``atomic_query=None``).
                from baldur.core import state_backend as _state_backend

                get_redis = getattr(_state_backend, "get_redis_client", None)
                if not callable(get_redis):
                    raise ImportError(
                        "baldur.core.state_backend.get_redis_client is a "
                        "PRO-only extension; AtomicStateQuery requires a "
                        "real Redis client."
                    )
                _atomic_query = AtomicStateQuery(get_redis())
    return _atomic_query


def reset_atomic_state_query() -> None:
    """Reset the singleton (for tests)."""
    global _atomic_query
    _atomic_query = None
