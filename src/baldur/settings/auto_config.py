"""
Auto-Configuration Settings for configure_baldur() wrapper.

Controls the behavior of the configure_baldur() function via
environment variables (BALDUR_AUTO_CONFIG_* prefix).
"""

from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class AutoConfigSettings(BaseSettings):
    """Settings controlling the behavior of the configure_baldur() wrapper."""

    model_config = make_settings_config("BALDUR_AUTO_CONFIG_")

    middleware: bool = True
    exception_handler: bool = True
    celery_signal_warning: bool = True


_cached: AutoConfigSettings | None = None


def get_auto_config_settings() -> "AutoConfigSettings":
    global _cached
    if _cached is None:
        _cached = AutoConfigSettings()
    return _cached


def reset_auto_config_settings() -> None:
    global _cached
    _cached = None
