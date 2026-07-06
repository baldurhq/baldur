"""
Base Settings Configuration for Pydantic Settings.

Provides common configuration for all settings classes.
"""

from typing import Any

from pydantic_settings import SettingsConfigDict

# Common configuration for all settings
COMMON_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=None,
    extra="ignore",
    validate_default=True,
    env_nested_delimiter="__",
)


def make_settings_config(env_prefix: str, **overrides: Any) -> SettingsConfigDict:
    """Create a SettingsConfigDict with common defaults and the given env_prefix.

    Args:
        env_prefix: Environment variable prefix (e.g. "BALDUR_CB_").
        **overrides: Additional SettingsConfigDict keys to override
                     (e.g. extra="forbid").

    Returns:
        SettingsConfigDict with common defaults merged.
    """
    merged: dict[str, Any] = {
        **COMMON_SETTINGS_CONFIG,
        "env_prefix": env_prefix,
        **overrides,
    }
    return SettingsConfigDict(**merged)  # type: ignore[typeddict-item]
