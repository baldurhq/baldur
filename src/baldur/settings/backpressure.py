"""
Backpressure Settings - Pydantic v2.

Auto-Scaling & Backpressure 설정.
트래픽 제어용 설정 (기존 ScaleSettings와 역할 분리).

Moved from: scaling/config.py (위치 통일)

환경변수 접두사: BALDUR_BACKPRESSURE_
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings

from baldur.settings.base import make_settings_config
from baldur.settings.field_types import (
    Percentage,
    Probability,
    ShortInterval,
)


class BackpressureLevel(str, Enum):
    """
    Backpressure 레벨.

    큐 크기에 따라 시스템 부하 상태를 나타냅니다.
    Ordering: NONE < LOW < MEDIUM < HIGH < CRITICAL (severity-based).
    """

    NONE = "none"  # 정상 상태
    LOW = "low"  # 약간 과부하
    MEDIUM = "medium"  # 중간 과부하
    HIGH = "high"  # 높은 과부하
    CRITICAL = "critical"  # 위험 (긴급 조치 필요)

    @property
    def severity(self) -> int:
        """Numeric severity for ordering comparisons."""
        return _BP_SEVERITY_ORDER[self]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] >= _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] > _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __le__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] <= _BP_SEVERITY_ORDER[other]
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, BackpressureLevel):
            return _BP_SEVERITY_ORDER[self] < _BP_SEVERITY_ORDER[other]
        return NotImplemented


_BP_SEVERITY_ORDER: dict[BackpressureLevel, int] = {
    BackpressureLevel.NONE: 0,
    BackpressureLevel.LOW: 1,
    BackpressureLevel.MEDIUM: 2,
    BackpressureLevel.HIGH: 3,
    BackpressureLevel.CRITICAL: 4,
}


class BackpressureStrategy(str, Enum):
    """
    Backpressure 전략.

    과부하 시 어떤 방식으로 대응할지 결정합니다.
    """

    DROP_OLDEST = "drop_oldest"  # 오래된 항목 삭제
    DROP_NEWEST = "drop_newest"  # 최신 항목 삭제
    REJECT = "reject"  # 거부 (HTTP 503)
    THROTTLE = "throttle"  # Rate Limit 적용
    QUEUE = "queue"  # 대기열에 추가


# =============================================================================
# AIMD (Additive Increase, Multiplicative Decrease) 패턴
# 레벨별 Rate 감소 배율
# =============================================================================

LEVEL_RATE_MULTIPLIERS: dict[BackpressureLevel, float] = {
    BackpressureLevel.NONE: 1.0,  # 정상: 100% 처리율
    BackpressureLevel.LOW: 1.0,  # 약간: 유지
    BackpressureLevel.MEDIUM: 0.9,  # 중간: 90%로 감소
    BackpressureLevel.HIGH: 0.8,  # 높음: 80%로 감소
    BackpressureLevel.CRITICAL: 0.5,  # 위험: 50%로 급감 (AIMD의 MD 부분)
}


class BackpressureSettings(BaseSettings):
    """
    Auto-Scaling & Backpressure 설정.

    환경변수:
        BALDUR_BACKPRESSURE_ENABLED=true
        BALDUR_BACKPRESSURE_DEFAULT_STRATEGY=throttle
        BALDUR_BACKPRESSURE_MAX_RATE_PER_SECOND=1000
        ...
    """

    model_config = make_settings_config("BALDUR_BACKPRESSURE_")

    # Backpressure 활성화
    backpressure_enabled: bool = Field(
        default=False,
        description="Enable/disable backpressure",
    )

    # 기본 전략
    default_strategy: BackpressureStrategy = Field(
        default=BackpressureStrategy.THROTTLE,
        description="Default backpressure strategy",
    )

    # 큐 임계치 (큐 크기에 따른 레벨 결정)
    queue_low_threshold: int = Field(
        default=100,
        ge=1,
        description="LOW level queue size threshold",
    )
    queue_medium_threshold: int = Field(
        default=500,
        ge=1,
        description="MEDIUM level queue size threshold",
    )
    queue_high_threshold: int = Field(
        default=1000,
        ge=1,
        description="HIGH level queue size threshold",
    )
    queue_critical_threshold: int = Field(
        default=5000,
        ge=1,
        description="CRITICAL level queue size threshold",
    )

    # Rate Limit (처리/초)
    max_rate_per_second: float = Field(
        default=1000.0,
        ge=1.0,
        description="Maximum processing rate (items/second)",
    )
    min_rate_per_second: float = Field(
        default=10.0,
        ge=1.0,
        description="Minimum processing rate (items/second)",
    )

    # Rate 조절 파라미터
    rate_increase_factor: float = Field(
        default=1.1,
        ge=1.0,
        le=2.0,
        description="Rate increase factor (on recovery)",
    )
    rate_adjust_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        description="Rate adjustment interval (seconds)",
    )

    # 큐 크기 캐싱 (Redis 네트워크 지연 방지)
    queue_size_cache_ttl_seconds: float = Field(
        default=2.0,
        ge=0.5,
        le=10.0,
        description="Queue size cache TTL (seconds)",
    )

    # Prometheus 메트릭 설정
    metrics_enabled: bool = Field(
        default=False,
        description="Enable Prometheus metrics",
    )
    metrics_prefix: str = Field(
        default="baldur_",
        description="Metrics name prefix",
    )

    # HPA 설정
    hpa_enabled: bool = Field(
        default=False,
        description="Enable HPA custom metrics",
    )
    hpa_target_queue_depth: int = Field(
        default=100,
        ge=1,
        description="HPA target queue depth",
    )

    # Multi-process Redis sync for LS stats (ENT)
    redis_sync_enabled: bool = Field(
        default=False,
        description="Enable periodic Redis sync for multi-process LS stats",
    )

    # Graceful Degradation
    graceful_degradation_enabled: bool = Field(
        default=False,
        description="Enable graceful degradation",
    )

    # CPU 사용률 기반 Rate 감쇠 임계치
    resource_cpu_high_threshold: Percentage = Field(
        default=80.0,
        description="Reduce rate to 50% when CPU usage exceeds this threshold",
    )
    resource_cpu_critical_threshold: Percentage = Field(
        default=90.0,
        description="Reduce rate to 10% when CPU usage exceeds this threshold",
    )

    # 503 응답 커스터마이징
    reject_message: str = Field(
        default="Service temporarily unavailable due to high load",
        description="Response message for 503 rejection",
    )
    reject_retry_after_seconds: ShortInterval = Field(
        default=5,
        description="Retry-After header value (seconds)",
    )

    # =========================================================================
    # Priority Watermark — 토큰 잔량 비율 임계치
    # 현재 토큰 비율이 이 값 미만이면 해당 tier 요청을 거부한다.
    # 환경변수 예: BALDUR_BACKPRESSURE_WATERMARK_STANDARD=0.4
    # =========================================================================
    watermark_critical: Probability = Field(
        default=0.0,
        description="Critical tier watermark threshold. Reject when token ratio falls below this.",
    )
    watermark_standard: Probability = Field(
        default=0.3,
        description="Standard tier watermark threshold. Reject when token ratio falls below this.",
    )
    watermark_non_essential: Probability = Field(
        default=0.6,
        description="Non-essential tier watermark threshold. Reject when token ratio falls below this.",
    )

    # =========================================================================
    # External Level TTL — Throttle SLA → RateController bridge
    # =========================================================================
    external_level_ttl_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=120.0,
        description=(
            "TTL for external backpressure level from Throttle SLA events. "
            "Acts as a lease — each event reception renews the TTL."
        ),
    )

    def get_level_for_queue_size(self, queue_size: int) -> BackpressureLevel:
        """
        큐 크기에 따른 Backpressure 레벨 반환.

        Args:
            queue_size: 현재 큐 크기

        Returns:
            BackpressureLevel: 해당하는 레벨
        """
        if queue_size >= self.queue_critical_threshold:
            return BackpressureLevel.CRITICAL
        if queue_size >= self.queue_high_threshold:
            return BackpressureLevel.HIGH
        if queue_size >= self.queue_medium_threshold:
            return BackpressureLevel.MEDIUM
        if queue_size >= self.queue_low_threshold:
            return BackpressureLevel.LOW
        return BackpressureLevel.NONE

    def get_rate_multiplier(self, level: BackpressureLevel) -> float:
        """
        레벨별 Rate 감소 배율 반환 (AIMD 패턴).

        Args:
            level: Backpressure 레벨

        Returns:
            float: Rate 배율 (0.0 ~ 1.0)
        """
        return LEVEL_RATE_MULTIPLIERS.get(level, 1.0)

    def get_priority_watermarks(self) -> dict[str, float]:
        """Tier별 Watermark 임계치 딕셔너리 반환."""
        return {
            "critical": self.watermark_critical,
            "standard": self.watermark_standard,
            "non_essential": self.watermark_non_essential,
        }

    def get_retry_after_for_level(self, level: BackpressureLevel) -> int:
        """BackpressureLevel별 Retry-After 값 반환.

        부하가 높을수록 클라이언트 재시도 간격을 늘려
        Retry Storm을 방지한다.
        base * 배율로 계산하며 단일 설정 노브로 전체 스케일 조절 가능.

        Args:
            level: 현재 Backpressure 레벨

        Returns:
            Retry-After 값 (초)
        """
        base = self.reject_retry_after_seconds
        multiplier = {
            BackpressureLevel.NONE: 1,
            BackpressureLevel.LOW: 1,
            BackpressureLevel.MEDIUM: 2,
            BackpressureLevel.HIGH: 4,
            BackpressureLevel.CRITICAL: 8,
        }
        return base * multiplier.get(level, 1)


def get_backpressure_settings() -> BackpressureSettings:
    from baldur.settings.root import get_config

    return get_config().scaling.backpressure


def reset_backpressure_settings() -> None:
    from baldur.settings.root import get_config

    try:
        del get_config().scaling.__dict__["backpressure"]
    except KeyError:
        pass
