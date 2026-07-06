"""
Admission Control Settings - Pydantic v2.

HTTP 요청 유입 제어(Admission Control) 설정입니다.
Tier별 Bulkhead 격벽 동시 실행 수와 활성화 여부를 관리합니다.

Environment Variables:
    BALDUR_ADMISSION_CONTROL_ENABLED=true
    BALDUR_ADMISSION_CONTROL_TIER_CRITICAL_MAX_CONCURRENT=100
    BALDUR_ADMISSION_CONTROL_TIER_STANDARD_MAX_CONCURRENT=50
    BALDUR_ADMISSION_CONTROL_TIER_NON_ESSENTIAL_MAX_CONCURRENT=20
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import LargeCount


class AdmissionControlSettings(BaseSettings):
    """HTTP 유입 제어 설정."""

    model_config = make_settings_config("BALDUR_ADMISSION_CONTROL_")

    enabled: bool = Field(
        default=True,
        description="Enable/disable Admission Control",
    )

    # =========================================================================
    # Tier별 Bulkhead 최대 동시 실행 수
    # =========================================================================
    tier_critical_max_concurrent: LargeCount = Field(
        default=100,
        description="Maximum concurrent executions for critical tier bulkhead",
    )

    tier_standard_max_concurrent: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum concurrent executions for standard tier bulkhead",
    )

    tier_non_essential_max_concurrent: int = Field(
        default=20,
        ge=1,
        le=200,
        description="Maximum concurrent executions for non_essential tier bulkhead",
    )

    # =========================================================================
    # Tier별 Bulkhead 획득 대기 Timeout (초)
    # 0이면 즉시 실패 (Zero-Wait). Micro-burst 흡수를 위해
    # critical/standard에만 짧은 대기를 부여하고,
    # non_essential은 부하 시 가장 먼저 차단되므로 Zero-Wait 유지.
    # =========================================================================
    tier_critical_bulkhead_timeout_seconds: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Critical tier bulkhead acquire timeout (seconds). 0 means fail immediately.",
    )

    tier_standard_bulkhead_timeout_seconds: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description="Standard tier bulkhead acquire timeout (seconds). 0 means fail immediately.",
    )

    tier_non_essential_bulkhead_timeout_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Non_essential tier bulkhead acquire timeout (seconds). Default zero-wait.",
    )

    def get_tier_max_concurrent(self, tier_id: str) -> int:
        """tier_id에 대응하는 Bulkhead 최대 동시 실행 수 반환."""
        tier_map = {
            "critical": self.tier_critical_max_concurrent,
            "standard": self.tier_standard_max_concurrent,
            "non_essential": self.tier_non_essential_max_concurrent,
        }
        return tier_map.get(tier_id, self.tier_standard_max_concurrent)

    def get_tier_bulkhead_timeout(self, tier_id: str) -> float | None:
        """tier별 Bulkhead 대기 timeout 반환. 0이면 None(즉시 실패)."""
        tier_map = {
            "critical": self.tier_critical_bulkhead_timeout_seconds,
            "standard": self.tier_standard_bulkhead_timeout_seconds,
            "non_essential": self.tier_non_essential_bulkhead_timeout_seconds,
        }
        value = tier_map.get(tier_id, 0.0)
        return value if value > 0 else None


def get_admission_control_settings() -> AdmissionControlSettings:
    from baldur.settings.root import get_config

    return get_config().core.admission_control


def reset_admission_control_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().core.__dict__["admission_control"]
    except KeyError:
        pass
