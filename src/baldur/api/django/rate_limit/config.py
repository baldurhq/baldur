"""
Rate Limit Configuration — Settings loader and Prometheus metrics.

Provides runtime config reading and lazy Prometheus metric initialization.

The Control-API limit/window/emergency values come from the single canonical
env-var surface ``RateLimitSettings`` (``BALDUR_RATE_LIMIT_*``), the same
variables the PRO RuntimeConfigManager seeds from and exposes as
console-editable — so the limit behaves identically with and without PRO
registered. ``ApiRateLimitSettings`` continues to own the non-overlapping
fields (path prefix, Redis health checker, local-limiter cleanup).

Extracted from api/django/rate_limit.py as part of 358 rate_limit package split.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


# =============================================================================
# Configuration Constants (from Settings - env var based)
# =============================================================================


def _get_api_rate_limit_settings():
    """Get ApiRateLimitSettings instance (lazy import)."""
    try:
        from baldur.settings.api_rate_limit import get_api_rate_limit_settings

        return get_api_rate_limit_settings()
    except ImportError:
        return None


def _get_setting(attr: str, fallback: Any) -> Any:
    """Get value from Settings or return fallback."""
    settings = _get_api_rate_limit_settings()
    if settings is not None:
        return getattr(settings, attr, fallback)
    return fallback


# Fallback constants (used when Settings load fails)
_FALLBACK_DEFAULT_RATE_LIMIT = 100
_FALLBACK_DEFAULT_WINDOW_SECONDS = 60
_FALLBACK_EMERGENCY_RATE_LIMIT = 10
_FALLBACK_EMERGENCY_WINDOW_SECONDS = 60
_FALLBACK_CONTROL_API_PATH_PREFIX = "/api/baldur/"

# Fallback log path
FALLBACK_LOG_PATH = Path("logs/rate_limit_fallback.jsonl")


# =============================================================================
# Runtime Config Reader (API Control)
# =============================================================================


def _settings_sourced_config() -> dict:
    """Source the Control-API limit config from the canonical RateLimitSettings.

    ``RateLimitSettings.control_api_* / emergency_*`` (``BALDUR_RATE_LIMIT_*``)
    is the single env-var surface governing the limit in both OSS and PRO.
    Falls back to the hardcoded constants only if the settings import itself
    fails (the final ImportError fallback).
    """
    try:
        from baldur.settings.rate_limit import get_rate_limit_settings

        settings = get_rate_limit_settings()
        return {
            "control_api_rate_limit": settings.control_api_rate_limit,
            "control_api_window_seconds": settings.control_api_window_seconds,
            "emergency_rate_limit": settings.emergency_rate_limit,
            "emergency_window_seconds": settings.emergency_window_seconds,
        }
    except ImportError:
        return {
            "control_api_rate_limit": _FALLBACK_DEFAULT_RATE_LIMIT,
            "control_api_window_seconds": _FALLBACK_DEFAULT_WINDOW_SECONDS,
            "emergency_rate_limit": _FALLBACK_EMERGENCY_RATE_LIMIT,
            "emergency_window_seconds": _FALLBACK_EMERGENCY_WINDOW_SECONDS,
        }


def _get_runtime_config_manager():
    """Return the registered PRO RuntimeConfigManager, or None if PRO is absent.

    A None result is the normal OSS path — not a failure — so it carries no
    log. Only a registered manager that actually raises is a failure worth a
    WARNING (handled in ``get_rate_limit_config``).
    """
    try:
        from baldur.factory.registry import ProviderRegistry

        return ProviderRegistry.runtime_config_manager.safe_get()
    except Exception:
        return None


def get_rate_limit_config() -> dict:
    """
    Get rate limit configuration from RuntimeConfigManager or Settings.

    Priority:
    1. RuntimeConfigManager (PRO runtime dynamic settings, when registered)
    2. RateLimitSettings (canonical BALDUR_RATE_LIMIT_* env-var surface)
    3. Hardcoded fallback constants (settings import failure only)

    PRO absent (RuntimeConfigManager not registered) is the normal OSS path:
    the settings-sourced dict is returned directly with no warning. A WARNING
    is emitted only when a *registered* manager raises.

    Returns:
        dict with keys:
        - control_api_rate_limit: int (requests/minute for normal mode)
        - control_api_window_seconds: int
        - emergency_rate_limit: int (requests/minute for emergency mode)
        - emergency_window_seconds: int
    """
    settings_config = _settings_sourced_config()

    manager = _get_runtime_config_manager()
    if manager is None:
        return settings_config

    try:
        config = manager.get_rate_limit_config()
        return {
            "control_api_rate_limit": config.get(
                "control_api_rate_limit", settings_config["control_api_rate_limit"]
            ),
            "control_api_window_seconds": config.get(
                "control_api_window_seconds",
                settings_config["control_api_window_seconds"],
            ),
            "emergency_rate_limit": config.get(
                "emergency_rate_limit", settings_config["emergency_rate_limit"]
            ),
            "emergency_window_seconds": config.get(
                "emergency_window_seconds",
                settings_config["emergency_window_seconds"],
            ),
        }
    except Exception as e:
        # A registered manager actually failed — settings fallback + WARNING.
        logger.warning(
            "rate_limit.runtime_config_failed",
            error=e,
        )
        return settings_config


# =============================================================================
# Prometheus Metrics (Lazy Import)
# =============================================================================


def _get_metrics():
    """Get or create Prometheus metrics (lazy import to avoid circular deps)."""
    try:
        from prometheus_client import REGISTRY, Counter, Gauge

        # Check if already registered
        if "baldur_rate_limit_exceeded_total" in REGISTRY._names_to_collectors:
            exceeded_total = REGISTRY._names_to_collectors[
                "baldur_rate_limit_exceeded_total"
            ]
            degraded_mode = REGISTRY._names_to_collectors[
                "baldur_rate_limit_degraded_mode"
            ]
            failover_total = REGISTRY._names_to_collectors[
                "baldur_rate_limit_failover_total"
            ]
        else:
            exceeded_total = Counter(
                "baldur_rate_limit_exceeded_total",
                "Rate limit exceeded count",
                ["mode"],  # normal, emergency
            )
            degraded_mode = Gauge(
                "baldur_rate_limit_degraded_mode",
                "Rate limit operating in degraded mode (1=yes, 0=no)",
            )
            failover_total = Counter(
                "baldur_rate_limit_failover_total",
                "Number of times rate limit failed over to local memory",
            )

        return exceeded_total, degraded_mode, failover_total
    except ImportError:
        return None, None, None


__all__ = [
    "FALLBACK_LOG_PATH",
    "get_rate_limit_config",
]
