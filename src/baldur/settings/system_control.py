"""
System Control Settings - Pydantic v2.

State backend configuration replacing the legacy _get_config() pattern
in core/state_backend.py with proper Pydantic BaseSettings.

Supports file, Redis, and memory backends with Django settings fallback
and Redis URL 3-tier fallback chain for backward compatibility.

Environment Variables:
    BALDUR_SYSTEM_CONTROL_BACKEND=file
    BALDUR_SYSTEM_CONTROL_DIR=logs/baldur_state
    BALDUR_SYSTEM_CONTROL_REDIS_URL=
    BALDUR_SYSTEM_CONTROL_REDIS_KEY_PREFIX=baldur:state:
    BALDUR_SYSTEM_CONTROL_REDIS_SCAN_BATCH_SIZE=100
    BALDUR_SYSTEM_CONTROL_REDIS_MAX_SCAN_KEYS=10000
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class SystemControlSettings(BaseSettings):
    """
    State backend configuration.

    Replaces the legacy _get_config() pattern in core/state_backend.py
    with Pydantic BaseSettings for type-safe, validated configuration.
    """

    model_config = make_settings_config("BALDUR_SYSTEM_CONTROL_")

    # =========================================================================
    # Backend selection
    # =========================================================================
    backend: Literal["file", "redis", "memory"] = Field(
        default="file",
        description="State backend type: 'file' (single server), 'redis' (multi server), 'memory' (testing).",
    )

    # =========================================================================
    # File backend
    # =========================================================================
    dir: str = Field(
        default="logs/baldur_state",
        description="Directory for file-based state storage.",
    )

    @property
    def state_dir(self) -> str:
        """Alias for dir field (backward compatibility with state_backend.py)."""
        return self.dir

    # =========================================================================
    # Redis backend
    # =========================================================================
    redis_url: str = Field(
        default="",
        description="Redis URL for state backend. Empty triggers fallback chain.",
    )
    redis_key_prefix: str = Field(
        default="baldur:state:",
        description="Key prefix for Redis state backend.",
    )
    redis_scan_batch_size: int = Field(
        default=100,
        ge=50,
        le=1000,
        description="Batch size for Redis SCAN operations.",
    )
    redis_max_scan_keys: int = Field(
        default=10000,
        ge=100,
        le=1_000_000,
        description="Maximum keys to return from Redis SCAN (DoS prevention).",
    )

    @field_validator("backend", mode="before")
    @classmethod
    def normalize_backend(cls, v: str) -> str:
        """Normalize backend value to lowercase for compatibility."""
        if isinstance(v, str):
            return v.lower()
        return v

    @model_validator(mode="before")
    @classmethod
    def _django_settings_fallback(cls, data: dict) -> dict:
        """Preserve Django settings priority from legacy _get_config() pattern."""
        if not os.environ.get("DJANGO_SETTINGS_MODULE"):
            return data
        try:
            from django.conf import settings as django_settings

            mapping = {
                "backend": "BALDUR_SYSTEM_CONTROL_BACKEND",
                "dir": "BALDUR_SYSTEM_CONTROL_DIR",
                "redis_url": "BALDUR_REDIS_URL",
            }
            for field_name, django_key in mapping.items():
                if field_name not in data or not data[field_name]:
                    val = getattr(django_settings, django_key, None)
                    if val is not None:
                        data[field_name] = str(val)
        except Exception:
            pass
        return data

    @model_validator(mode="after")
    def _fallback_redis_url(self) -> SystemControlSettings:
        """3-tier fallback: STATE_REDIS_URL -> REDIS_URL (legacy env) -> RedisSettings.url"""
        if not self.redis_url:
            # Tier 1: legacy env var (existing state_backend.py compat)
            legacy = os.environ.get("BALDUR_REDIS_URL")
            if legacy:
                self.redis_url = legacy
                return self
            # Tier 2: global RedisSettings (Convention over Configuration)
            try:
                from baldur.settings.redis import get_redis_settings

                self.redis_url = get_redis_settings().url
            except Exception:
                pass
        return self


def get_system_control_settings() -> SystemControlSettings:
    """Return cached SystemControlSettings via RootConfig."""
    from baldur.settings.root import get_config

    return get_config().core.system_control


def reset_system_control_settings() -> None:
    """Reset cached SystemControlSettings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["system_control"]
    except KeyError:
        pass
