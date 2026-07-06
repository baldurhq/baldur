"""Settings for dependency graph and constraint engine."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

__all__ = [
    "SettingsDependencySettings",
    "get_settings_dependency_settings",
    "reset_settings_dependency_settings",
]


class SettingsDependencySettings(BaseSettings):
    """Settings for dependency graph and constraint engine."""

    model_config = make_settings_config("BALDUR_DEPENDENCY_")

    enabled: bool = Field(
        default=True,
        description="Enable dependency graph + constraint engine",
    )
    max_cascade_depth: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum BFS propagation depth",
    )
    auto_fix_enabled: bool = Field(
        default=False,
        description="Enable auto-fix in validate_and_fix() (False = report only)",
    )
    max_fix_iterations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum auto-fix iteration count",
    )


def get_settings_dependency_settings() -> SettingsDependencySettings:
    from baldur.settings.root import get_config

    return get_config().meta.settings_dependency


def reset_settings_dependency_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().meta.__dict__["settings_dependency"]
    except KeyError:
        pass
