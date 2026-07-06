"""
Config Drift Monitor — SHA-256 based environment variable change detection.

Monitors environment variable changes and invalidates cached settings
when drift is detected.

Migrated from config.py (v6.3.0) as part of 358 settings consolidation.

Usage:
    monitor = get_config_drift_monitor()

    # Check for env var changes and invalidate cache if needed
    if monitor.check_and_invalidate("circuit_breaker", "BALDUR_CB_"):
        logger.info("config.cache_invalidated")

    # Or use the safe getter pattern in each settings module:
    settings = get_circuit_breaker_settings_safe()
"""

from __future__ import annotations

import hashlib
import os
import threading

import structlog

logger = structlog.get_logger()

# Drift Detection metrics
try:
    from baldur.metrics.drift_metrics import (
        record_config_cache_hit,
        record_config_cache_invalidated,
        record_config_cache_miss,
        record_config_env_changed,
    )

    HAS_DRIFT_METRICS = True
except ImportError:
    HAS_DRIFT_METRICS = False

    def record_config_env_changed(config_type: str) -> None:
        return None

    def record_config_cache_invalidated(config_type: str) -> None:
        return None

    def record_config_cache_hit(config_type: str) -> None:
        return None

    def record_config_cache_miss(config_type: str) -> None:
        return None


class ConfigDriftMonitor:
    """
    Environment variable change detection and cache invalidation.

    v6.3.0: Drift Detection implementation.

    When environment variables change, the associated cache functions
    are invalidated and Prometheus metrics are recorded.

    Security Hardening: Uses SHA-256 for collision resistance.

    Usage:
        monitor = get_config_drift_monitor()

        # Check and invalidate on env var change
        if monitor.check_and_invalidate("circuit_breaker", "BALDUR_CB_"):
            logger.info("config.cache_invalidated")

        # Get settings with drift check
        settings = get_circuit_breaker_settings_safe()
    """

    _instance: ConfigDriftMonitor | None = None
    _lock = threading.Lock()

    def __new__(cls) -> ConfigDriftMonitor:
        """Singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._init()
                    cls._instance = instance
        return cls._instance

    def _init(self) -> None:
        """Initialize internal state."""
        self._env_hashes: dict[str, str] = {}
        self._hash_lock = threading.Lock()
        self._cache_functions: dict[str, callable] = {}

    def register_cache_function(self, config_type: str, func: callable) -> None:
        """
        Register a cache invalidation function for config_type.

        Args:
            config_type: Configuration type (e.g. "notification")
            func: An lru_cache function (cache_clear called) or
                  a plain callable (called directly to reset cache)
        """
        with self._hash_lock:
            self._cache_functions[config_type] = func

    def _compute_env_hash(self, prefix: str) -> str:
        """Compute SHA-256 hash of environment variables with given prefix.

        Security Hardening (214_SECURITY_VULNERABILITY_FIXES):
        - MD5 -> SHA-256 for collision resistance
        """
        relevant_vars = {k: v for k, v in os.environ.items() if k.startswith(prefix)}
        content = str(sorted(relevant_vars.items()))
        return hashlib.sha256(content.encode()).hexdigest()

    def check_and_invalidate(self, config_type: str, prefix: str) -> bool:
        """
        Check for env var changes and invalidate cache if needed.

        Args:
            config_type: Configuration type
            prefix: Environment variable prefix (e.g. "BALDUR_CB_")

        Returns:
            True if cache was invalidated, False otherwise
        """
        with self._hash_lock:
            current_hash = self._compute_env_hash(prefix)
            previous_hash = self._env_hashes.get(config_type)

            if previous_hash and current_hash != previous_hash:
                # Environment variable change detected
                record_config_env_changed(config_type)
                self._invalidate_cache(config_type)
                self._env_hashes[config_type] = current_hash
                return True

            self._env_hashes[config_type] = current_hash
            return False

    def _invalidate_cache(self, config_type: str) -> None:
        """Invalidate cache for given config type."""
        record_config_cache_invalidated(config_type)

        func = self._cache_functions.get(config_type)
        if func:
            if hasattr(func, "cache_clear"):
                func.cache_clear()
            else:
                # Plain callable (e.g. reset_*_settings functions)
                func()

    def get_stats(self) -> dict[str, str]:
        """Return current stored hash values."""
        with self._hash_lock:
            return self._env_hashes.copy()


def get_config_drift_monitor() -> ConfigDriftMonitor:
    """
    Get the singleton ConfigDriftMonitor instance.

    Returns:
        ConfigDriftMonitor singleton
    """
    return ConfigDriftMonitor()


def reset_config_drift_monitor() -> None:
    """Reset the singleton ConfigDriftMonitor (for testing)."""
    ConfigDriftMonitor._instance = None


__all__ = [
    "ConfigDriftMonitor",
    "get_config_drift_monitor",
    "reset_config_drift_monitor",
]
