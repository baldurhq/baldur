"""
Event Logging Settings — Runtime-configurable event log levels.

Provides runtime-changeable log levels for DLQ, Circuit Breaker,
Replay, and SLA event logging without server restart.

Migrated from config.py as part of 358 settings consolidation.

Priority (highest to lowest):
1. API/Admin settings (runtime changes)
2. Environment variables (container defaults)
3. Hardcoded defaults

Environment Variables:
    BALDUR_EVENT_LOGGING_DLQ_LOG_LEVEL=INFO
    BALDUR_EVENT_LOGGING_CB_LOG_LEVEL=WARNING
    BALDUR_EVENT_LOGGING_REPLAY_LOG_LEVEL=INFO
    BALDUR_EVENT_LOGGING_SLA_LOG_LEVEL=WARNING

Usage:
    config = get_event_logging_config()
    config.update(dlq_log_level="DEBUG")  # Runtime change
    config.get_dlq_log_level()  # "DEBUG"
"""

from __future__ import annotations

import threading

from pydantic_settings import BaseSettings, SettingsConfigDict

from baldur.utils.time import utc_now

__all__ = [
    "EventLoggingConfig",
    "get_event_logging_config",
    "reset_event_logging_config",
]


class _EventLoggingDefaults(BaseSettings):
    """EventLoggingConfig environment variable defaults.

    Uses BaseSettings for automatic env var parsing.
    """

    model_config = SettingsConfigDict(
        env_prefix="BALDUR_EVENT_LOGGING_",
        extra="ignore",
    )

    dlq_log_level: str = "INFO"
    cb_log_level: str = "WARNING"
    replay_log_level: str = "INFO"
    sla_log_level: str = "WARNING"


class EventLoggingConfig:
    """
    Runtime-configurable event logging settings.

    Allows API-level log level adjustment so operators can change
    logging levels from dashboard/API without server restart.

    Priority (highest to lowest):
    1. API/Admin settings (runtime changes)
    2. Environment variables (container defaults)
    3. Hardcoded defaults

    Example:
        >>> config = get_event_logging_config()
        >>> config.update(dlq_log_level="DEBUG")  # Runtime change
        >>> config.get_dlq_log_level()  # "DEBUG"
    """

    # Valid log levels
    VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    # Singleton instance
    _instance: EventLoggingConfig | None = None
    _lock = threading.Lock()

    def __new__(cls) -> EventLoggingConfig:
        """Singleton pattern for global configuration."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init_defaults()
                    cls._instance = instance
        return cls._instance

    def _init_defaults(self) -> None:
        """Initialize default values from environment or hardcoded defaults."""
        self._runtime_lock = threading.Lock()

        # Runtime-configurable values (API level)
        self._runtime_config: dict = {}

        # BaseSettings auto-parses environment variables
        _defaults = _EventLoggingDefaults()
        self._env_defaults = {
            "dlq_log_level": _defaults.dlq_log_level,
            "cb_log_level": _defaults.cb_log_level,
            "replay_log_level": _defaults.replay_log_level,
            "sla_log_level": _defaults.sla_log_level,
        }

        # Hardcoded defaults (fallback)
        self._hardcoded_defaults = {
            "dlq_log_level": "INFO",
            "cb_log_level": "WARNING",
            "replay_log_level": "INFO",
            "sla_log_level": "WARNING",
        }

        # Last updated timestamp (for audit trail)
        self._last_updated: dict = {}

    def _validate_level(self, level: str) -> str:
        """Validate and normalize log level."""
        level = level.upper()
        if level not in self.VALID_LEVELS:
            raise ValueError(
                f"Invalid log level: {level}. Valid levels: {self.VALID_LEVELS}"
            )
        return level

    def _get_value(self, key: str) -> str:
        """Get value with priority: runtime > env > hardcoded."""
        with self._runtime_lock:
            if key in self._runtime_config:
                return self._runtime_config[key]
        return self._env_defaults.get(key, self._hardcoded_defaults.get(key, "INFO"))

    def update(
        self,
        dlq_log_level: str | None = None,
        cb_log_level: str | None = None,
        replay_log_level: str | None = None,
        sla_log_level: str | None = None,
        updated_by: str = "api",
    ) -> dict:
        """
        Update logging configuration at runtime.

        Args:
            dlq_log_level: DLQ event log level (INFO recommended)
            cb_log_level: Circuit Breaker log level (WARNING recommended)
            replay_log_level: Replay event log level (INFO recommended)
            sla_log_level: SLA breach log level (WARNING recommended)
            updated_by: Change source (for audit trail)

        Returns:
            Updated configuration as dict
        """

        updates = {}

        with self._runtime_lock:
            if dlq_log_level is not None:
                level = self._validate_level(dlq_log_level)
                self._runtime_config["dlq_log_level"] = level
                updates["dlq_log_level"] = level

            if cb_log_level is not None:
                level = self._validate_level(cb_log_level)
                self._runtime_config["cb_log_level"] = level
                updates["cb_log_level"] = level

            if replay_log_level is not None:
                level = self._validate_level(replay_log_level)
                self._runtime_config["replay_log_level"] = level
                updates["replay_log_level"] = level

            if sla_log_level is not None:
                level = self._validate_level(sla_log_level)
                self._runtime_config["sla_log_level"] = level
                updates["sla_log_level"] = level

            if updates:
                self._last_updated = {
                    "timestamp": utc_now().isoformat(),
                    "updated_by": updated_by,
                    "changes": updates,
                }

        return self.to_dict()

    def reset(self) -> None:
        """Reset to environment/default values (clear runtime config)."""
        with self._runtime_lock:
            self._runtime_config.clear()
            self._last_updated = {}

    # Property-style getters for each log level
    def get_dlq_log_level(self) -> str:
        """Get DLQ event log level."""
        return self._get_value("dlq_log_level")

    def get_cb_log_level(self) -> str:
        """Get Circuit Breaker log level."""
        return self._get_value("cb_log_level")

    def get_replay_log_level(self) -> str:
        """Get Replay event log level."""
        return self._get_value("replay_log_level")

    def get_sla_log_level(self) -> str:
        """Get SLA breach log level."""
        return self._get_value("sla_log_level")

    def get_log_level_int(self, level_name: str) -> int:
        """Convert level name to logging module integer."""
        import logging

        return getattr(logging, level_name.upper(), logging.INFO)

    def to_dict(self) -> dict:
        """Export current configuration as dict."""
        return {
            "dlq_log_level": self.get_dlq_log_level(),
            "cb_log_level": self.get_cb_log_level(),
            "replay_log_level": self.get_replay_log_level(),
            "sla_log_level": self.get_sla_log_level(),
            "last_updated": self._last_updated,
        }


def get_event_logging_config() -> EventLoggingConfig:
    """
    Get the singleton EventLoggingConfig instance.

    Returns:
        EventLoggingConfig singleton
    """
    return EventLoggingConfig()


def reset_event_logging_config() -> None:
    """Reset the singleton EventLoggingConfig (for testing)."""
    EventLoggingConfig._instance = None
