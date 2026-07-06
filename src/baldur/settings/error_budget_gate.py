"""
Error Budget Gate Settings - Pydantic v2.

에러 예산 게이트 설정.
자동화 허용/차단을 에러 예산 기반으로 결정하는 게이트 설정.

Moved from: services/error_budget_gate/config.py (BaseSettings 전환)

Environment Variables:
    BALDUR_ERROR_BUDGET_GATE_ENABLED=true
    BALDUR_ERROR_BUDGET_GATE_CRITICAL_THRESHOLD_PERCENT=10.0
    BALDUR_ERROR_BUDGET_GATE_FAIL_OPEN=true

Reference:
- docs/baldur/12_ERROR_BUDGET.md
"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import Percentage


class ErrorBudgetGateSettings(BaseSettings):
    """
    에러 예산 게이트 설정.

    Attributes:
        enabled: 게이트 활성화 여부 (False면 항상 자동화 허용)
        critical_threshold_percent: 이 값 미만이면 자동화 차단 (기본: 10%)
        warning_threshold_percent: 이 값 미만이면 경고 표시 (기본: 20%)
        threshold_hysteresis_buffer_percent: 임계치 복구 시 버퍼 (플래핑 방지, 기본: 2%)
        fail_open: 에러 예산 조회 실패 시 자동화 허용 여부 (기본: True)
        cache_ttl_seconds: 에러 예산 캐시 TTL (기본: 30초)
    """

    model_config = make_settings_config("BALDUR_ERROR_BUDGET_GATE_")

    enabled: bool = Field(
        default=False,
        description="Enable gate (False always allows automation)",
    )
    critical_threshold_percent: Percentage = Field(
        default=10.0,
        description="Block automation below this threshold (%)",
    )
    warning_threshold_percent: Percentage = Field(
        default=20.0,
        description="Show warning below this threshold (%)",
    )
    threshold_hysteresis_buffer_percent: float = Field(
        default=2.0,
        ge=0.0,
        le=10.0,
        description="Buffer applied during threshold recovery to prevent flapping (%)",
    )
    fail_open: bool = Field(
        default=True,
        description="Allow automation when error budget query fails",
    )
    cache_ttl_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Error budget cache TTL (seconds)",
    )

    # 티어별 차등 임계치
    tier_thresholds_enabled: bool = Field(
        default=False,
        description="Enable per-tier differentiated thresholds (False uses global thresholds)",
    )
    tier_thresholds: dict[str, dict[str, float]] = Field(
        default={
            "critical": {
                "critical_threshold_percent": 15.0,
                "warning_threshold_percent": 30.0,
            },
            "standard": {
                "critical_threshold_percent": 10.0,
                "warning_threshold_percent": 20.0,
            },
            "non_essential": {
                "critical_threshold_percent": 5.0,
                "warning_threshold_percent": 10.0,
            },
        },
        description="Per-tier differentiated thresholds. Based on VALID_TIER_IDS (service criticality).",
    )

    # 리전별 임계치 오버라이드
    regional_thresholds_enabled: bool = Field(
        default=False,
        description="Enable per-region threshold overrides",
    )
    regional_thresholds: dict[str, dict[str, float]] = Field(
        default={},
        description=(
            "Per-region threshold overrides. "
            "Keys must match ClusterIdentity.region values. "
            "Example: {'seoul': {'critical_threshold_percent': 15.0}}"
        ),
    )

    # Fail-Open Rate Limiting (최소한의 제약이 있는 방임)
    fail_open_rate_limit_enabled: bool = Field(
        default=False,
        description="Apply rate limiting during fail-open mode",
    )
    fail_open_rate_limit_per_minute: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum allowed requests per minute during fail-open",
    )
    fail_open_rate_limit_window_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="Rate limit sliding window size (seconds)",
    )

    # Circuit Breaker (빠른 실패 처리)
    circuit_breaker_enabled: bool = Field(
        default=False,
        description="Enable Circuit Breaker",
    )
    circuit_breaker_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Consecutive failure count threshold",
    )
    circuit_breaker_recovery_timeout: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Circuit recovery wait time (seconds)",
    )

    # 알림 설정
    alert_on_fail_open: bool = Field(
        default=True,
        description="Send alert when fail-open is triggered",
    )
    alert_cooldown_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Cooldown before resending the same alert (seconds)",
    )

    def to_dict(self) -> dict[str, Any]:
        """설정을 딕셔너리로 변환."""
        return {
            "enabled": self.enabled,
            "critical_threshold_percent": self.critical_threshold_percent,
            "warning_threshold_percent": self.warning_threshold_percent,
            "threshold_hysteresis_buffer_percent": self.threshold_hysteresis_buffer_percent,
            "fail_open": self.fail_open,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "tier_thresholds_enabled": self.tier_thresholds_enabled,
            "tier_thresholds": self.tier_thresholds,
            "regional_thresholds_enabled": self.regional_thresholds_enabled,
            "regional_thresholds": self.regional_thresholds,
            "fail_open_rate_limit_enabled": self.fail_open_rate_limit_enabled,
            "fail_open_rate_limit_per_minute": self.fail_open_rate_limit_per_minute,
            "fail_open_rate_limit_window_seconds": self.fail_open_rate_limit_window_seconds,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "circuit_breaker_failure_threshold": self.circuit_breaker_failure_threshold,
            "circuit_breaker_recovery_timeout": self.circuit_breaker_recovery_timeout,
            "alert_on_fail_open": self.alert_on_fail_open,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
        }

    def get_thresholds_for_tier(self, tier_id: str) -> tuple[float, float]:
        """
        티어별 (critical_threshold, warning_threshold) 반환.

        tier_thresholds_enabled=False면 글로벌 임계치 반환.

        Args:
            tier_id: "critical" | "standard" | "non_essential"

        Returns:
            (critical_threshold_percent, warning_threshold_percent)
        """
        if not self.tier_thresholds_enabled:
            return self.critical_threshold_percent, self.warning_threshold_percent

        tier_config = self.tier_thresholds.get(tier_id)
        if tier_config is None:
            return self.critical_threshold_percent, self.warning_threshold_percent

        return (
            tier_config.get(
                "critical_threshold_percent", self.critical_threshold_percent
            ),
            tier_config.get(
                "warning_threshold_percent", self.warning_threshold_percent
            ),
        )

    def get_thresholds_for_region(self, region: str) -> tuple[float, float]:
        """
        리전별 (critical_threshold, warning_threshold) 반환.

        regional_thresholds_enabled=False면 글로벌 임계치 반환.

        Args:
            region: 리전 식별자 (e.g., "seoul", "tokyo")

        Returns:
            (critical_threshold_percent, warning_threshold_percent)
        """
        if not self.regional_thresholds_enabled:
            return self.critical_threshold_percent, self.warning_threshold_percent

        region_config = self.regional_thresholds.get(region)
        if region_config is None:
            return self.critical_threshold_percent, self.warning_threshold_percent

        return (
            region_config.get(
                "critical_threshold_percent", self.critical_threshold_percent
            ),
            region_config.get(
                "warning_threshold_percent", self.warning_threshold_percent
            ),
        )

    def get_effective_thresholds(
        self,
        tier_id: str | None = None,
        region: str | None = None,
    ) -> tuple[float, float]:
        """
        최종 적용 임계치 반환.

        우선순위:
        1. regional_thresholds[region] (리전 명시 오버라이드)
        2. tier_thresholds[tier_id] (티어별 기본값)
        3. global (critical_threshold_percent, warning_threshold_percent)

        Returns:
            (critical_threshold_percent, warning_threshold_percent)
        """
        # 1단계: 리전 오버라이드 확인
        if region and self.regional_thresholds_enabled:
            region_config = self.regional_thresholds.get(region)
            if region_config:
                return (
                    region_config.get(
                        "critical_threshold_percent",
                        self.critical_threshold_percent,
                    ),
                    region_config.get(
                        "warning_threshold_percent",
                        self.warning_threshold_percent,
                    ),
                )

        # 2단계: 티어별 확인
        if tier_id and self.tier_thresholds_enabled:
            return self.get_thresholds_for_tier(tier_id)

        # 3단계: 글로벌 기본값
        return self.critical_threshold_percent, self.warning_threshold_percent

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ErrorBudgetGateSettings:
        """딕셔너리에서 설정 생성 (runtime config 지원)."""
        valid_keys = {k: v for k, v in data.items() if k in cls.model_fields}
        return cls(**valid_keys)


# =============================================================================
# Singleton
# =============================================================================


def get_error_budget_gate_settings() -> ErrorBudgetGateSettings:
    """Get cached ErrorBudgetGateSettings instance."""
    from baldur.settings.root import get_config

    return get_config().services_group.error_budget_gate


def reset_error_budget_gate_settings() -> None:
    """Reset cached settings (for testing)."""
    from baldur.settings.root import get_config

    try:
        del get_config().services_group.__dict__["error_budget_gate"]
    except KeyError:
        pass
