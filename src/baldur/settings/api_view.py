"""
API View Settings - Pydantic v2.

Default API pagination and filtering settings.

Replaces:
- default_limit, default_offset, max_limit in api/django/views

Environment Variables:
    BALDUR_API_VIEW_DEFAULT_LIMIT=100
    BALDUR_API_VIEW_DEFAULT_OFFSET=0
    BALDUR_API_VIEW_MAX_LIMIT=1000
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ApiViewSettings(BaseSettings):
    """
    API View pagination and filtering settings.

    Pagination:
    - default_limit: default page size (100)
    - default_offset: default start position (0)
    - max_limit: maximum page size (1000)

    Ordering:
    - default_order: default sort order ("-created_at")

    Other:
    - max_events: maximum number of XTest events (500)
    - max_incidents: maximum number of XTest incidents (100)
    """

    model_config = make_settings_config("BALDUR_API_VIEW_")

    # ==========================================================================
    # Pagination - from api/django/views
    # ==========================================================================
    default_limit: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Default page size",
    )

    default_offset: int = Field(
        default=0,
        ge=0,
        description="Default offset",
    )

    max_limit: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum page size",
    )

    # ==========================================================================
    # Ordering - from api/django/views
    # ==========================================================================
    default_order: str = Field(
        default="-created_at",
        description="Default sort order (- prefix for descending)",
    )

    # ==========================================================================
    # XTest Views - from xtest/base.py
    # ==========================================================================
    max_events: int = Field(
        default=500,
        ge=100,
        le=5000,
        description="Maximum number of XTest events",
    )

    max_incidents: int = Field(
        default=100,
        ge=50,
        le=1000,
        description="Maximum number of XTest incidents",
    )

    max_injection: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Maximum number of XTest injections",
    )

    # ==========================================================================
    # Throttle Adapter - from throttle_adapter.py
    # ==========================================================================
    throttle_max_limit: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Throttle adapter maximum limit",
    )

    # ==========================================================================
    # Auto-Tuning Views - from views/auto_tuning.py (Phase 3 refactor)
    # ==========================================================================
    auto_tuning_export_limit: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Maximum records for Auto-Tuning CSV export",
    )

    auto_tuning_default_page_size: int = Field(
        default=20,
        ge=5,
        le=100,
        description="Default page size for Auto-Tuning history",
    )

    # ==========================================================================
    # XTest Observability Views - from views/xtest/observability.py (Phase 3)
    # ==========================================================================
    xtest_timeline_default_limit: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Default limit for XTest timeline queries",
    )

    # Postmortem history limit (new field name)
    postmortem_history_limit: int = Field(
        default=100,
        ge=50,
        le=500,
        description="History query limit for postmortem generation",
    )

    # ==========================================================================
    # Auto Postmortem - automatic post-mortem on CB CLOSED (new field name)
    # ==========================================================================
    auto_postmortem_min_duration: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="Minimum incident duration for automatic post-mortem generation (seconds)",
    )

    # ==========================================================================
    # Post-mortem Notification - post-mortem alert after CB recovery
    # ==========================================================================
    postmortem_notification_min_duration: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="Minimum incident duration for post-mortem notification (seconds)",
    )

    # ==========================================================================
    # Access Logging - from middleware/access_logging.py
    # ==========================================================================
    access_log_path: str = Field(
        default="logs/sensitive_access.log",
        description="File path for sensitive endpoint access logging",
    )

    @model_validator(mode="after")
    def validate_limits(self) -> "ApiViewSettings":
        """Validate that default_limit does not exceed max_limit."""
        if self.default_limit > self.max_limit:
            raise ValueError(
                f"default_limit ({self.default_limit}) must be less than or equal to "
                f"max_limit ({self.max_limit})"
            )
        return self


# ==========================================================================
# Singleton management
# ==========================================================================


def get_api_view_settings() -> "ApiViewSettings":
    """Get cached ApiViewSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ApiViewSettings)


def reset_api_view_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ApiViewSettings)
