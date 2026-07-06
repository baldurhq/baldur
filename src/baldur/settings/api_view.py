"""
API View Settings - Pydantic v2.

API 페이징 및 필터링 기본 설정입니다.

Replaces:
- api/django/views 내 default_limit, default_offset, max_limit

Environment Variables:
    BALDUR_API_VIEW_DEFAULT_LIMIT=100
    BALDUR_API_VIEW_DEFAULT_OFFSET=0
    BALDUR_API_VIEW_MAX_LIMIT=1000

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 4 [22])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §3.6
"""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config


class ApiViewSettings(BaseSettings):
    """
    API View 페이징 및 필터링 설정.

    페이징:
    - default_limit: 기본 페이지 크기 (100)
    - default_offset: 기본 시작 위치 (0)
    - max_limit: 최대 페이지 크기 (1000)

    정렬:
    - default_order: 기본 정렬 순서 ("-created_at")

    기타:
    - max_events: XTest 최대 이벤트 수 (500)
    - max_incidents: XTest 최대 인시던트 수 (100)
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
    # Auto-Tuning Views - from views/auto_tuning.py (Phase 3 리팩토링)
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
    # XTest Observability Views - from views/xtest/observability.py (Phase 3 리팩토링)
    # ==========================================================================
    xtest_timeline_default_limit: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Default limit for XTest timeline queries",
    )

    # Postmortem 히스토리 limit (새 필드명)
    postmortem_history_limit: int = Field(
        default=100,
        ge=50,
        le=500,
        description="History query limit for postmortem generation",
    )

    # ==========================================================================
    # Auto Postmortem - CB CLOSED 시 자동 Post-mortem 생성 (새 필드명)
    # ==========================================================================
    auto_postmortem_min_duration: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="Minimum incident duration for automatic post-mortem generation (seconds)",
    )

    # ==========================================================================
    # Post-mortem Notification - CB 복구 후 Post-mortem 알림
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
        """default_limit이 max_limit보다 작은지 검증."""
        if self.default_limit > self.max_limit:
            raise ValueError(
                f"default_limit ({self.default_limit}) must be less than or equal to "
                f"max_limit ({self.max_limit})"
            )
        return self


# ==========================================================================
# Singleton 관리
# ==========================================================================


def get_api_view_settings() -> "ApiViewSettings":
    """Get cached ApiViewSettings instance."""
    from baldur.runtime import get_runtime

    return get_runtime().get_settings(ApiViewSettings)


def reset_api_view_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.runtime import get_runtime

    get_runtime().reset_settings(ApiViewSettings)
