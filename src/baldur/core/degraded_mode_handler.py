# packages/baldur-python/src/baldur/core/degraded_mode_handler.py
"""
DegradedModeHandler - manages Degraded Mode when the command center is
unreachable (Platinum SLA optimization).

Runtime fallback defaults + Degraded Mode state management. Keeps 0ms
protection logic running even while the command center is down.

Note: static configuration defaults live in safe_defaults.py.
"""

import os
import threading
from typing import Any

import structlog

from baldur.settings.introspection import register_direct_read_env_vars

__all__ = ["DegradedModeHandler"]

logger = structlog.get_logger()


class DegradedModeHandler:
    """
    Manages Degraded Mode and runtime fallback when the command center fails.

    Characteristics:
    - Package-internal (no external file dependency)
    - Thread-safe
    - Degraded Mode awareness logging
    - Runtime override support

    Usage:
        # Use defaults
        config = DegradedModeHandler.get_cb_config()

        # Override (optional)
        DegradedModeHandler.set('CB_FAILURE_THRESHOLD', 5)

        # Or via environment variable
        # BALDUR_CB_FAILURE_THRESHOLD=5
    """

    _lock = threading.RLock()
    _degraded_warned = False
    _is_degraded = False
    _degraded_reason: str = ""

    # Default settings (conservative)
    _defaults: dict[str, Any] = {
        # Circuit Breaker
        "CB_FAILURE_THRESHOLD": 3,  # Open after 3 failures
        "CB_RECOVERY_TIMEOUT": 60,  # Half-Open after 60s
        "CB_HALF_OPEN_MAX_CALLS": 1,  # Test only once in Half-Open
        # Rate Limit
        "RATE_LIMIT_PER_MINUTE": 100,  # 100 per minute
        "RATE_LIMIT_BURST": 10,  # Burst of 10
        # Timeout
        "DEFAULT_TIMEOUT_MS": 5000,  # 5s
        "HEALTH_CHECK_TIMEOUT_MS": 1000,  # health check 1s
        # Retry
        "MAX_RETRY_ATTEMPTS": 3,  # up to 3 retries
        "RETRY_BACKOFF_BASE_MS": 100,  # backoff base 100ms
    }

    # Runtime override store
    _overrides: dict[str, Any] = {}

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        """
        Thread-safe config lookup.

        Priority: environment variable > runtime override > default
        """
        with cls._lock:
            # 1. Check environment variable
            env_key = f"BALDUR_{key}"
            env_value = os.environ.get(env_key)
            if env_value is not None:
                return cls._parse_value(env_value)

            # 2. Check runtime override
            if key in cls._overrides:
                return cls._overrides[key]

            # 3. Return default
            return cls._defaults.get(key, default)

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        """Thread-safe config override."""
        with cls._lock:
            cls._overrides[key] = value

    @classmethod
    def get_all_keys(cls) -> list:
        """List of all available config keys."""
        with cls._lock:
            return list(cls._defaults.keys())

    @classmethod
    def get_cb_config(cls) -> dict[str, Any]:
        """Bulk CB config lookup."""
        return {
            "failure_threshold": cls.get("CB_FAILURE_THRESHOLD"),
            "recovery_timeout": cls.get("CB_RECOVERY_TIMEOUT"),
            "half_open_max_calls": cls.get("CB_HALF_OPEN_MAX_CALLS"),
        }

    @classmethod
    def get_rate_limit_config(cls) -> dict[str, Any]:
        """Bulk Rate Limit config lookup."""
        return {
            "per_minute": cls.get("RATE_LIMIT_PER_MINUTE"),
            "burst": cls.get("RATE_LIMIT_BURST"),
        }

    @classmethod
    def get_timeout_config(cls) -> dict[str, Any]:
        """Bulk Timeout config lookup."""
        return {
            "default_timeout_ms": cls.get("DEFAULT_TIMEOUT_MS"),
            "health_check_timeout_ms": cls.get("HEALTH_CHECK_TIMEOUT_MS"),
        }

    @classmethod
    def get_retry_config(cls) -> dict[str, Any]:
        """Bulk Retry config lookup."""
        return {
            "max_retry_attempts": cls.get("MAX_RETRY_ATTEMPTS"),
            "retry_backoff_base_ms": cls.get("RETRY_BACKOFF_BASE_MS"),
        }

    @classmethod
    def enter_degraded_mode(cls, reason: str = "") -> None:
        """
        Enter Degraded Mode.

        - Called when the command center connection fails
        - Emits the warning log only once (Singleton)

        Args:
            reason: reason for entering Degraded Mode
        """
        with cls._lock:
            cls._is_degraded = True
            cls._degraded_reason = reason

            if not cls._degraded_warned:
                logger.warning(
                    "degraded_mode_handler.entered_degraded_mode",
                    reason=reason or "command_center_connection_failed",
                )
                cls._degraded_warned = True

    @classmethod
    def exit_degraded_mode(cls) -> None:
        """Exit Degraded Mode (on command center reconnect)."""
        with cls._lock:
            if cls._is_degraded:
                logger.info(
                    "degraded_mode_handler.exited_degraded_mode",
                )
            cls._is_degraded = False
            cls._degraded_warned = False
            cls._degraded_reason = ""

    @classmethod
    def is_degraded(cls) -> bool:
        """Whether currently in Degraded Mode."""
        with cls._lock:
            return cls._is_degraded

    @classmethod
    def get_status(cls) -> dict[str, Any]:
        """DegradedModeProtocol conformance — degraded mode status."""
        with cls._lock:
            return {
                "is_degraded": cls._is_degraded,
                "status": "degraded" if cls._is_degraded else "healthy",
                "source": "local_defaults" if cls._is_degraded else "command_center",
                "reason": cls._degraded_reason,
            }

    @classmethod
    def get_health_response(cls) -> dict[str, Any]:
        """
        HealthBridge integration point.

        The health response to return while in Degraded Mode. The consuming side
        (e.g. a shopping mall app) calls this method to integrate.
        """
        with cls._lock:
            return {
                "status": "degraded" if cls._is_degraded else "healthy",
                "is_degraded": cls._is_degraded,
                "source": "local_defaults" if cls._is_degraded else "command_center",
                "config": {
                    "cb": cls.get_cb_config(),
                    "rate_limit": cls.get_rate_limit_config(),
                    "timeout": cls.get_timeout_config(),
                    "retry": cls.get_retry_config(),
                },
            }

    @classmethod
    def _parse_value(cls, value: str) -> Any:
        """Convert an environment-variable string into an appropriate type."""
        # Boolean
        if value.lower() in ("true", "1", "yes"):
            return True
        if value.lower() in ("false", "0", "no"):
            return False

        # Integer
        try:
            return int(value)
        except ValueError:
            pass

        # Float
        try:
            return float(value)
        except ValueError:
            pass

        # Plain string
        return value

    @classmethod
    def reset(cls) -> None:
        """Reset state (for tests)."""
        with cls._lock:
            cls._overrides.clear()
            cls._is_degraded = False
            cls._degraded_warned = False
            cls._degraded_reason = ""


# Channel-2 direct-read registration: ``get()`` reads ``f"BALDUR_{key}"`` over
# the closed ``_defaults`` table, so each fallback knob is a real but
# computed-name direct read invisible to the literal AST registry scan. Register
# the enumerable set at import so the startup unknown-env-var scan does not
# false-positive an advertised degraded-mode knob (e.g. BALDUR_DEFAULT_TIMEOUT_MS,
# BALDUR_RATE_LIMIT_PER_MINUTE) that does not resolve to a Pydantic field.
register_direct_read_env_vars(
    *(f"BALDUR_{key}" for key in DegradedModeHandler._defaults)
)
