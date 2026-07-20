"""
Security Settings - Pydantic v2.

Single Source of Truth for security-related configuration.

Replaces:
- core/config.py:SecurityConfig (lines 174-185)
- core/safe_defaults.py:SAFE_DEFAULTS["security"]
- core/safe_defaults.py:VALIDATION_RULES["security"]

Environment Variables:
    BALDUR_SECURITY_INJECTION_BAN_HOURS=24
    ... etc
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import MediumCount
from baldur.settings.validators import warn_below


class SecuritySettings(BaseSettings):
    """
    Security-related thresholds and timeouts with validation.

    All defaults match core/config.py:SecurityConfig
    All validation rules match core/safe_defaults.py:VALIDATION_RULES["security"]

    Note: Some fields are FATAL configs (see core/safe_defaults.py:FATAL_CONFIGS)
    - injection_ban_hours: SQL injection response
    """

    model_config = make_settings_config("BALDUR_SECURITY_")

    # ==========================================================================
    # Ban Settings (from core/config.py lines 179-181)
    # ==========================================================================
    temporary_ban_hours: int = Field(
        default=1,
        ge=1,
        le=168,
        description="Hours for temporary ban",
    )
    permanent_ban_threshold: MediumCount = Field(
        default=5,
        description="Number of temp bans before permanent ban",
    )

    # ==========================================================================
    # Cache and Security (from core/config.py lines 182-185)
    # ==========================================================================
    suspicious_ip_cache_timeout: int = Field(
        default=86400,
        ge=3600,
        le=604800,
        description="Suspicious IP cache timeout in seconds (1h-7d)",
    )
    injection_ban_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        description="Hours to ban for SQL injection attempts (FATAL config)",
    )

    # ==========================================================================
    # Cache Prefixes (from core/config.py lines 186-187)
    # ==========================================================================
    suspicious_ip_cache_prefix: str = Field(
        default="security:suspicious_ip:",
        description="Redis key prefix for suspicious IPs",
    )
    banned_ip_cache_prefix: str = Field(
        default="security:banned_ip:",
        description="Redis key prefix for banned IPs",
    )

    # ==========================================================================
    # Session Settings (368: Django Settings Decoupling)
    # ==========================================================================
    session_engine: str = Field(
        default="django.contrib.sessions.backends.db",
        description="Session backend engine identifier",
    )
    session_cookie_age: int = Field(
        default=1209600,
        ge=60,
        le=31536000,
        description="Session cookie TTL in seconds (default 14 days)",
    )

    @field_validator("injection_ban_hours")
    @classmethod
    def _warn_injection_ban_hours(cls, v: int) -> int:
        """
        FATAL config: injection_ban_hours.

        SQL injection attempts should result in meaningful bans.
        """
        return warn_below(12, "fatal_config.short_consider_hours_sql")(v)


def get_security_settings() -> "SecuritySettings":
    """Get cached SecuritySettings instance."""
    from baldur.settings.root import get_config

    return get_config().security_group.security


def reset_security_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().security_group.__dict__["security"]
    except KeyError:
        pass
