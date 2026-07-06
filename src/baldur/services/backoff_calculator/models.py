"""
Backoff Calculator Models

Dataclasses and value types for the backoff calculator package.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from baldur.core.serializable import SerializableMixin
from baldur.settings import get_config

# =============================================================================
# Constants
# =============================================================================

# 시스템 전체 타임아웃 (30분) - 이를 초과하면 사용자 체감 불가
SYSTEM_TIMEOUT_SECONDS = 1800


# =============================================================================
# Throttle State Models
# =============================================================================


@dataclass
class ThrottleState:
    """AdaptiveThrottle 현재 상태 스냅샷."""

    current_limit: int
    initial_limit: int
    emergency_level: int = 0
    full_stop_active: bool = False
    sla_warning_active: bool = False
    sla_critical_active: bool = False
    recovery_dampening_active: bool = False
    error_budget_reduction_active: bool = False


@dataclass
class PushBasedThrottleStateCache:
    """
    EventBus 푸시 기반 Throttle 상태 캐시.

    매번 get_stats() 호출로 Lock 경합하는 대신
    EventBus 이벤트를 구독하여 상태 변경 시에만 캐시를 업데이트합니다.
    """

    multiplier: float = 1.0
    reason: str = "normal"
    last_updated: float = 0.0
    full_stop_active: bool = False
    emergency_level: int = 0

    # 캐시 유효 시간 (EventBus 이벤트 누락 대비 폴백)
    max_cache_age_seconds: float = 30.0

    def is_stale(self) -> bool:
        """캐시가 오래되었는지 확인 (Fail-safe)."""
        return (time.time() - self.last_updated) > self.max_cache_age_seconds


@dataclass
class GlobalThrottleState(SerializableMixin):
    """
    클러스터 전체 Throttle 상태 (Redis 저장).

    Pod 간 상태 공유를 위한 집계 데이터 구조.
    """

    cluster_avg_rtt_ms: float = 0.0
    cluster_emergency_level: int = 0
    cluster_sla_warning_count: int = 0
    cluster_sla_critical_count: int = 0
    reporting_pod_count: int = 0
    last_updated: float = 0.0


@dataclass
class BackoffConfig:
    """Configuration for exponential backoff calculation."""

    base: int = 4  # Base for exponential (4^n seconds)
    max_delay: int = 180  # Maximum wait time (3 minutes)
    jitter_percent: int = 25  # ±25% random jitter
    min_delay: int = 1  # Minimum delay in seconds

    @classmethod
    def from_settings(cls, domain: str | None = None) -> BackoffConfig:
        """
        Load configuration from core config.

        Args:
            domain: Optional domain for per-domain overrides

        Returns:
            BackoffConfig with merged settings
        """
        root = get_config()
        backoff_settings = root.core.backoff
        retry_settings = root.core.retry

        # Legacy backoff fields live in BackoffSettings (doc 359 Option B)
        config = cls(
            base=backoff_settings.legacy_base,
            max_delay=int(retry_settings.max_delay),
            jitter_percent=backoff_settings.legacy_jitter_percent,
            min_delay=backoff_settings.legacy_min_delay,
        )

        # Apply per-domain overrides if available
        if domain:
            # Get domain config from centralized config
            full_config = get_config()
            domain_configs = getattr(full_config, "domain_configs", {})
            domain_config = domain_configs.get(domain, {})
            if "backoff_base" in domain_config:
                config.base = domain_config["backoff_base"]

        return config
