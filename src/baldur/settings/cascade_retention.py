"""
Cascade Retention Settings - Pydantic v2.

Cascade 데이터의 Hot/Warm/Cold 계층별 보관 정책 설정입니다.

Replaces:
- audit/cascade_config.py:CascadeRetentionConfig (하드코딩된 기본값)

Environment Variables:
    BALDUR_CASCADE_RETENTION_HOT_RETENTION_DAYS=7
    BALDUR_CASCADE_RETENTION_WARM_RETENTION_DAYS=90
    BALDUR_CASCADE_RETENTION_COLD_RETENTION_DAYS=365

Reference:
- docs/baldur/middleware_system/92_CONFIG_IMPLEMENTATION_GUIDE.md (Week 3 [16])
- docs/baldur/middleware_system/91_CONFIG_INVENTORY.md §14.2
- docs/baldur/middleware_system/76_CASCADE_EVENT_AUDIT.md
"""

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config

logger = structlog.get_logger()


class CascadeRetentionSettings(BaseSettings):
    """
    Cascade 데이터 보관 정책 설정.

    Tiered Storage 모델:
    - Hot (Redis): 실시간 조회용, 짧은 보관 (7일)
    - Warm (PostgreSQL): 복잡한 쿼리, 중간 보관 (90일)
    - Cold (Archive): 법적 요구사항, 장기 보관 (365일)

    데이터 흐름:
    Hot → Warm → Cold → 삭제
    """

    model_config = make_settings_config("BALDUR_CASCADE_RETENTION_")

    # ==========================================================================
    # Hot Tier (Redis) - from cascade_config.py
    # ==========================================================================
    hot_retention_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description="Retention period in Redis (days). For fast lookups.",
    )

    hot_max_count: int = Field(
        default=10000,
        ge=1000,
        le=100000,
        description="Maximum event count in Redis (memory limit)",
    )

    # ==========================================================================
    # Warm Tier (PostgreSQL) - from cascade_config.py
    # ==========================================================================
    warm_retention_days: int = Field(
        default=90,
        ge=30,
        le=365,
        description="Retention period in PostgreSQL (days). For audit purposes.",
    )

    # ==========================================================================
    # Cold Tier (Archive) - from cascade_config.py
    # ==========================================================================
    cold_retention_days: int = Field(
        default=365,
        ge=180,
        le=2555,  # 7년
        description="Archive retention period (days). Legal requirement.",
    )

    # ==========================================================================
    # Index & Anchor - from cascade_config.py
    # ==========================================================================
    index_retention_days: int = Field(
        default=30,
        ge=7,
        le=90,
        description="Index key retention period (days)",
    )

    anchor_retention_days: int = Field(
        default=90,
        ge=30,
        le=365,
        description="Checkpoint (anchor) retention period (days)",
    )

    # ==========================================================================
    # Buffer Settings (from cascade_config.py#L274-281)
    # ==========================================================================
    buffer_warning_threshold: float = Field(
        default=0.7,
        ge=0.5,
        le=0.9,
        description="Buffer usage warning threshold (70%)",
    )

    buffer_critical_threshold: float = Field(
        default=0.9,
        ge=0.7,
        le=0.99,
        description="Buffer usage critical threshold (90%)",
    )

    # ==========================================================================
    # Rate Limiting (from cascade_config.py#L288)
    # ==========================================================================
    max_events_per_second: int = Field(
        default=10000,
        ge=1,
        le=1000000,
        description=(
            "Maximum audit events per second threshold. "
            "100,000+ recommended for enterprise environments. "
            "Sampling or warnings triggered when exceeded."
        ),
    )

    # ==========================================================================
    # Cascade Auditor - from audit/cascade_auditor.py
    # ==========================================================================
    max_cascade_index_size: int = Field(
        default=10000,
        ge=1000,
        le=100000,
        description="Maximum cascade index size. Limits Redis memory usage.",
    )

    @model_validator(mode="after")
    def validate_tier_order(self) -> "CascadeRetentionSettings":
        """보관 기간 순서 검증: Hot < Warm < Cold."""
        if self.hot_retention_days >= self.warm_retention_days:
            logger.warning(
                "cascade_retention.hot_retention_not_shorter_than_warm",
                hot_retention_days=self.hot_retention_days,
                warm_retention_days=self.warm_retention_days,
            )
        if self.warm_retention_days >= self.cold_retention_days:
            logger.warning(
                "cascade_retention.warm_retention_not_shorter_than_cold",
                warm_retention_days=self.warm_retention_days,
                cold_retention_days=self.cold_retention_days,
            )
        return self

    @field_validator("buffer_critical_threshold")
    @classmethod
    def validate_buffer_order(cls, v: float, info) -> float:
        """Critical이 Warning보다 커야 함."""
        # Note: cross-field validation은 model_validator에서 더 적합하지만
        # 여기서는 경고만 발생
        if v <= 0.7:
            logger.warning(
                "cascade_retention.buffer_critical_threshold_low",
                setting_value=v,
            )
        return v


# =============================================================================
# Singleton Pattern
# =============================================================================


def get_cascade_retention_settings() -> "CascadeRetentionSettings":
    """Get cached CascadeRetentionSettings instance."""
    from baldur.settings.root import get_config

    return get_config().audit_group.cascade_retention


def reset_cascade_retention_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().audit_group.__dict__["cascade_retention"]
    except KeyError:
        pass
